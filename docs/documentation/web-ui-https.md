# HTTPS for the web UI

How the web UI is served over HTTPS on a single Pi, how devices trust it, and
how to roll it back. Background and decisions:
[the plan](../plans/general_open_web-ui-https.md).

## How it works

- A dedicated Caddy service, `mpdrussoundpi-web-proxy`, terminates TLS on
  port **443** and forwards to the web container on `127.0.0.1:8000`. It is
  separate from the playback-agent proxy (port 8443) and from the package's
  `caddy.service`, which stays disabled.
- Port **80** only redirects to `https://` and serves the trust page at
  `/ca/`. Port 443 serves the trust page at `/ca/` too, because browsers such
  as Chrome upgrade `http://` links to `https://` on their own. On a device
  that does not trust the CA yet, that means one certificate warning before
  the page.
- The certificates come from a **local CA owned by this installation**. Its
  name constraints limit it to the private address ranges and the Pi's names
  (`musicpi.local`, `musicpi` by default). A device that trusts it trusts it
  for nothing else.
- The server certificate is valid for 397 days. The timer
  `mpdrussoundpi-web-tls-renew.timer` checks it daily and reissues it, with a
  new key, when less than 60 days are left or the names changed. It then
  restarts the proxy (about a second; open pages reconnect). Not a reload:
  Caddy 2.6 skips a reload whose configuration is unchanged, so the new
  certificate would never be served, and a forced reload crashed it while
  event streams were open.
- `tool/web_tls.py` does all certificate work. Ansible copies it to
  `/usr/local/lib/mpdrussoundpi/web_tls.py`, where it runs with the system
  Python, independent of the deployment tooling.

Files on the Pi (`/etc/mpdrussoundpi-web-tls`, `root:caddy 0750`):

| Path | Mode | Purpose |
|---|---|---|
| `ca/ca.key` | `root 0400` in a `0700` directory | CA private key; never leaves the Pi; **back it up** |
| `ca/ca.crt` | `0644` | The root certificate devices trust |
| `server.key` | `root:caddy 0640` | Server key, replaced on every renewal |
| `server.crt` | `0644` | Server certificate |
| `public/` | `0755` | Trust page, certificate and iOS profile served at `/ca/`, plus `status.json` (certificate expiry, names, CA fingerprint) for the Status page |
| `nginx-disabled` | `0644` | Marker: this play disabled the nginx default site |

**The CA is created once and never replaced** by the play or the timer. If the
CA key is lost, every device has to trust a new CA. Include
`/etc/mpdrussoundpi-web-tls/ca/` in the host backup.

## Install

Caddy and OpenSSL must be installed on the Pi (they are, for the playback
proxy). Port 80 is taken from nginx only if nginx serves nothing but Debian's
default site; otherwise the play stops and says so.

```sh
# Prepare: tool, CA, certificate, proxy configuration and units. No port is taken.
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-web-tls.yml --limit musicpi

# Take ports 80 and 443 and start the proxy and the renewal timer.
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-web-tls.yml --limit musicpi \
  -e web_tls_start=true
```

The play prints the **CA SHA-256 fingerprint** and copies the public root
certificate to `docker/secrets/musicpi-web-ca.crt` on the controller. It
verifies `https://<pi>/status` with the CA pinned, the redirect on port 80
and the trust page.

Inputs (defaults in `ansible/roles/deployment_inputs/defaults/web_tls.yml`):
`web_tls_dns_names` (default `<hostname>.local`, `<hostname>`),
`web_tls_ip_addresses` (default the inventory address),
`web_tls_permitted_dns` and `web_tls_permitted_ips` (CA constraints, fixed
when the CA is created), and the ports `web_tls_https_port`,
`web_tls_http_port`, `web_tls_upstream_port` and `web_tls_admin_port` (2021).

Until the web container is moved to loopback (stage 4 of the plan), the UI is
still reachable at `http://<pi>:8000` as well.

## Web application settings

The deployment's HTTPS layout sets these together with the loopback publish
address ([container deployment](./container-deployment.md), "HTTPS layout").
The reviewed tooling accepts all of them as one change and never the proxy
settings without the loopback listener:

- `RUSSOUND_TRUSTED_PROXY_COUNT=1`: trust Caddy's `X-Forwarded-*` headers, so
  the app sees HTTPS, sets `Secure` cookies and logs the client's address.
  **Only safe on a loopback publish address**: on a port the network can reach,
  any client could claim HTTPS or another address and bypass the login rules.
- `RUSSOUND_REQUIRE_HTTPS_LOGIN=true`: the administrator login and
  reauthentication are refused over plain HTTP. The Status page then shows the
  HTTPS address instead of the password field. The server does not start with
  this setting but without a trusted proxy, because nobody could log in.
- `public/` mounted read-only at `/run/web-tls`: the Status page's web section
  shows "HTTPS certificate: valid until …", a warning below 30 days (the
  renewal has failed) and a problem once expired.

## Trust the CA on a device

Open `http://<pi>/ca/` on the device (or `https://<pi>/ca/`, accepting the
certificate warning once) and follow the instructions for its platform. Before
trusting, compare the fingerprint on the page with the one the play printed, or
with:

```sh
sudo python3 /usr/local/lib/mpdrussoundpi/web_tls.py status
```

The trust page arrives over plain HTTP or an untrusted connection, so only the
comparison proves it is the right certificate.

- **iPhone, iPad:** install the profile, then turn on full trust under
  Settings → General → About → Certificate Trust Settings.
- **Android:** install the certificate as a CA certificate (Settings →
  Security → Encryption & credentials). Chrome trusts it afterwards.
- **macOS:** open the certificate and set it to "Always Trust" in Keychain Access.
- **Windows:** install it into "Trusted Root Certification Authorities" of the
  current user.

## Operate

```sh
systemctl status mpdrussoundpi-web-proxy mpdrussoundpi-web-tls-renew.timer
sudo python3 /usr/local/lib/mpdrussoundpi/web_tls.py status       # fingerprint, names, expiry
sudo systemctl start mpdrussoundpi-web-tls-renew.service          # check now; renews only when due
```

Changing the Pi's address or names: set `web_tls_ip_addresses` or
`web_tls_dns_names` and run the play again. It issues a new server
certificate. Devices need nothing new as long as the address stays inside the
CA's permitted ranges and names.

## Roll back

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-web-tls.yml --limit musicpi --tags rollback
```

This stops and disables the proxy and the timer and restores the nginx default
site if this play had disabled it. The CA and the certificates stay, so the
devices remain trusted for a later re-enable.

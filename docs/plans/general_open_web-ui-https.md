# HTTPS for the web UI

**Status:** open. Planned and decided on 2026-11-22; nothing implemented yet. Brought forward from the "Later: whole-UI HTTPS" stage of the
[user-triggered image updates](./general_open_user-triggered-image-updates.md)
(decision 6) so that no interim login protection is needed. A prerequisite for
[administrator mode and protected settings](./web_open_admin-mode-and-protected-settings.md),
which puts the configuration behind the administrator login.

## Why

The UI is served over plain HTTP on port 8000. The administrator password, the
session cookie, the CSRF token, the settings and the JavaScript itself travel
unprotected on the local network. An interim login protocol (challenge-response,
SRP) was considered and rejected: it would hide only the password from passive
listeners, the session could still be taken over, an active attacker could
replace the script, and `crypto.subtle` does not exist on plain-HTTP pages
anyway. HTTPS solves all of it.

## Current state (checked on the Pi, 2026-11-22)

- The web container publishes `0.0.0.0:8000` (deployment spec `publish_host`,
  `publish_port`; compose `RUSSOUND_PUBLISH_HOST`/`RUSSOUND_PUBLISH_PORT`).
- **Caddy 2.6.2** is installed as a host service for the playback-agent proxy
  (`mpdrussoundpi-playback-agent-proxy.service`, port 8443, own Caddyfile,
  admin endpoint `127.0.0.1:2020`). Its certificate is self-signed for
  `IP:192.168.1.105`, valid until **2027-10-16** (the playback guide says ten
  years; the Pi's certificate has one year).
- **nginx** listens on port 80 with Debian's default site (`/var/www/html`
  placeholder). Nothing in this project uses it.
- The host name is `musicpi`; `avahi-daemon` runs, so `musicpi.local` resolves
  on most clients.
- The web application already supports a proxy: `RUSSOUND_TRUSTED_PROXY_COUNT`
  enables Werkzeug's `ProxyFix`, and the admin and session cookies set `Secure`
  from `request.is_secure`.
- The deployment tooling accepts a loopback `publish_host` for the managed
  gateway (only a standalone gateway requires a LAN listener). Its `http`
  verification check uses the spec's host and port.
- The secrets directory is mounted read-only into the web container.

## Decisions (confirmed by the user on 2026-11-22)

1. **TLS terminates in Caddy on the host**, not in the container. A second,
   dedicated unit and Caddyfile (`mpdrussoundpi-web-proxy`), separate from
   the playback-agent proxy, same hardening. Caddy listens on **443** and
   proxies to `127.0.0.1:8000`. The private key stays outside the container,
   and the web image does not change for TLS.
   Rejected: TLS inside the Flask/Werkzeug server (key in the container, no
   redirect listener, reloads restart the app), and the package's
   `caddy.service` (shared config with things this project does not own).
2. **The web container listens on loopback only**: `publish_host` becomes
   `127.0.0.1` through a deployment spec change, with
   `RUSSOUND_TRUSTED_PROXY_COUNT=1`. Port 8000 is then unreachable from the
   network, so nobody can bypass TLS by typing `:8000`.
3. **Port 80 only redirects** to `https://` and serves the CA download page
   (decision 5). The unused nginx default site is disabled (nginx is stopped
   and disabled if nothing else uses it). **No HSTS** for now: if a device
   loses the trust, HSTS would lock it out without a way to click through.
   Revisit when trust distribution has proven itself.
4. **Certificates come from a small local CA owned by this installation:**
   - root CA: ECDSA P-256, 10 years, with **name constraints** limited to
     the private address ranges (`10.0.0.0/8`, `172.16.0.0/12`,
     `192.168.0.0/16`) and the DNS names `musicpi.local` and `musicpi`
     (configurable). A device that trusts this root trusts it for nothing
     else, so a leaked key cannot impersonate other sites;
   - server certificate: SAN `IP:192.168.1.105`, `DNS:musicpi.local`,
     `DNS:musicpi`, **397 days** (stays under Apple's 825-day limit for
     user-installed roots and under the browsers' expectations);
   - **automatic renewal** on the Pi: a systemd timer reissues the server
     certificate when it has less than 60 days left and reloads Caddy. For
     that, the CA key lives on the Pi, root-only (`0400`, outside Caddy's
     data directory). The name constraints make that acceptable;
   - an IP or name change (see [network relocation](./general_idea_network-relocation.md))
     needs only a new server certificate, as long as the new address stays
     in the permitted ranges. No device has to trust anything new.

   Rejected or deferred:
   - **Self-signed server certificate** (like the playback proxy): every
     renewal or address change means trusting a new certificate on every
     device.
   - **Caddy's `tls internal`**: automatic, but its root has no name
     constraints and a 12-hour leaf rotation, which is harder to diagnose.
   - **Let's Encrypt with DNS-01**: no device trust needed, but it needs a
     public domain, a DNS API token on the Pi and local name resolution. It is
     kept as an option for installations with their own domain ("Later").
5. **Trust distribution:** `http://<pi>/ca` (served by the redirect listener)
   offers the root certificate (`.crt` for all platforms, plus a
   `.mobileconfig` profile for iOS) and short per-platform instructions. The
   **SHA-256 fingerprint** is printed by the Ansible play and shown on the Pi
   by a CLI command. The user compares it once, because the HTTP download
   itself can be swapped on the network.
6. **The application refuses logins over plain HTTP** once HTTPS is enabled
   (new setting, on by default in the managed deployment). If someone still
   reaches the app over HTTP, the sheet says to use the `https://` address
   instead of sending the password. The HTTP warning on the login sheet goes
   away over HTTPS.

## Design details

- **Caddyfile (sketch):**
  ```
  {
      admin 127.0.0.1:2021
      auto_https off
  }
  https://:443 {
      tls /etc/mpdrussoundpi-web-tls/server.crt /etc/mpdrussoundpi-web-tls/server.key
      reverse_proxy 127.0.0.1:8000 {
          flush_interval -1          # /api/events is server-sent events
      }
  }
  http://:80 {
      handle /ca*  { root * /etc/mpdrussoundpi-web-tls/public; file_server }
      handle       { redir https://{host}{uri} 308 }
  }
  ```
  The server-sent events stream (`/api/events`) must not be buffered; it
  is checked in acceptance. Request bodies and timeouts stay within Caddy's
  defaults, which already exceed the app's limits.
- **Web application:**
  - `RUSSOUND_ALLOWED_HOSTS` and the origin check accept the `https://` origin;
  - cookies are `Secure` through `ProxyFix` (already implemented; tested);
  - the new login-over-HTTP refusal;
  - the Status page shows the certificate's expiry in the host card (warning
    at 30 days, when automatic renewal has failed).
- **Deployment and the updater:**
  - an Ansible playbook `deploy-web-tls.yml` creates the CA (once, never
    overwritten), the server certificate, the Caddy unit and Caddyfile, the
    renewal timer and the CA page, and disables the nginx default site;
  - the `publish_host` change goes through the reviewed deployment tooling
    as a spec change. The `http` verification check keeps using
    `127.0.0.1:8000`, and a new check also verifies `https://` through Caddy
    with the CA pinned;
  - web-triggered updates keep working: the updater talks to Docker, not to
    the proxy, and Caddy keeps running while the container restarts (the
    page's reconnect and reload logic sees a short 502);
  - the evidence and backup paths do not change. The CA key goes into the
    documented host backup list, because losing it means re-trusting all
    devices.
- **Rollback:** stop the web proxy unit, set `publish_host` back to
  `0.0.0.0` and re-enable plain HTTP. The playbook documents this as its
  `--tags rollback`.

## Stages

- [x] **1. Decisions** confirmed by the user (2026-11-22), including disabling
      the nginx default site and keeping the CA key on the Pi for automatic renewal.
- [ ] **2. Certificates and proxy:** the playbook with the CA, the server
      certificate, the renewal timer, the Caddy unit and Caddyfile, the
      redirect and the CA page; tests for the templates and the certificate
      checks (SAN, constraints, validity).
- [ ] **3. Web application:** login refusal over HTTP, allowed origins,
      certificate expiry in the host card, sheet text; tests.
- [ ] **4. Deployment spec:** loopback `publish_host`, trusted proxy count,
      the https verification check; tests; documentation in
      `container-deployment.md`, `README.md` (Security) and `web/API.md`.
- [ ] **5. Rollout on the Pi:** run the playbook, compare the fingerprint,
      trust the root on the user's devices, then switch `publish_host`.
- [ ] **6. Acceptance:**
  - desktop (macOS Safari and Chrome) and phone (iOS Safari, Android Chrome)
    without certificate warnings, by IP and by `musicpi.local`;
  - `http://` redirects; `:8000` is unreachable from the LAN;
  - login over HTTPS sets a `Secure` cookie; a login over HTTP is refused;
  - live updates over server-sent events keep working;
  - a web-triggered update and a restore over HTTPS, including the page's
    automatic reload;
  - a forced renewal (temporarily lowered threshold) reloads Caddy without
    dropping the page;
  - name constraints are honoured: a test certificate for another name
    signed by the CA is rejected by the browsers.

## Risks and notes

- **Device trust is manual** and has to be repeated on every new device. The
  CA page and the fingerprint keep it short; without it the browsers show
  warnings, which users learn to click through. Documented, not hidden.
- **Name constraints support:** current Chrome, Firefox, Safari and Android
  honour them on roots. They are checked in acceptance instead of assumed.
- **CA key on the Pi:** whoever is root on the Pi can issue certificates, but
  only for the permitted names and ranges. Root on the Pi already means full
  control of this installation.
- **Port 80 and 443** become owned by this project. Other software on the Pi
  that wants them must go through this Caddy.
- **Playback-agent proxy certificate** (expires 2027-10-16) is not touched by
  this plan; see "Later".

## Later

- Issue the playback-agent proxy certificate from the same CA and renew it
  automatically (removes the 2027-10-16 deadline).
- HSTS once device trust has proven itself.
- Let's Encrypt (DNS-01) for installations with their own domain.
- A separate host for the web UI (split-host) uses the same CA.

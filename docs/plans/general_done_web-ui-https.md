# HTTPS for the web UI

**Status:** done (2026-11-23). Planned and decided on 2026-11-22; implemented, rolled out on the reference Pi and accepted on 2026-11-23. The one remaining check, a web update and a restore over HTTPS, moved to the [user-triggered image updates](./general_open_user-triggered-image-updates.md) acceptance. Brought forward from the "Later: whole-UI HTTPS" stage of the
[user-triggered image updates](./general_open_user-triggered-image-updates.md)
(decision 6) so that no interim login protection is needed. A prerequisite for
[administrator mode and protected settings](./web_done_admin-mode-and-protected-settings.md),
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
    as a named layout addition (see stage 4). The `http` verification check
    uses `127.0.0.1:8000` and also verifies `https://` through Caddy with the
    CA pinned;
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
- [x] **2. Certificates and proxy** (2026-11-23):
  - `tool/web_tls.py`: stdlib and `openssl` only, so it runs with the Pi's
    system Python. It creates the CA once (ECDSA P-256, 10 years,
    `pathlen:0`, critical name constraints), issues the server certificate
    with a new key, verifies chain, constraints and key before replacing
    anything, and writes the trust page, the `.crt` and an iOS profile;
  - `ansible/playbooks/deploy-web-tls.yml` with its input defaults: tool,
    CA, certificate, Caddyfile (validated by Caddy), proxy unit with only
    `CAP_NET_BIND_SERVICE`, daily renewal service and timer, the nginx guard,
    verification with the CA pinned, and `--tags rollback`;
  - templates `web-proxy.Caddyfile.j2`, `web-proxy.service.j2`,
    `web-tls-renew.{service,timer}.j2`;
  - tests: `tests/test_web_tls.py` (real OpenSSL: constraints hold even for a
    certificate signed outside the tool, renewal, idempotence, refusal
    before writing) and `tests/test_web_tls_playbook.py` (templates, gating,
    rollback, input validation, syntax check);
  - guide: [HTTPS for the web UI](../documentation/web-ui-https.md);
  - checked on the Pi without changing it: the tool runs with Python 3.13
    and OpenSSL 3.5, and Caddy 2.6.2 accepts the generated configuration.
- [x] **3. Web application** (2026-11-23):
  - `RUSSOUND_REQUIRE_HTTPS_LOGIN`: login and reauthentication over plain
    HTTP get `403 https_required` before the body is read (nothing checked,
    nothing counted). The server refuses to start with it but without a
    trusted proxy;
  - `GET /api/admin/session` reports `secure`, `https_required` and
    `https_url`. The Status page shows the plain-HTTP warning only on HTTP
    and, where HTTPS is required, a link to the HTTPS address instead of the
    password field;
  - `tool/web_tls.py` also publishes `public/status.json`. The web
    application reads it from `RUSSOUND_WEB_TLS_STATUS_FILE` (default
    `/run/web-tls/status.json`) and shows the "HTTPS certificate" row in the
    web section, whose level now follows its rows;
  - no change was needed for origins: the same-origin check compares with
    `request.host_url`, which is `https://…` behind the trusted proxy, and
    browsers also send `Sec-Fetch-Site: same-origin`;
  - **finding:** `RUSSOUND_TRUSTED_PROXY_COUNT` must only be set together
    with the loopback publish address (stage 4). On a port the LAN can reach,
    a client could send `X-Forwarded-Proto: https` and `X-Forwarded-For` to
    bypass the HTTPS rule and the per-client login throttle;
  - tests in `test_admin_auth.py`, `test_status_overview.py`,
    `test_status_ui.js` and `test_web_tls.py`; `web/API.md`, `README.md` and
    the guide updated.
- [x] **4. Deployment layout** (2026-11-23). The tooling accepts no spec or
  layout changes apart from named additions, so the HTTPS switch became the
  second named addition next to the updater socket mount
  (`tool/deployment_compose.py`, `layout_changes`):
  - one all-or-nothing change on the web service: listener on `127.0.0.1`,
    exactly `RUSSOUND_TRUSTED_PROXY_COUNT=1` and
    `RUSSOUND_REQUIRE_HTTPS_LOGIN=true`, and exactly the read-only bind of
    `/etc/mpdrussoundpi-web-tls/public` to `/run/web-tls`. It is shown as
    `layout_change`, may arrive with the updater mount, and partial,
    altered or removing changes are refused;
  - general rule: proxy settings or the certificate mount without a loopback
    listener are refused for any layout;
  - preflight requires the certificate status and a running
    `mpdrussoundpi-web-proxy.service`;
  - the `http` runtime check also requests `/api/rooms` through the proxy,
    verifying the certificate against the local CA for the published name
    (the name and port come from `status.json`, which now also contains
    `https_url`). No new check name, because the required checks are part of
    the recorded intent;
  - recovery uses the backed-up layout and specification, so a restore goes
    back to the LAN listener, and the next update applies the change again;
  - tests in `test_deployment_operator.py` and `test_deployment_https.py` (a
    real TLS server with a certificate from the tool); rollout steps in
    `container-deployment.md`.
- [ ] **5. Rollout on the Pi** (started 2026-11-23):
  - [x] trusted tooling upgraded from `bd36633` to `cdbc3c5`, updater restarted
    while idle;
  - [x] `deploy-web-tls.yml` run, first without and then with
    `web_tls_start=true`. The CA was created (SHA-256
    `9E:44:9C:3D:F1:B9:5D:BC:FE:3F:2B:DD:FF:8C:5C:D2:82:3A:81:8D:77:C5:62:1E:5B:10:08:68:29:BE:3D:01`),
    the server certificate is valid until 2027-12-24, and nginx is stopped and
    disabled (marker written). Checked from the controller: HTTPS by IP and by
    `musicpi.local` with the CA pinned, the 308 redirect, `/ca/` on both
    ports (added in PR #101 because Chrome upgrades `http://` links), the
    unbuffered event stream, and the playback proxy on 8443 unaffected;
  - [x] HTTPS compose input on the target
    (`/etc/mpdrussoundpi-deploy/russound-dev-compose-https.json`, root
    `0600`). The tooling's `layout_changes` on the Pi accepts it as exactly the
    HTTPS layout;
  - [x] the controller's private target specification (backup
    `pi-target.json.before-https` next to it) has `publish_host` set to
    `127.0.0.1` and `compose` pointing to the new input; the updater was
    reinstalled with it (`deploy-updater.yml`);
  - [ ] devices trust the CA;
  - [x] the update from the Status page to `cdbc3c5` (operation `e98f54a1`)
    applied the HTTPS layout: listener on `127.0.0.1:8000`, both proxy
    settings set, verified-complete;
  - **bug found:** afterwards nobody could log in. The session reported
    `secure: false` over HTTPS because Waitress 3 deletes `X-Forwarded-*`
    from peers it does not trust (`clear_untrusted_proxy_headers`, on by
    default) before ProxyFix sees them. `RUSSOUND_TRUSTED_PROXY_COUNT` had
    therefore never worked under Waitress, and the stage 3 tests used the Flask
    test client, which skips Waitress. Fix: with a trusted proxy, Waitress
    keeps the headers and ProxyFix evaluates them (safe because the tooling
    allows the proxy settings only with a loopback listener). A new test runs
    a real Waitress server. The fixed image has to be installed with the SSH
    operator, because the page cannot log in;
  - [x] fixed image `7573cf2` installed with the SSH operator (operation
    `15f5d587`, approved by the user at the terminal, because the operator
    reads the approval from `/dev/tty`). Verified-complete, all five checks
    passed (the `http` check now includes the HTTPS route through the proxy).
    Checked afterwards:
    - `GET /api/admin/session` over HTTPS reports `secure: true`;
    - `http://` redirects (308), and port 8000 is closed to the LAN (only
      `127.0.0.1:8000` listens);
    - the certificate mount is read-only in the container, and the Status
      page shows "HTTPS certificate: Valid until 2027-12-24";
    - the cleanup kept the previous version (`cdbc3c5`, recovery tag
      `15f5d587`), still 10 images; the renewal timer is scheduled; nginx and
      the package's Caddy are inactive; no warnings in the journals;
    - all components ok, 3 of 3 outputs playing.
- [x] **6. Acceptance** (2026-11-23):
  - [x] login over HTTPS works on the Mac and the phone (user, 2026-11-23).
    The audit log records the devices' real addresses (`192.168.1.56`, and
    the phone over IPv6 link-local), not the proxy's, so the per-client
    throttle sees real clients;
  - [x] `http://` redirects (308); `:8000` is unreachable from the LAN (only
    `127.0.0.1:8000` listens);
  - [x] the server refuses logins over plain HTTP: no plain-HTTP route to the
    application is left, and the refusal itself is covered by the tests;
  - [x] live updates over server-sent events work through the proxy;
  - [x] devices without certificate warnings: Mac (Safari, Chrome, Firefox)
    and phone, by `musicpi.local` and by IP;
  - [x] forced renewal through the real renewal service, with an open event
    stream and availability probes. **Found two problems:**
    - Caddy 2.6.2 skipped the reload ("config is unchanged"), so a renewed
      certificate was never served;
    - a forced reload panicked with an open stream, and a restart waited for
      open streams with the listener closed (about 20 s).

    Fixed in PR #106: the renewal restarts the proxy, and `grace_period 2s`
    caps the wait. Measured afterwards: the new certificate is served, no
    panic, about 2 s of refused connections, and pages reconnect on their own;
  - [x] name constraints honoured. A test certificate signed by the CA for
    `IP:192.168.1.105` plus `DNS:bank.example`, served on a temporary port, was
    rejected by OpenSSL and curl ("permitted subtree violation"), by Firefox
    (`SEC_ERROR_CERT_NOT_IN_NAME_SPACE`), by Chrome (`ERR_CERT_INVALID`) and by
    Safari. The test site, key and certificate were removed afterwards;
  - moved to the [user-triggered image updates](./general_open_user-triggered-image-updates.md)
    acceptance: a web-triggered update and a restore over HTTPS, including
    the page's automatic reload (needs the next real code change).

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

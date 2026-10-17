# Raspberry Pi playback agent deployment

Status: read-only S06 deployment completed on 2026-10-17. The initial isolated
agent rollout is recorded below; production IDs, the TLS proxy and the Docker
web status integration are now active. Playback write controls remain out of
scope.

## Scope and prerequisites

Use [deploy-playback-agent.yml](../ansible/playbooks/deploy-playback-agent.yml),
not the full [music Pi setup](../ansible/playbooks/setup-musicpi.yml). The latter
regenerates audio/MPD and restarts playback; it is not an additive agent rollout.
The dedicated play copies the current controller checkout's agent entry point,
dependency manifest and Python helpers into a separate directory. It does not
pull/reset the Pi's repository or modify its shared playback virtual environment.
Uncommitted local agent sources are therefore included; review them before use.

Prerequisites for a later approved run:

- Ansible on the controller, the existing inventory/SSH account and sudo
  permission for `loginctl enable-linger` if lingering is not already enabled.
  Run as the existing non-root audio user, not with global `--become`.
- Existing Pi Python 3 with `venv`/pip support and `pactl`; existing MPD/audio
  services. The agent-only play deliberately performs no apt upgrades or audio
  package installation.
- A live user manager with the audio user's owned mode-0700 `/run/user/<uid>`
  and D-Bus socket. The play refuses to bootstrap it: starting a missing user
  manager could implicitly activate existing enabled playback services.
- An operational config whose stream entries already have the approved S04
  persisted IDs/labels. The play validates with the read-only collector but
  does not migrate, seed or overwrite configuration. The production migration
  and assigned IDs are recorded in the production rollout section below.
- A controller-side private token file outside the repository, preferably in
  a private directory with mode 0600. Use a cryptographically random token
  of at least 32 non-whitespace ASCII characters; one trailing newline is fine.
  Supply only its path. Do not put the token in CLI arguments, inventory,
  source files or browser assets.
- Explicit deployment approval, normally a maintenance window after endurance testing. Capture config/
  intention checksums, daemon PIDs, queues, routes and gain before activation.
  These scripts do not replace that approval or the S06 acceptance procedure.

## Agent-only install and activation

The initial agent-only installation is complete. These commands remain
operator examples for a reviewed reinstall or token rotation. From the
repository root:

```sh
# Static controller-only validation; no target tasks.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml --syntax-check

# Later: install files/dependencies/unit without starting or enabling the agent.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  -e playback_agent_token_source=/private/path/playback-agent-token

# Later, only after migration and deployment approval: activate the agent.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  -e playback_agent_token_source=/private/path/playback-agent-token \
  -e playback_agent_start=true
```

## Prepare the Caddy TLS proxy

The selected D06 design keeps the agent bound to `127.0.0.1:8081` and uses a
separate Caddy service on the Pi, listening on TCP port `8443` by default.
`deploy-playback-agent-proxy.yml` does not install or upgrade Caddy: install it
through the Pi's approved package-management process first. The play requires
the `caddy` user/group, Caddy binary at `/usr/bin/caddy`, and OpenSSL. It writes
an isolated Caddyfile and systemd unit; it does not edit or restart the
package-provided `caddy.service`.

The play generates a self-signed certificate on the Pi with the configured DNS
name or IPv4 address as its subject alternative name. The private key remains
on the Pi with root/caddy-only access. It copies only the public certificate to
the controller's ignored `docker/secrets/musicpi-proxy.crt`; normal TLS
hostname verification remains enabled. The Docker URL host must exactly match
the certificate SAN and resolve from inside the web container. Do not use
`.local` unless it is actually resolvable inside Docker.
The play verifies the self-signed trust anchor, SAN, key match and at least
30 days of remaining certificate validity. It never overwrites an existing
certificate/key pair or renews it automatically; rotation requires a separately
backed-up, approved replacement procedure and coordinated proxy/web restart.

The Caddy route forwards only `/v1/health`, `/v1/capabilities`, `/v1/outputs`
and `/v1/outputs/*` to the loopback agent. It applies a source CIDR allowlist,
listens on an unprivileged port, disables HTTP/3 and keeps its administrative
endpoint on loopback. A host/network firewall is normally an additional
boundary. For this deployment the user explicitly declined a host firewall
rule; Caddy's source allowlist is the only ingress filter and must not be
treated as firewall enforcement. Keep this deployment on a trusted LAN and do
not add router port forwarding.

The Pi endpoint is `192.168.1.105` (also the inventory address). The initial
web rollout mistakenly used the Mac's Wi-Fi address, `192.168.1.56`, as the
proxy source. A later live probe showed that Docker Desktop's traffic reaches
the Pi from the Mac's Ethernet address, `192.168.1.142` (`en5`). On
2026-10-17, the user explicitly approved expanding Caddy's source allowlist to
the trusted LAN subnet `192.168.1.0/24`; this is the active matcher. The Pi
has no host INPUT firewall policy (`iptables INPUT` is ACCEPT); the nftables
service is disabled and its stock config flushes the ruleset. The user chose
not to install a host firewall rule. Caddy is therefore the sole source-address
filter. The TLS listener accepts handshakes on its interfaces; HTTP requests
from outside the allowed subnet are rejected.

The production proxy was initially installed with an exact-source matcher.
The current approved LAN-subnet policy can be applied with:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent-proxy.yml \
  -e playback_proxy_hostname=192.168.1.105 \
  -e playback_proxy_allowed_cidr=192.168.1.0/24 \
  -e playback_proxy_start=true
```

The play still defaults to `playback_proxy_start=false`. The production
activation above uses `true` only after the agent is healthy and the user's
trusted-LAN/no-host-firewall choice is recorded. Caddy's `/v1/start` request
returned 404; only the read API is proxied. The proxy rollout did not restart
the agent or any MPD, PipeWire, Russound, or serial-gateway service.

For the web container, create a private `docker/secrets/musicpi-playback-token`
file containing the same bearer token provisioned to the agent. The compose
mount exposes this directory read-only at `/run/secrets`. Set `RUSSOUND_UID`
and `RUSSOUND_GID` so the web process can read the token without making it
world-readable, and configure `RUSSOUND_PLAYBACK_AGENTS` in the untracked local
`.env` file:

```dotenv
RUSSOUND_PLAYBACK_AGENTS=[{"agent_id":"musicpi","url":"https://192.168.1.105:8443","token_file":"/run/secrets/musicpi-playback-token","tls_ca_file":"/run/secrets/musicpi-proxy.crt"}]
```

Do not commit `.env`, the bearer token, the Caddy private key, or any generated
operational configuration. A local Compose configuration check is not proof
of Pi reachability, firewall enforcement, or agent health.

Default locations:

| Resource | Location relative to the audio user's home |
| --- | --- |
| Agent entry point/helpers | `.local/share/mpdrussoundpi-agent/` |
| Isolated agent venv | `.local/share/mpdrussoundpi-agent/venv/` |
| Private token | `.config/mpdrussoundpi-agent/token` |
| User unit | `.config/systemd/user/mpdrussoundpi-playback-agent.service` |
| Existing config, read only | `.config/mpdrussoundpi/config.json` |

Override `playback_agent_id`, `playback_agent_port`, `playback_agent_config`
or installation path variables as necessary. Paths must be absolute and
systemd-safe (no spaces, traversal or `%` specifiers). The fixed listener is
**127.0.0.1:8081** by default and must stay loopback-only. The selected D06 design places Caddy on the Pi as a TLS-terminating reverse
proxy in front of this listener. Docker connects to Caddy using a self-signed
certificate trusted via the web agent's `tls_ca_file`; hostname validation
remains enabled, so the Docker-reachable DNS name must match the certificate
subject alternative name. Caddy forwards the bearer token and only the read API.
Firewall proxy ingress to the Docker host. Do not configure
agent remote binding, expose an unprotected HTTP proxy, or treat this as
browser access protection (D07). Proxy software/configuration, certificate
provisioning and firewall rules are not configured by this agent play. The
certificate may be mounted read-only at `/run/secrets/musicpi-proxy.crt`;
the private key must remain on the Pi. Keep both files out of version control.

The installation validates config without MPD/audio commands, preserves token
privacy in task output and enables lingering without restarting the user manager.
It reloads user unit definitions only. On explicit activation it enables/starts
only the agent, restarting it if its sources, unit, token or dependencies changed.
It checks authenticated health/identity and inventory, plus unauthenticated
401 rejection on the Pi's loopback interface.
Inventory HTTP 200 can contain failed or pending probes; these checks do not
prove successful MPD reads, fresh metadata or correct audio routes.

Install-only reruns do not stop/restart an already running agent, but can update
its files/token/unit. Use explicit activation to load those changes. Token
rotation is an explicit operator replacement of the private source file followed
by activation; tokens are never silently regenerated.
Check mode is incomplete for a new install: dependency installation, runtime
config validation, daemon reload and HTTP checks are skipped. Do not treat
`--check` as proof of target readiness; use syntax/static tests and subsequent
approved target acceptance.

The [user service template](../ansible/templates/playback-agent.service.j2)
has no `Wants`/`Requires` dependency on MPD/audio services. It supplies the
existing D-Bus/Pulse environment, a private umask and bounded on-failure
restarts. It creates no playback ledger/state files; future write support needs
its own reviewed writable-state provisioning. SIGTERM shutdown is allowed
90 seconds to finish bounded reads before systemd terminates its control group.
Journal output uses the agent's sanitized observation/status logging.

## Rollback later

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/remove-playback-agent.yml
```

[remove-playback-agent.yml](../ansible/playbooks/remove-playback-agent.yml)
stops/disables only the agent, removes only its unit and reloads user definitions.
It preserves code, venv, private token, lingering, operational config/intentions
and all MPD/audio services. Keeping files allows deliberate reinstallation;
credential deletion/revocation is a separate operator action. It requires the
same existing audio user manager; it is not a general Pi teardown.

## Preparation validation

[Static deployment tests](../tests/test_playback_agent_deployment.py) parse
the playbooks and render the unit, checking opt-in activation, agent-only service
operations, isolated dependencies, private credentials, loopback binding,
non-mutating config validation and limited rollback. They never execute Ansible
tasks or target commands. Ansible `--syntax-check` is controller-only.
Preparation validation passed: six static tests and syntax checks for both
playbooks. No target execution or systemd acceptance is claimed.

## Approved isolated target test (2026-10-15)

The user explicitly approved deploying during the endurance run using a
separate private config copy. The agent-only play completed with no failures
and started `mpdrussoundpi-playback-agent.service` at 01:02:29 CEST. The service
is enabled and remains running. No full Pi setup or rollback play was executed.

- Installed the isolated venv and current committed agent sources; existing
  playback checkout/venv were not updated.
- Copied the current operational config to
  `~/.config/mpdrussoundpi-agent/observation-test-config.json`, adding test
  IDs `front`, `centerlfe`, `rear` and test labels only in that copy.
  This is not the S04 production migration. The copy does not follow edits
  to the original config; use the approved operational config after migration.
- Generated a private random service credential outside the checkout; target
  token/config are mode 0600 under a mode-0700 directory. No credential values
  were logged or committed.
- At the time of this test, the actual listener was **127.0.0.1:8081**, not a
  LAN listener; the D06 reverse proxy and Docker/web integration were still
  unimplemented. SSH carried the test commands.
- Verified authenticated health/capabilities/inventory, per-output HEAD,
  unknown-output 404, unauthenticated 401 and unsupported POST 405.
  `write_operations` is empty.
- Three cached HTTP reads measured approximately 41.35, 3.19 and 6.44 ms on
  the Pi. These are a small smoke sample, not p95 LAN or metadata-update latency.
- At the successful verification around 02:13 CEST all streams were playing,
  with one queue entry, fresh title metadata, ready audio and correct routes.
  Measured stereo gain units were `[49905, 49905]`, `[65536, 65536]` and
  `[64044, 64044]`, matching -7.1, 0 and -0.6 dB respectively.
- Before/after SHA-256 checks confirmed byte-unchanged production config and
  intentions. PIDs, start times and restart counts were unchanged for all
  three MPDs, supervisor, recorder, PipeWire, PipeWire-Pulse and WirePlumber.
  The recorder remained sampling and its summary timestamp advanced.
- Radio 88.6's pre-existing faults continued: state problem samples rose
  from 9 to 17, route measurement problems from 11 to 17 and suspected-silence
  samples from 22 to 34 across the deployment/verification interval.
  FM4 counters remained 10 state/4 route; OE3 remained unflagged.
  This proves neither uninterrupted playback nor absence of polling/load
  effects. No playback repairs or fault injection were performed.
- One verification SSH connection reset; the read-only check was retried with
  keepalive and completed. No service restart was used to recover the test.
- At 02:14:50 CEST the agent still had zero restarts after approximately
  72 minutes, about 35 MiB resident memory and lifetime average CPU of 0.7%
  (one `ps` sample, not a load benchmark). Recorder and supervisor remained
  active with zero restarts; the recorder was still sampling before its deadline.

Private baseline evidence is retained on the Pi at
`~/.config/mpdrussoundpi-agent/deployment-baseline.json`. The separate config
and service remained in place for read-only tests. At this historical
checkpoint, full S06 acceptance still required production IDs, the D06 proxy
and Docker reachability; those were addressed in the production rollout below.

## USB maintenance update (2026-10-16)

The user approved the coordinated USB recovery maintenance deployment described
in [the recovery record](./usb-audio-recovery-fix.md#approved-maintenance-deployment-2026-10-16).
The separate observation-test config now explicitly includes the same USB
model selector as the operational config; its test IDs/labels and every other
field were preserved. Relevant observer helpers were updated together, and the
agent was intentionally stopped during replacement and resumed at 20:46:03 CEST.

Post-maintenance authenticated inventory is fresh and reports playing streams,
ready downstream audio, correct routes and expected gains for all three outputs.
Unauthenticated inventory remains 401, write capabilities remain empty, and the
listener remains loopback-only. At this checkpoint production IDs and D06/web transport were still
outstanding; they were addressed in the production rollout below. Physical
USB-reconnect acceptance remains outstanding. The original Oct 15
uninterrupted agent-lifetime sample is historical; the Oct 16 agent restart
was an explicit maintenance event.

## Production read-only rollout (2026-10-17)

After explicit approval, prepared a private offline S04 bundle from exact
copies of the production config and intentions. Applied only these identities:

| Legacy stream name | Stable ID | Display label |
| --- | --- | --- |
| `Radio 88.6` | `radio_886` | `Radio 88.6` |
| `FM4 Radio` | `fm4_radio` | `FM4 Radio` |
| `OE3 Radio` | `oe3_radio` | `OE3 Radio` |

The migration tool verified all non-identity stream fields were unchanged.
The original config SHA-256 was
`965b6a7819465d818695d61b1c0dddc10e10c3c6882467388503fb7d637bea4b`; the
prepared config SHA-256 is
`74d75070d1473ee962a7145be483420112b9b255d87048353fded71175639c96`. The
intentions file stayed byte-identical at SHA-256
`e8ecf227527c83470812de2757e96ecb0355ce8b79d717fb900afde730740875`.
The protected rollback bundle is retained under
`~/.local/state/mpdrussoundpi/s04-production-migration/` on the Pi and in the
session's private state directory. The playback supervisor was stopped only
for the atomic config replacement and then restored. All three MPD unit PIDs
remained `65286`, `65283` and `65284`; statuses remained `play`, configured
gains remained `-7.1`, `0` and `-0.6` dB, and all routes remained correct.
The read-only agent was aligned to the prepared production config and
restarted; no MPD, PipeWire, Russound or serial-gateway service was restarted.

Caddy 2.6.2 is installed. The package-provided `caddy.service` is disabled and
inactive; the dedicated `mpdrussoundpi-playback-agent-proxy.service` is enabled
and active. Nginx still owns port 80; the agent remains on `127.0.0.1:8081`,
Caddy's admin API is on `127.0.0.1:2020`, and Caddy serves TLS on port 8443.
The self-signed certificate's IP SAN, trust anchor, key match and validity
passed the playbook checks. Only the public certificate is in the ignored
`docker/secrets/musicpi-proxy.crt`; the private key remains on the Pi.

The initial source restriction used `192.168.1.56/32`, but this was the Mac's
Wi-Fi address, not the source used by Docker Desktop. During error
investigation on 2026-10-17, a temporary Pi-side TCP probe confirmed the
Docker-originated connection source as `192.168.1.142`, the Mac's Ethernet
interface (`en5`). The web container's authenticated requests consequently
received HTTP 403 from Caddy. After the user's explicit approval, the proxy
matcher was updated to `192.168.1.0/24` and the dedicated proxy was restarted;
no agent, MPD, audio, or Russound service was restarted. This is still only an
application-level source filter, not host firewall enforcement. The deployment
assumes a trusted LAN and no router port forwarding.

The local `russound-web` container was rebuilt/recreated with the private bearer
token and pinned certificate mounted read-only at `/run/secrets`; its health
check is healthy. During the initial `/32` rollout, authenticated TLS health
returned 200, unauthenticated health returned 401, and a request from the Pi's
then non-allowed source returned 403. `/v1/start` returned 404. The existing
Russound hardware warning on the local dashboard is independent of this
playback integration.

After the allowlist correction, authenticated Docker-to-Pi TLS health,
capability and output reads returned HTTP 200; the web cache reported
`musicpi: ok` and the local playback page displayed all three output cards.
The web integration is read-only: no per-output playback, source or gain
controls are exposed.

## S06 LAN latency acceptance (2026-10-17)

A 156-second sample ran on the Raspberry Pi (`musicpi`) against the Docker
host's LAN address, `http://192.168.1.56:8000/api/playback`, with a 500 ms
delay between API fetches. It completed 240 fetches and collected 720 healthy
per-output observations (three outputs per fetch), with zero stale or error
observations.
All three outputs reported 64 metadata observation changes; their measured
change intervals were p50 2.458 s, p95 2.971 s and max 3.394 s.

The conservative web-visible age estimate was p50 2.978 s, p95 4.819 s and
max 5.806 s. It adds each agent-reported playback age to elapsed time since
the web cache's `last_success_at` and the HTTP request duration. The cache
timestamp is second-precision, so the estimate can overstate age by almost a
second. The proposed healthy-LAN p95 target of 5 seconds is met; the maximum
is an observed outlier above that target, not a p95 failure. This is a
Pi-to-web-host LAN-path sample, not an independent phone/desktop browser
measurement. An earlier loopback sample was provisional and is not used for
acceptance.

Immediately after this sample, the agent, proxy, supervisor and all three MPD
units were active. The MPD process IDs remained 65283 (FM4), 65284 (OE3) and
65286 (Radio 88.6), unchanged by the agent deployment. No MPD/audio,
Russound or serial-gateway service was restarted for this acceptance.

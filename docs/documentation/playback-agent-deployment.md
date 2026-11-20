# Playback deployment

This guide installs or updates the current Pi playback stack and connects it
to the web dashboard. Run Ansible commands from the repository root on the
deployment controller. Replace example addresses, paths and DNS resolver IPs
with your installation values.

For a web-only start, see the [README](../../README.md#quick-start-local-development).
For MPD hardware/configuration details, see
[headless playback](./mpd-headless-deployment.md).
Historical checks and outstanding acceptance are in the
[implementation plan](../plans/general_done_mpd-web-integration.md#6-deployment-acceptance), not in the procedure.

## Components and prerequisites

| Component | Host / listener | Purpose |
| --- | --- | --- |
| MPD, PipeWire, supervisor | Pi; MPD `127.0.0.1:6601-6603` in the seed | Decode, route and recover audio |
| Playback agent | Pi; `127.0.0.1:8081` | Authenticated observations and optional durable commands |
| Caddy proxy | Pi; TLS `8443` | Restricted web-to-agent access |
| Web application | Docker host or native Linux host; HTTP `8000` | Browser UI and same-origin gateway |
| Egress filter | Pi; no listener | Block non-public MPD stream destinations |

Required:

- Linux Pi with systemd user services/cgroup v2, a supported USB audio device,
  network access and a non-root audio account (`musicpi` in the inventory).
- Ansible on the controller, SSH access and sudo on the Pi. Review
  [inventory.yaml](../../ansible/inventory.yaml) before running any play.
- Matching source revisions on the controller and Pi checkout. Agent/proxy/
  egress plays copy files from the controller checkout; audio setup normally
  pulls Git. Commit/publish reviewed code before remote Git deployment.
  Never force/reset a dirty Pi checkout.
- On the Pi, preinstall `caddy`, `openssl` and `nftables` through your normal
  package-management procedure. The proxy requires the `caddy` account and
  `/usr/bin/caddy`; the filter requires `/usr/sbin/nft` with cgroup-v2 matching.
- A trusted LAN. The browser UI has no login. Caddy restricts HTTP source CIDRs,
  not incoming packets. Do not expose either listener by router port forwarding.

Capture and independently verify an off-host backup before any approved
activation. Stop writers before copying the operation SQLite database, or use
SQLite's backup API. The audio setup playbook now stages only; native activation
is blocked pending the shared reservation/recovery workflow. Component
defaults are role defaults, so inventory values override them.

## 1. Prepare the Pi audio stack

Review the seed's USB hardware selector, profile, sinks, output IDs/ports,
station defaults and gains before any future activation. Candidate staging
does not create or alter `~/.config/mpdrussoundpi/config.json`; on an existing
installation, that persistent file is only read for validation.

Stage an immutable audio candidate without changing the active services:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/setup-musicpi.yml --tags stage \
  -e mpd_russoundpi_revision=<full-40-or-64-character-commit>
```

The play creates a new revision-specific source checkout and virtual
environment, installs only the bounded audio dependency manifest, and
validates the existing operational config when present. It does not modify the
active checkout, gateway, native units, audio configuration, or playback
state. An already-existing candidate path is refused instead of overwritten.
Native activation, including backup, reservation, restart, recovery and health
verification, is not implemented by this playbook yet. Do not use legacy
`--tags init,new` as a substitute.

## 2. Install public-only stream enforcement

Inspect the Pi's DNS resolver addresses (`cat /etc/resolv.conf`). Supply exact
resolver IP literals, not names, as a typed JSON list as shown below. Do not
pass a single resolver using `-e playback_egress_resolvers=192.168.1.1`; the
shared Ansible input contract requires a nonempty typed list of IP literals.

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-egress.yml \
  -e playback_egress_activate=true \
  -e '{"playback_egress_resolvers":["192.168.1.1"]}'
```

The play verifies exact configured MPD output IDs against managed unit
markers, effective systemd fragment/drop-in paths and selected config files
before mutation. Legacy, overridden or colliding units are refused for
separate inspection/migration; stale or unrelated units are reported and left
unchanged. It installs root-owned helpers, `mpdrussoundpi-egress.service`, the
user `mpdstreams.slice` and per-output `50-egress.conf` drop-ins, then restarts
only configured outputs. It leaves existing host firewall tables unchanged.
UDP and TCP DNS are excepted only to supplied resolver IPs on port 53; HTTP
streams to those IPs remain blocked.

MPD startup fails if the root service is unavailable or its recorded cgroup
inode no longer matches. Restart the root service after recreating the slice.
Stopping the root service does not remove its loaded rules. Private/LAN URL
stations are deliberately unsupported. See the
[egress policy](../plans/mpd_done_playback-egress-hardening.md) for details.

On the Pi, initialize/reconcile once, then enable supervision:

```sh
AUDIO_BIN=$(dirname "$(systemctl --user show -p ExecStart --value mpd_playback_supervisor.service | sed -n 's/.*path=\([^ ;]*\).*/\1/p')")
"$AUDIO_BIN/mpd-supervisor" --once ~/.config/mpdrussoundpi/config.json
systemctl --user enable --now mpd_meta_control.service mpd_playback_supervisor.service
```

Do not start supervision after a failed reconciliation; inspect diagnostics.
`AUDIO_BIN` is the `bin` directory of the active audio candidate
(`~/.local/share/mpdrussoundpi/audio-candidates/<revision>/venv/bin`), read
from the supervisor unit so it always matches the running revision. The
commands are console scripts of the installed `mpdrussoundpi` package.


## 3. Stage an immutable agent candidate

The agent playbook currently stages only. It clones the exact selected commit
into a new private candidate directory and installs only the bounded agent
dependencies into a separate virtual environment. It refuses to overwrite an
existing candidate; inspect a partial candidate and select a fresh revision
path rather than rerunning over it.

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  --tags stage \
  -e playback_agent_revision=FULL_40_OR_64_CHARACTER_COMMIT_SHA
```

Candidate staging may access Git and package indexes from the Pi. If an
operational MPD configuration exists, candidate code parses it without
contacting MPD or audio. Staging does not read or install credentials, alter
the active agent source or service, enable lingering, start or restart any
service, or activate the candidate. Active deployment and credential rotation
remain blocked until the reviewed native transaction, verified recovery
backup and health checks are implemented. The saved token, if one already
exists, is not touched.

Manual-operation and TuneIn settings apply only when activation is implemented.
The installed agent and TLS proxy remain separate services; do not use proxy
activation as evidence that a staged agent candidate is active.

## 4. Activate the TLS proxy

Choose a DNS name or IPv4 address reachable from the web host and a source CIDR
that matches traffic as the Pi actually sees it (Docker/NAT may change it).
Prefer an exact host `/32`; broaden only intentionally.
When the web container runs on the Pi, also allow its Docker network's actual
source CIDR using `playback_proxy_additional_allowed_cidrs`. Keep the existing
allowed CIDR during migration if the previous web host still needs access.

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent-proxy.yml \
  -e playback_proxy_hostname=192.168.1.105 \
  -e playback_proxy_allowed_cidr=192.168.1.142/32 \
  -e playback_proxy_enable_operations=true \
  -e playback_proxy_start=true
```

Omit `playback_proxy_enable_operations=true` for read-only transport. Both agent
and proxy must allow commands for dashboard controls to work.

The dedicated service does not change the package's `caddy.service`. It creates
a ten-year (3650-day) self-signed certificate with a matching SAN on first install,
validates the pair and fetches only the public certificate to
`docker/secrets/musicpi-proxy.crt` on the controller. The private key stays on
the Pi. Existing certificates must match the host and have at least 30 days'
validity; the play does not automatically renew/replace them.

Reads cover health, capabilities, outputs, favorites and operation lookup.
Writes are limited to output/favorites operations. All agent requests require
the bearer token; Caddy's source filter does not replace authentication.

## 5. Connect the web application

For Docker, copy the token into `docker/secrets/musicpi-playback-token` on the
web host; copy the fetched public certificate there if the web host is separate
from the deployment controller. Ensure the container's configured UID/GID can
traverse the directory and read the token without making it world-readable.

When the controller checkout is also the web host checkout:

```sh
install -d -m 700 docker/secrets
install -m 600 "$HOME/.config/mpdrussoundpi-deploy/musicpi-token" docker/secrets/musicpi-playback-token
export RUSSOUND_UID="$(id -u)"
export RUSSOUND_GID="$(id -g)"
```

Persist the resulting numeric `RUSSOUND_UID`/`RUSSOUND_GID` values in the local
`.env` for later Compose runs. In an Ansible-managed Docker deployment, the play
sets them to the deployment user's IDs.

For a manually managed Docker deployment, add this single line to the web
host's private `.env`:

```dotenv
RUSSOUND_PLAYBACK_AGENTS=[{"agent_id":"musicpi","url":"https://192.168.1.105:8443","token_file":"/run/secrets/musicpi-playback-token","tls_ca_file":"/run/secrets/musicpi-proxy.crt"}]
```

The URL host must match the certificate SAN. Never disable TLS verification.
Changing Docker projects may change the web container's source subnet. Verify
the proxy's explicit allowed CIDRs against the new network before cutover;
HTTP 403 can indicate source authorization failure, not a bad token or CA.
Back up and validate proxy changes before applying them. Existing installations
whose proxy unit lacks `ExecReload` can use Caddy's direct reload command with
the configured admin endpoint `127.0.0.1:2020`; do not assume `systemctl reload`
is supported until the unit has been updated.
For an Ansible-managed Docker deployment, supply the agent list through
`russound_playback_agents` instead: the play rewrites `.env` on rerun.
For a manually launched native web process, set `RUSSOUND_PLAYBACK_AGENTS`
with absolute local token/cert paths. For the Ansible-managed native systemd
service, use the `russound_playback_agents` Ansible variable: its generated
unit sets this environment value explicitly, so changing a shell environment
does not update the service.
`.env`, private tokens, runtime config/state and TLS private keys stay outside Git.

For a new isolated container installation, follow the
[container guide](container-deployment.md) and use only its external-gateway
web profile. For an existing installation, retain its actual ordered Compose
files and image selection; do not rebuild or switch layouts simply to add an
agent. The new Ansible preview does not provision playback credentials.

Open `http://<web-host>:8000/playback`. A stream's Config dialog contains
transport controls, station favorites/search and calibration gain. Favorites
are capped at 20. Selecting a station starts playback. With no station, Play
is visible but disabled.

## Updating an existing installation

1. Back up, review source changes and schedule interruption if audio changes.
2. For agent-only changes, repeat step 3 with the existing token. Proxy-only
   changes use step 4. Neither play updates the Pi checkout/supervisor.
3. For shared backend/audio changes, stop the agent during the update; rerun
   step 1 to update the Pi checkout and regenerate units while stopped.
   For an explicitly staged clean source tree, Git fetching can be skipped with
   `-e mpd_russoundpi_update_repo=false`. Rerun step 2 after adding outputs,
   changing resolver IPs or changing the shared destination policy.
4. Restart supervision only after successful verification; update/reactivate
   the agent from the matching controller revision, then recreate web as needed.
5. Check the gates below. No deployment command is evidence of successful audio
   or persistence by itself.

New outputs must receive the egress drop-in before they start. Regenerating base
units preserves existing drop-ins; staging without playback avoids starting
new unprotected instances.

## Health checks and troubleshooting

On the Pi:

```sh
systemctl --user status mpd_playback_supervisor mpdrussoundpi-playback-agent --no-pager
systemctl status mpdrussoundpi-egress mpdrussoundpi-playback-agent-proxy --no-pager
ss -ltn | grep -E ':(660[1-3]|8081|8443)\b'
journalctl --user -u mpdrussoundpi-playback-agent -u mpd_playback_supervisor -n 60 --no-pager
sudo nft list table inet mpdrussoundpi_egress
```

MPD/agent must bind loopback only. The proxy is the network-facing listener.
From the web host, `GET /api/playback` must report the agent `ok`, fresh playback
and correct routes/gains. The HTTP/container healthcheck checks web availability,
not audio or Russound readiness.

### Bounded USB profile recovery

Selector-based readiness requires matching native PipeWire/Pulse profiles as
well as the configured sink/channel graph. Read-only agent observations never
change the profile or restart audio services.

Enforcing startup checks share a per-user kernel lock and recovery record at
`$XDG_RUNTIME_DIR/mpdrussoundpi-audio-profile.lock`; the runtime directory must
be absolute, owned by the audio user and mode `0700`. After a five-second grace:

- A persistently reported `off` profile with an otherwise valid 5.1 graph can
  receive one off/on reset per device/PipeWire-server generation.
- Persistently missing profile inventories can request one nonblocking
  `wireplumber.service` user-service restart per boot, across all guarded cards.
  This approved fallback affects the host session, not only one USB device.

Both recoveries require fresh unique device identities and refuse running or
unknown-state audio. Conflicting native Profile entries are not resolved by
picking one. Budgets are persisted before mutation, including when a command
fails; service retries or WirePlumber restarts do not replenish the boot-scoped
budget. A subsequent complete readiness check is required before startup
succeeds. Inspect the audio unit journal for the recovery reason or exhausted
budget; do not delete the lock/state file to force another attempt.

The deployed recovery is resilience for observed audio-stack publication/
restoration failures, not an upstream PipeWire/WirePlumber patch. See the
[acceptance record](../plans/general_done_simplify-deployment-configuration.md#approved-session-recovery-and-installed-reboot-acceptance)
for real-fault, explicitly injected-fault and unattended reboot evidence.

| Symptom | Check |
| --- | --- |
| TLS error | SAN matches URL, pinned cert mounted/readable, certificate not expired |
| Proxy 403 | Actual source IP belongs to configured Caddy CIDR |
| Agent 401 | Same private token installed on Pi and readable by web |
| Controls unavailable | Agent and proxy operation opt-ins; agent capabilities |
| MPD refuses startup | Root filter active; cgroup inode matches; exact resolver IPs |
| Station unavailable | Public DNS/redirect policy, provider availability, MPD journal |
| Russound warning but playback healthy | Serial/TCP gateway and physical controller; independent of MPD |

Default Pi paths:

| Resource | Path under the audio user's home |
| --- | --- |
| Admin config | `.config/mpdrussoundpi/config.json` |
| Runtime state | `.local/state/mpdrussoundpi/playback.json` |
| Agent sources/venv | `.local/share/mpdrussoundpi-agent/` |
| Token | `.config/mpdrussoundpi-agent/token` |
| Operations ledger | `.local/state/mpdrussoundpi-agent/operations.sqlite3` |
| User units / MPD configs | `.config/systemd/user/` / `.config/mpd/` |

## Removal and rollback

To retire only the API while preserving local playback:

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/remove-playback-agent.yml
sudo systemctl disable --now mpdrussoundpi-playback-agent-proxy
```

The first command runs on the controller; the second on the Pi. Agent removal
keeps code, credentials, configuration, runtime state and local supervision.
It does not stop persisted playback intentions.

For an audio/schema rollback, stop the supervisor, agent and MPD writers first,
then restore matching code/config/state/units from a private backup. Never
overwrite newer station/favorite/gain edits without explicitly choosing to
restore them. Keep egress protection unless deliberately reverting that policy.
Its removal procedure is in the [egress guide](../plans/mpd_done_playback-egress-hardening.md#deployment-and-rollback).

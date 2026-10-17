# Playback deployment

This guide installs or updates the current Pi playback stack and connects it
to the web dashboard. Run Ansible commands from the repository root on the
deployment controller. Replace example addresses, paths and DNS resolver IPs
with your installation values.

For a web-only start, see the [README](../README.md#quick-start-local-development).
For MPD hardware/configuration details, see
[headless playback](./mpd-headless-deployment.md).
Historical checks and outstanding acceptance are in
[deployment evidence](./playback-deployment-evidence.md), not in the procedure.

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
  [inventory.yaml](../ansible/inventory.yaml) before running any play.
- Matching source revisions on the controller and Pi checkout. Agent/proxy/
  egress plays copy files from the controller checkout; audio setup normally
  pulls Git. Commit/publish reviewed code before remote Git deployment.
  Never force/reset a dirty Pi checkout.
- On the Pi, preinstall `caddy`, `openssl` and `nftables` through your normal
  package-management procedure. The proxy requires the `caddy` account and
  `/usr/bin/caddy`; the filter requires `/usr/sbin/nft` with cgroup-v2 matching.
- A trusted LAN. The browser UI has no login. Caddy restricts HTTP source CIDRs,
  not incoming packets. Do not expose either listener by router port forwarding.

Capture a private backup before changing a running installation: admin config,
runtime state, user/system units, source revision and agent credentials. Stop
writers before copying the operation SQLite database, or use SQLite's backup
API. Full audio deployment and egress activation interrupt playback.

## 1. Prepare the Pi audio stack

Review the seed's USB hardware selector, profile, sinks, output IDs/ports,
station defaults and gains first. The first install seeds
`~/.config/mpdrussoundpi/config.json`; later runs preserve it. Edit that
persistent file for an existing installation.

Stage the audio/MPD units without starting playback:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/setup-musicpi.yml --tags init,new \
  -e mpd_russoundpi_start_playback=false
```

This prepares the user manager, audio services, native ser2net, MPD units and
runtime defaults. It leaves MPD/supervision stopped and their meta/supervisor
autostart disabled so egress can be installed
before streams start. If audio-group membership was newly added to an already
running user manager, reboot in a maintenance window before continuing.

Runtime defaults: an output with `default_station` initially intends to play;
without it, it stays silent/stopped. Missing `default_gain_db` means 0 dB.
Existing runtime station/gain/intention/favorites are preserved. Do not delete
`playback.json` during an update.

## 2. Install public-only stream enforcement

Inspect the Pi's DNS resolver addresses (`cat /etc/resolv.conf`). Supply exact
resolver IP literals, not names:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-egress.yml \
  -e playback_egress_activate=true \
  -e '{"playback_egress_resolvers":["192.168.1.1"]}'
```

The play installs root-owned helpers, `mpdrussoundpi-egress.service`, the user
`mpdstreams.slice` and per-MPD `50-egress.conf` drop-ins, then restarts configured
MPD instances into the protected slice. It leaves existing host firewall tables
unchanged. UDP DNS is excepted only to the supplied resolver IPs on port 53;
HTTP streams to those IPs remain blocked.

MPD startup fails if the root service is unavailable or its recorded cgroup
inode no longer matches. Restart the root service after recreating the slice.
Stopping the root service does not remove its loaded rules. Private/LAN URL
stations are deliberately unsupported. See the
[egress policy](./playback-egress-hardening-plan.md) for details.

On the Pi, initialize/reconcile once, then enable supervision:

```sh
cd ~/MPDRussoundPi/mpdrussoundpi/russound
~/russound-venv/bin/python mpdsupervisor.py --once ~/.config/mpdrussoundpi/config.json
systemctl --user enable --now mpd_meta_control.service mpd_playback_supervisor.service
```

Do not start supervision after a failed reconciliation; inspect diagnostics.

## 3. Create a service token and activate the agent

Generate the token once on the controller, outside Git, in a private directory.
Use the same file when redeploying; replacing it is credential rotation.

```sh
install -d -m 700 "$HOME/.config/mpdrussoundpi-deploy"
(umask 077; set -C; openssl rand -hex 32 > "$HOME/.config/mpdrussoundpi-deploy/musicpi-token")
```

Do not rerun generation over an existing token. Keep its contents out of shell
arguments, inventory, logs and browser code. Activate:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  -e playback_agent_token_source="$HOME/.config/mpdrussoundpi-deploy/musicpi-token" \
  -e playback_agent_enable_operations=true \
  -e playback_agent_start=true
```

Omit `playback_agent_enable_operations=true` for a read-only agent. TuneIn
selection defaults on with manual operations; use
`playback_agent_enable_tunein_selection=false` to disable new TuneIn selections.
The agent-only play does not regenerate or restart MPD/audio and requires an
existing user manager. Without `playback_agent_start=true`, it installs files
but does not activate/reload a running process.

## 4. Activate the TLS proxy

Choose a DNS name or IPv4 address reachable from the web host and a source CIDR
that matches traffic as the Pi actually sees it (Docker/NAT may change it).
Prefer an exact host `/32`; broaden only intentionally.

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

Add this single line to the web host's private `.env`:

```dotenv
RUSSOUND_PLAYBACK_AGENTS=[{"agent_id":"musicpi","url":"https://192.168.1.105:8443","token_file":"/run/secrets/musicpi-playback-token","tls_ca_file":"/run/secrets/musicpi-proxy.crt"}]
```

The URL host must match the certificate SAN. Never disable TLS verification.
For native web, set `RUSSOUND_PLAYBACK_AGENTS` with absolute local token/cert
paths, or use the `russound_playback_agents` Ansible variable.
`.env`, private tokens, runtime config/state and TLS private keys stay outside Git.

Rebuild/recreate only the web service when using an existing external gateway:

```sh
docker compose build russound-web
docker compose up -d --no-deps --no-build russound-web
```

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
Its removal procedure is in the [egress guide](./playback-egress-hardening-plan.md#deployment-and-rollback).

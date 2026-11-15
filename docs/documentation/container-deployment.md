# Container deployment

This is the authoritative guide for the new container layouts. Use them for
**new, isolated development deployments** or the explicitly reviewed
[operator workflow](#reviewed-development-deployment-and-recovery) below.
That workflow supports prepared Linux web/external-gateway, web/managed-gateway
and standalone gateway layouts, verified local images or pinned Hub candidates, same-layout adoption,
ordinary pinned reruns, explicit updates, off-host recovery and approved
restoration. It does not silently migrate legacy layouts, native services,
networks, credentials or application schemas. Existing Ansible preview plays
remain preview/preflight only; do not bypass their apply gate.
An explicitly approved manual Pi trial reached real controller communication,
then required a separately approved agent proxy allowlist change after the
Docker subnet changed. Repeated checks passed after that correction; see the
[target-check record](../plans/general_open_simplify-deployment-configuration.md#early-raspberry-pi-target-check-2026-11-13).
That trial is not a validated migration procedure.

## What is available

| Choice | Ordered Compose files | Images |
|---|---|---|
| Development, external gateway | `compose.dev.yml` | Published web digest |
| Development, managed USB gateway | `compose.dev.yml`, `compose.gateway.yml` | Matching published web/gateway digests |
| Development, separate prepared backend host | `compose.backend.yml` on backend; `compose.dev.yml` on web host | Gateway-only and web-only pins; see [split-host deployment](#split-host-deployment-on-prepared-hosts) |
| Development, no hardware | `compose.dev.yml`, `compose.dummy.yml` | Published web digest and explicitly built local dummy |
| Development, explicit local-build preview | Same development files for the selected gateway | Verified controller-built image archives; no target transfer/load/apply |
| Production | `compose.prod.yml`, optional `compose.gateway.yml` | Layout only; blocked until approved stable images and deployment tooling exist |

Include **one gateway overlay at most**. External mode has no gateway service,
device requirement or hidden dependency. The same-host gateway and dummy are internal
to the project network: port 6666 is not published. Only the explicit standalone
backend layout publishes its gateway on a reviewed host address/port. Web defaults to host port
8000. Different projects have separate networks and generated container names;
they must also have separate data directories and non-conflicting host ports.

None of these files contains `build`. Starting containers never implicitly
builds an application image. The development profile enables debug logging;
the production layout disables it. Every container rotates Docker logs at
10 MiB, three files. Updated runtime source implements the following logging
and configuration contract; older pinned images do not acquire it on redeploy.

### Runtime configuration, logs and dependencies

For settings with multiple sources, explicit CLI options win over environment,
then saved configuration, then built-in defaults. Ansible variable precedence
and Compose interpolation happen **before** the process starts; they do not
add runtime sources. The editor continues to save file settings, not deployment
overrides. Its effective backend summary and connectivity status consume shared
[API metadata](../../web/API.md#get-apiconfig).

Optional blank/whitespace environment values are unset. Nonblank invalid
booleans/integers are startup errors, without echoing their values. Boolean
values accept `true/false`, `yes/no`, `on/off` and `1/0` (case-insensitive).
Environment integers are nonnegative ASCII decimal strings of at most nine
digits; listener/backend ports allow at most five digits and require
`1..65535`. Worker threads must exceed the four reserved request workers;
an explicit event-stream limit cannot exceed the remaining workers.
`RUSSOUND_MAX_EVENT_STREAMS=0` selects automatic sizing;
`RUSSOUND_TCP_USER_TIMEOUT_SECONDS=0` disables that socket timeout.
Its maximum is 2147483 seconds so conversion to milliseconds fits the socket
API's signed integer without overflowing on the first client connection.
`--debug` / `--no-debug`, `--port` and `--waitress-threads` override even a
malformed corresponding environment value. Invalid nonblank backend ports
now fail instead of being ignored. Missing/invalid saved application config
still opens the existing setup UI and never triggers an automatic rewrite.

| Component | Destination and level |
|---|---|
| Web | stdout plus optional `RUSSOUND_LOG_FILE`; DEBUG in development, WARNING/errors plus essential health/security events in production. Files rotate at 10 MiB plus five backups. Waitress serves both modes; no Flask debugger. |
| Native playback agent / supervisor | stderr, captured by the user-service journal; same production minimum, with explicit `--debug` for development. No implicit log files. |
| Interactive audio/MPD setup commands | stderr at INFO for requested setup actions, DEBUG with `--debug`; imports/`--help` do not configure logging. |
| Protocol-frame audit | Separate explicit opt-in via saved config; 5 MiB plus three backups. Ordinary reruns preserve that choice. Disabling an existing audit requires the separately approved, backed-up migration. |
| Docker services / existing native daemons | Docker stdout/stderr uses the bounded driver above. Native daemon/journal settings remain host-managed; no global retention change. |

Web and native Python services redact HTTP(S) URLs, Bearer values and named
credential/session/path assignments in their formatters, including exception
text and quoted dictionaries/JSON. This is defense in depth, not permission to
log arbitrary secrets. Do not put credentials in command arguments, debug
messages or audit frames.

Dependencies use separate bounded manifests: web Flask `>=3.1,<4` and Waitress
`>=3.0.2,<4`; native agent adds python-mpd2 `>=3.1,<4`; audio-only uses just that
python-mpd2 range. Deployment tooling reuses web requirements and bounds
ansible-core to `>=2.19,<2.22`. Ordinary installs do not request package upgrades.
Fresh installations can resolve different compatible releases; these are not
locks or certification of every possible version/platform combination.
Security/dependency updates are explicit: review a changed bound or an update
within it, resolve/install in a disposable environment, run `pip check` and
the relevant regression/image tests, then use the reviewed deployment/update
workflow. Do not upgrade the live audio environment as a side effect of web
work. Prepare and validate a separate
[audio candidate environment](./mpd-headless-deployment.md#isolated-audio-dependencies)
and retain the working one until an approved switch.

### Runtime shutdown and native setup logging

Current web source handles SIGTERM/SIGINT, including as container PID 1.
It rejects new work, drains SSE/request workers and closes background polling
and gateway connections. The dev/prod layouts allow a 30-second web stop grace
period. Shutdown timeouts fail explicitly; always verify the actual writer
state before capturing data. This source fix is not present in older published
images merely because the Compose layout was updated. The approved Pi test
used older pinned images and still observed forced web termination and a
nonzero ser2net stop. A subsequent separately approved manual local-image
trial replaced that pair with ARM64 builds of revision `161853e` and verified
web exit 0 in 2.23 seconds with an open SSE connection. It preserved config,
secrets, the project network and native playback services; real controller/
agent routing and LAN HTTP passed. See the
[local-image trial record](../plans/general_open_simplify-deployment-configuration.md#approved-local-image-pi-trial-2026-11-13).
This does not enable automated apply or certify ser2net graceful shutdown.

Native `audiosetup.py` and `mpdsetup.py` now initialize logging only when
executed, at INFO by default or DEBUG with `--debug`, writing to stderr
(captured by the service journal when invoked from a unit). Imports and
`--help` do not configure root logging or create files. They no longer
implicitly create `russound_debugging.log`; an existing file is left untouched.
Capture command output explicitly if persistent diagnostics are needed.
This intentionally replaces the old import-order-dependent DEBUG/file
behavior, not journal retention or the separate protocol-audit policy.

## Select published development images

The controller can resolve a matching published development revision without
pulling images, building, contacting the target or using Docker credentials.
Use Python 3.10+ from the repository root. Create a private directory first:

```sh
install -d -m 700 "$HOME/.config/russound"
python3 -m tool.deployment_images resolve --gateway external --platform linux/arm64 \
  --output "$HOME/.config/russound/dev-candidate.json"
```

Choose `managed` instead of `external` to include ser2net; choose `dummy` for
web plus a separately built dummy. Use `linux/amd64` for an AMD64 target.
Resolution requires the moving `dev` aliases and exact `dev-<full-sha>` tags
to agree. It verifies source/version/revision labels, platform, and index,
platform-manifest and configuration digests. Incomplete publication, mismatches,
alias movement or registry failure stop resolution; there is no fallback.
Only metadata is downloaded, not image layers. This verifies registry metadata,
not a signature, release approval, layer availability or hardware behavior.

The output is a **candidate**, not a committed target deployment record.
New files are created with owner-only `0600` permissions, are never hard-linked,
and never overwrite an existing selection. Reuse also requires an owner-owned,
single-link `0600` regular file opened without following symlinks, under a
validated directory path.
Reuse that file on subsequent previews rather than resolving `dev` again:

```sh
python3 -m tool.deployment_images reuse --gateway external --platform linux/arm64 \
  --selection "$HOME/.config/russound/dev-candidate.json"
```

`reuse` validates schema, gateway and platform offline and preserves the exact
references and timestamps. It does not reverify registry metadata.
With the same options, replace `reuse` with `verify` to recheck the selected
digests online without consulting moving aliases; use `check` to compare a
fresh candidate against the saved **index digests**. Neither command changes
the selection or target. An approved update requires a new `resolve --output`
path. Automatic apply, committed target pins and transactional updates remain
unimplemented.

## Prepare local development images

The explicit [builder](../../tool/deployment_build.py) builds on the controller,
not the Pi. It uses a clean checkout at an explicit full commit; dirty tracked,
untracked or submodule state is refused without resetting anything. Ignored
files and Git internals never enter the committed-source snapshot. Source
symlinks/submodules are currently unsupported. Use a separate clean Git
worktree if the development checkout has edits.

Select a local Docker context with its context-bound Docker Buildx builder
(`docker` driver); remote endpoints/workers and `DOCKER_HOST`/`DOCKER_CONTEXT`
overrides are rejected. The build pins that context/builder rather than using
an ambient selected worker. Create a private output parent outside the checkout:

```sh
install -d -m 700 "$HOME/.local/share/russound/builds"
python3 -m tool.deployment_build build \
  --source /absolute/path/to/clean-checkout \
  --revision REPLACE_WITH_FULL_40_CHARACTER_COMMIT \
  --output-parent "$HOME/.local/share/russound/builds" \
  --gateway managed --platform linux/arm64 --allow-build-network
```

`managed` builds matching web/ser2net components from that one snapshot;
`external` and `dummy` build web only. Dummy runtime still requires its separate,
explicit local dummy image. `linux/amd64` is also supported. No registry push
or Hub fallback occurs. Builds load only their operation-unique tags into the
controller daemon and export Docker-save archives. They do not contact the target.

`--allow-build-network` explicitly permits package downloads in Dockerfile
`RUN` steps (needed for the current pip/apt installs). Without it, those steps
use `--network none`, but **base-image resolution can still access a registry**.
Base tags and package dependencies are not locked: a clean source revision
does not imply bit-reproducible or fully offline builds.

Successful JSON output contains `bundle` and `manifest_sha256`. Retain that
digest separately before copying; do not obtain the trusted digest from an
untrusted replacement bundle. Each new operation directory is mode `0700`,
its private `build.log` is `0600`, and archives/manifest are `0400`. Failed
builds retain private partial evidence, not a successful selection; inspect
it before retrying. Do not repair partial bundles by editing their manifest.

Verify a complete bundle, including after a mode-preserving copy:

```sh
python3 -m tool.deployment_build verify \
  --bundle /absolute/controller/path/to/bundle \
  --manifest-sha256 sha256:REPLACE_WITH_SEPARATELY_RETAINED_DIGEST \
  --gateway managed --platform linux/arm64
```

Verification needs neither Docker, Git nor network. It checks private,
single-link regular files, exact sizes/checksums, the component set, source
labels, target architecture, archive tags, config identity and OCI blobs.
Copied files must belong to the verifying account and retain their modes.
Archives are inspected, never extracted. Successful output selects exact
exporting-daemon image IDs (`sha256:...`), not repository pull digests.
Docker's classic and containerd image stores can identify the same image
differently. The internal reserved-load layer reconciles these identities;
the public transfer/apply workflow is not integrated yet.

### Preview a local bundle through Ansible

Use the shared development role via
[deploy-dev-build.yml](../../ansible/playbooks/deploy-dev-build.yml), replacing
Hub-selection variables in the inventory with:

```yaml
russound_local_bundle: /absolute/controller/path/to/bundle
russound_local_manifest_sha256: sha256:REPLACE_WITH_SEPARATELY_RETAINED_DIGEST
```

Keep the matching `russound_gateway`, `russound_platform`, data directory and
mode-specific serial GID/external endpoint/dummy ID:

```sh
ansible-playbook -i /path/to/preview-inventory.yml ansible/playbooks/deploy-dev-build.yml
```

This verifies existing artifacts offline and renders Compose with exact IDs.
It never builds implicitly, transfers/loads images, contacts the target or
starts/stops services, including with `--check`. Explicit
`russound_action=preflight` opts into the same first-install inspection described
below; it does not transfer/load images either. Hub selection, explicit Hub
references and local-bundle inputs cannot be mixed. `russound_action=apply`
remains blocked: human approval/input custody and the complete apply/recovery
workflow are not wired together. Local candidate records, reviewed SSH artifact
handoff and the reserved target-local loader now exist as internal runner APIs,
not Ansible actions.

## 1. Prepare one deployment file

Run commands from the repository root. Create a private environment file
outside the checkout, for example `$HOME/.config/russound/demo.env`.
Create its parent directory with mode `0700` and the file with mode `0600`.
Do not copy an existing deployment's data into a new layout without a reviewed
backup and migration.

```dotenv
RUSSOUND_PLATFORM=linux/arm64
RUSSOUND_WEB_IMAGE=itcornernet/mpdrussoundpi-web@sha256:REPLACE_WITH_VERIFIED_DIGEST
RUSSOUND_DATA_DIR=/absolute/path/to/new-russound-demo
RUSSOUND_UID=REPLACE_WITH_NUMERIC_UID
RUSSOUND_GID=REPLACE_WITH_NUMERIC_GID
RUSSOUND_PUBLISH_HOST=127.0.0.1
RUSSOUND_PUBLISH_PORT=8000
```

Replace placeholders; `linux/amd64` is also supported. Use `id -u` and `id -g`
for a directory owned by your deployment user. Create `config`, `data`, `logs`
and `secrets` inside `RUSSOUND_DATA_DIR` as that user; restrict `secrets` to
mode `0700`. Paths must exist before startup; Compose will not create missing
mount directories as root. The container identity must be able to write
config, data and logs and read any required secret files.

Copy `images.web.reference` from the saved candidate into `RUSSOUND_WEB_IMAGE`;
for managed mode also copy `images.ser2net.reference` into
`RUSSOUND_SER2NET_IMAGE`. Ordinary runs reuse these exact digest references,
not independently moving `dev` tags. Compose itself accepts tags and does not
verify source labels, release approval or matching image revisions.

The commands below assume selected images already exist locally. If not, pull
the **exact approved digest** explicitly before starting. Never substitute a
different image when that pull fails. Do not mix unrelated exported
`RUSSOUND_*` or `COMPOSE_*` variables into these commands: shell environment
overrides the environment file.

## 2. Choose the gateway

### External gateway

Add the existing gateway's container-reachable host and port to the file:

```dotenv
RUSSOUND_BACKEND_HOST=gateway.example
RUSSOUND_BACKEND_PORT=6666
```

`127.0.0.1` inside web is web itself, not the Docker host. Select a hostname/IP
reachable from the container. Host and port environment values override saved
web settings independently; remove both to restore a saved serial connection.
External mode does not start, stop or remove any existing gateway.

```sh
docker compose --env-file "$HOME/.config/russound/demo.env" -p russound-demo \
  -f compose.dev.yml config
docker compose --env-file "$HOME/.config/russound/demo.env" -p russound-demo \
  -f compose.dev.yml up -d --no-build --pull never --wait
```

### Managed USB gateway

Use a Linux USB host and verify no other service owns the serial adapter.
Add the approved gateway digest, device and device's numeric group ID
(`stat -c '%g' /dev/ttyUSB0`) to the file:

```dotenv
RUSSOUND_SER2NET_IMAGE=itcornernet/mpdrussoundpi-ser2net@sha256:REPLACE_WITH_MATCHING_DIGEST
RUSSOUND_SERIAL_DEVICE=/dev/ttyUSB0
RUSSOUND_SERIAL_GID=REPLACE_WITH_DEVICE_GROUP
```

The overlay supplies `russound-ser2net:6666` to web. Serial defaults are
`19200n81`; override `RUSSOUND_SERIAL_OPTIONS` only for known hardware needs.
The container healthcheck proves a TCP listener, not controller communication.

```sh
docker compose --env-file "$HOME/.config/russound/demo.env" -p russound-demo \
  -f compose.dev.yml -f compose.gateway.yml up -d --no-build --pull never --wait
```

### Dummy: no hardware

Build the dummy explicitly on the controller. This build may fetch a base image
and OS packages. Use clean, identified source for a deployment artifact;
working-tree builds are for disposable development tests, not release claims.

```sh
docker build -f Dockerfile.dummy -t mpdrussoundpi-dummy:local .
docker image inspect mpdrussoundpi-dummy:local --format '{{.Id}}'
```

Add the returned `sha256:...` image ID as `RUSSOUND_DUMMY_IMAGE` in the
environment file. Create `RUSSOUND_DATA_DIR/dummy-data` with the same writable
ownership as data. The overlay supplies `russound-dummy:6666` to web and seeds
dummy state separately from web state.

```sh
docker compose --env-file "$HOME/.config/russound/demo.env" -p russound-demo \
  -f compose.dev.yml -f compose.dummy.yml up -d --no-build --pull never --wait
```

The dummy TUI defaults off. To opt in, set `RUSSOUND_DUMMY_TUI=true` before
startup; attach using the same Compose options followed by
`exec russound-dummy tmux attach -t dummy`. Detach with `Ctrl-b d`; `Q` stops
the dummy. Dummy responses do not certify hardware or audio.

## 3. Configure and verify

Open `http://127.0.0.1:8000/` for the loopback example above. A fresh web
deployment opens setup; configure controllers, zones and sources without
guessing the real installation. Existing valid config/state are preserved.
HTTP health and `--wait` are not proof that a controller responds: check the
backend status separately. Playback agent TLS/authentication and audio require
their own acceptance.

The bind mounts are `/config`, `/data`, `/logs` and read-only `/run/secrets`.
Agent integration uses `RUSSOUND_PLAYBACK_AGENTS` plus separately provisioned
token and certificate files; see the [playback guide](playback-agent-deployment.md).
An unset agent value means no playback integration.
When changing Compose projects, check the selected network and the source
address actually seen by the agent proxy before cutover. A new project may
receive a different subnet and fail an existing proxy CIDR allowlist with
HTTP 403 even when TLS, web health and controller routing work. Do not copy
an old trust assumption or automatically authorize all Docker networks.

For LAN access, deliberately change the publish host, configure
`RUSSOUND_ALLOWED_HOSTS`, and review the [security notes](../../README.md#deployment-notes).
There is no user authentication; do not expose this port to the Internet.
Changing layouts or images is a deliberate operation, not a routine rerun.
Keep the same ordered Compose files and environment file for subsequent
commands. Before removing a development stack, review its project identity;
`down` with those exact inputs removes its containers/network, not its bind
data. Never use cleanup commands against an existing installation as a shortcut
for migration.

## Ansible preview

The new entry point defaults to **controller-only preview**. This default
does not connect to, provision or inspect the target. Inventory overrides role
defaults. Supply exactly one inventory host:

```yaml
all:
  hosts:
    preview:
      russound_gateway: external
      russound_backend_host: gateway.example
      russound_data_dir: /absolute/path/to/new-russound-demo
      russound_image_selection_file: /absolute/controller/path/to/dev-candidate.json
```

```sh
ansible-playbook -i /path/to/preview-inventory.yml ansible/playbooks/deploy-dev.yml
```

Use an absolute controller path to the saved candidate, matching the inventory's
gateway and platform (`russound_platform` defaults to `linux/arm64`).
For managed mode supply `russound_serial_gid`;
for dummy mode supply `russound_dummy_image` (the local image ID).
Remove mode-inapplicable image/endpoint inputs instead of retaining conflicting
configuration.

The Hub entry point's image selection has three mutually exclusive paths:

| Inputs | Behavior |
|---|---|
| `russound_image_selection_file` | Offline candidate validation and reuse; no registry calls |
| No selection file or image references | Fresh verified registry metadata resolution for this preview; candidate is **not saved** |
| Explicit `russound_web_image` and, for managed mode, `russound_ser2net_image` | Advanced offline, digest-syntax-only rendering; **no identity verification** |

The separate local-build entry point verifies a prepared bundle as described
above; it does not resolve or fall back to Hub images.

Do not combine a selection file with explicit images or provide only half a
managed pair. The preview reports which verification path it used.
The controller Python running Ansible invokes the standard-library resolver;
Docker Compose must also be installed on that controller.
The initial preview does not support playback agents or legacy source/mode
switches. Preview does not pull images or verify target permissions, installed
images, hardware or deployment safety.
`russound_action=apply` fails explicitly. Do not interpret a successful preview
as authorization or evidence of safe deployment.

## Reviewed development deployment and recovery

The public entry point is `python -m tool.deployment_operator`, not
`russound_action=apply`. It uses one strict-host-key SSH session for reviewed
approval, one target reservation, image transfer, independently verified
off-host backup, controlled local Ansible/Compose apply, independent checks
and finalization. There is no `--yes`, unattended update or automatic rollback.

### Prerequisites and trusted tooling

- Controller: Python **3.11+**, SSH, a private existing known-hosts file with
  independently verified target identity, and sufficient private off-host disk
  space. The controller must be a different host/storage failure domain from
  the target; a localhost copy is not disaster recovery.
- Target: Linux, Python **3.11+** with venv support, Git, local Docker and Compose,
  passwordless `sudo -n`, and prepared bind data/config/secrets with the correct
  numeric service owner/group. No rootless/userns daemon or remote Docker endpoint.
- Preserve the current Compose project, mounts, network and private environment.
  Supply a **fully rendered, self-contained private Compose JSON** matching the
  existing installation, with explicit platform and numeric `user`, the
  selected listener and (for web) config/data/logs/secrets binds. Render it using the exact current
  file order, project, working directory and environment; protect it with mode
  `0600` and root ownership on the target. For a first install, explicitly prepare
  these same inputs and valid web application configuration when deploying web; no sample rooms,
  credentials, proxy trust or audio mapping are generated.
- Only ordinary bridge/reused networks and the documented service fields are
  accepted. Custom commands, build instructions, privileged/shared namespaces,
  arbitrary mounts, extra services and layout changes require separate review.
  Rendered `command: null` and `entrypoint: null` are accepted unchanged as image
  defaults; non-null overrides (including empty strings/lists) are rejected.
  Dummy automatic deployment still needs its own pinned artifact integration;
  the isolated dummy layout remains available for tests.
- Recovery storage and live data must share a filesystem for preserved-tree
  activation. Unsupported source metadata/ownership and insufficient space
  fail explicitly. Backups are bounded to 8 GiB; allocate additional room for
  exported old images, received candidates and isolated restoration.

Install trusted tooling as a **separate administrative operation**, using a
reviewed, published full commit containing these tools:

```sh
ansible-playbook -i /path/to/target-inventory.yml \
  ansible/playbooks/bootstrap-deployment.yml \
  -e deployment_tools_revision=FULL_40_CHARACTER_REVIEWED_COMMIT
```

Select exactly one target using a dedicated inventory or `--limit HOST`.
Bootstrap, upgrade and restore accept arbitrary inventory host names and reject
multi-host selections before connecting or modifying tooling. This play installs only the exact tooling
checkout at `/opt/mpdrussoundpi-deploy`, its isolated dependency environment,
and private control roots. It never deploys/restarts application services.
An existing or partial tooling destination is rejected rather than overwritten.
Target package installation and dirty/unpublished controller changes are not
silently handled. Keep the approved tooling revision available for recovery.

### Upgrade or restore trusted tooling

Use the separate upgrade play for an existing installation. It requires the
exact current commit and an exact reviewed target commit; it rejects dirty
checkouts, existing stage paths, blocked deployment state and systems without
atomic Linux directory exchange. It reserves the host against deployment while
staging and switching, preserves the old checkout, recreates the virtual
environment at its fixed path, and verifies dependencies and the operator
entry point before releasing the reservation:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/upgrade-deployment.yml \
  --limit musicpi \
  -e deployment_tools_expected_revision=CURRENT_40_CHARACTER_COMMIT \
  -e deployment_tools_revision=REVIEWED_40_CHARACTER_COMMIT
```

The previous checkout remains at
`/opt/mpdrussoundpi-deploy.upgrade-REVIEWED_40_CHARACTER_COMMIT`. No application
service, configuration, credential, image or deployment data is changed. A
failed upgrade attempts to restore the prior checkout; uncertain outcomes keep
the host reservation and both trees for inspection. Do not remove the
reservation or either tree to force a retry. The retained virtual environment
uses the fixed active path and is usable again after the checkout is restored;
do not invoke it directly from the retained sibling.

To deliberately restore the retained checkout, identify both exact commits and
use:

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/restore-deployment-tools.yml \
  --limit musicpi \
  -e deployment_tools_active_revision=ACTIVE_40_CHARACTER_COMMIT \
  -e deployment_tools_restore_revision=RETAINED_40_CHARACTER_COMMIT
```

Restore changes only the tooling checkout and verifies its existing dependency
environment. The newer checkout remains retained under its upgrade path.
Interrupted restore or uncertain state requires inspection before any retry;
neither playbook removes old or partial trees automatically.

### Explicit target specification

Create a private controller JSON file, mode `0600`, containing exactly these
fields; replace example paths, identity, listener and gateway choices with the
actual reviewed installation:

```json
{
  "schema_version": 1,
  "deployment": "russound-dev",
  "project": "russound-dev",
  "compose": "/etc/mpdrussoundpi-deploy/baseline.json",
  "data_dir": "/srv/russound",
  "uid": 10001,
  "gid": 10001,
  "gateway": "external",
  "platform": "linux/arm64",
  "publish_host": "127.0.0.1",
  "publish_port": 8000,
  "serial_device": "/dev/ttyUSB0",
  "serial_gid": null,
  "require_controller": true,
  "require_playback": false,
  "adopt": false
}
```

`compose` and `data_dir` are **target paths**. Use `adopt: true` only for an
explicitly reviewed unrecorded existing project; actual generated Compose hashes
and container identities must match the supplied baseline. Managed mode requires
`gateway: "managed"` and the actual numeric serial group. External mode must
already contain its explicit backend environment/configuration.

Configure playback in the prepared Compose environment only if its protected
token and optional CA are present beneath the secrets bind; set
`require_playback: true` when configured. Credential parsing, service-mode access
and TLS trust-file loading are checked without disclosing their contents.
The deployment does not change proxy CIDRs, credentials or native playback.
`require_controller: false` is an explicit no-hardware acceptance choice, not
proof that a physical controller works.

### Split-host deployment on prepared hosts

The supported development split is:

- **Web host:** web container only, `gateway: "external"`, using the backend's
  reachable gateway address/port and HTTPS playback-agent endpoint.
- **Prepared backend host:** ser2net container only, `gateway: "standalone"`.
  The existing native agent, TLS proxy, supervisor, MPD and PipeWire/WirePlumber
  remain installed, configured and managed separately. This workflow never
  stages, activates, upgrades, stops or removes them.

This is **two independent reviewed host transactions**, not an atomic
multi-host rollout. Ordinary reruns, explicit image updates, off-host backups
and failed-operation recovery use the same operator as same-host deployments.
The backend transaction verifies container health and its published TCP
listener. The web transaction must require controller and playback acceptance
to establish the actual web-to-controller and web-to-agent paths. A TCP listener
alone does not prove a controller reply, audio readiness or audible output.

For representative runtime acceptance, a Mac with existing Docker Desktop can
run the web container against a prepared Pi backend without installing native
services or additional system-wide tools. A manual Mac-web/Pi-backend trial
verified real controller responses, authenticated CA-verified agent HTTPS,
fresh audio readiness/routes and reconnection after test-container recreation;
see the [acceptance record](../plans/general_open_simplify-deployment-configuration.md#real-mac-web--pi-backend-acceptance-2026-11-15).
Keep the Mac UI localhost-only and its data/credentials isolated, and remove
the test project and copied credentials afterward. This validates cross-host
runtime behavior, **not** the Linux-only root/Docker deployment operator on
macOS or the full automated two-target recovery workflow.

#### Prerequisites and network boundary

Both hosts need prepared data/control directories, a local Linux Docker daemon
and the approved trusted tooling revision described above. Keep their specs,
committed pins, operation evidence and backups distinct. The gateway data root
still has private `config`, `data`, `logs` and `secrets` subdirectories for the
common backup/recovery contract; they can be empty, are not mounted into
ser2net, and do not need a web configuration. **Do not point this root at native
playback data or credentials.** No web container is created on the backend.

Prepare these networking requirements explicitly before deployment:

- Give the backend a stable LAN address. Bind the standalone gateway to that
  specific address, not `0.0.0.0`, `::` or loopback. IPv4 and IPv6 are supported.
  Supply a free explicit host TCP port (typically 6666).
- Restrict the gateway port to the web host's actual source address using the
  network firewall or Docker-aware host filtering. **ser2net TCP has no TLS or
  authentication**: use only a trusted, restricted LAN/VPN, never a public
  listener or router port-forward. Publishing Docker ports can bypass ordinary
  host firewall rules such as UFW; verify the effective Docker forwarding rules.
  Listener binding is not a substitute for source filtering.
- Allow the web host's actual source address at the existing playback TLS proxy
  (normally the web host LAN address after Docker NAT, not its private bridge
  CIDR). Keep native agent and MPD listeners loopback-only.
- Prepare matching token and trusted CA files beneath the web host's private
  secrets bind, using the existing credential procedure. Use an HTTPS URL whose
  hostname/IP matches the certificate. Do not copy the proxy private key to the
  web host or disable certificate verification.

Firewall/proxy changes, credentials and native service installation are separate
administrative operations, not hidden side effects of a container update.
An unreachable gateway, bad token, certificate/name mismatch or unhealthy
configured playback prevents final web acceptance and leaves the operation
pending for inspection; it is not reported as a successful split deployment.

#### Select and preview each host

Resolve each host's exact candidate, allowing different supported architectures:

```sh
python -m tool.deployment_images resolve --gateway standalone \
  --platform linux/arm64 --output /absolute/private/backend-candidate.json
python -m tool.deployment_images resolve --gateway external \
  --platform linux/amd64 --output /absolute/private/web-candidate.json
```

Review their `revision` values together; use the same source revision for the
initial pair. A moving alias between resolutions can produce different
revisions: stop and resolve/review again rather than silently mixing them.
Standalone resolution pulls no images and inspects only the ser2net repository;
web-only resolution inspects only web. A verified local bundle built with
`tool.deployment_build --gateway standalone` likewise contains only ser2net.
Local bundles are built/verified on the controller, never on a deployment target.

The existing Ansible preview also supports `russound_gateway: standalone`.
In the backend host's inventory variables set:

```yaml
russound_gateway: standalone
russound_project: russound-backend
russound_platform: linux/arm64
russound_data_dir: /srv/russound-backend
russound_publish_host: "192.168.1.105"
russound_publish_port: 6666
russound_serial_device: /dev/ttyUSB0
russound_serial_gid: 20  # Replace with the actual device group.
russound_image_selection_file: /absolute/private/backend-candidate.json
```

Run `ansible/playbooks/deploy-dev.yml` with that inventory and `--limit` selecting
only the backend. Preview is controller-only; `russound_action=preflight`
explicitly inspects first-install prerequisites on the target without deploying.
Neither action starts services. Do not supply web images or playback-agent
inputs in standalone mode. Explicit gateway digest rendering and verified
local bundles are supported with the existing mutually exclusive image inputs.

For a protected, fully rendered target baseline, use **only**
`compose.backend.yml`, not `compose.dev.yml` plus an overlay. It extends the
same gateway definition used by the same-host layout, adding only the explicit
published TCP listener. Set `RUSSOUND_SER2NET_IMAGE` to the candidate's exact
reference, `RUSSOUND_PLATFORM`, `RUSSOUND_UID`, `RUSSOUND_GID`,
`RUSSOUND_SERIAL_DEVICE`, `RUSSOUND_SERIAL_GID` and optional
`RUSSOUND_SERIAL_OPTIONS`, plus these required listener variables:

```sh
export RUSSOUND_GATEWAY_PUBLISH_HOST=192.168.1.105
export RUSSOUND_GATEWAY_PUBLISH_PORT=6666
docker compose --env-file /dev/null -p russound-backend \
  -f compose.backend.yml config --format json > /absolute/private/backend-baseline.json
chmod 600 /absolute/private/backend-baseline.json
```

Use the protected target-baseline preparation procedure above to install these
reviewed bytes on the backend. `COMPOSE_DISABLE_ENV_FILE=1` and a clean, explicit
interpolation environment prevent accidental ambient settings; the Ansible
preview supplies that complete environment automatically.

#### Deploy, verify and recover independently

Use the documented specification schema with these backend values:
`deployment` and `project` set to `russound-backend`, `gateway: "standalone"`,
the backend's target baseline/data paths, actual UID/GID/device/group/platform
and the exact published host/port. Set **both** `require_controller` and
`require_playback` to `false`: those checks run on the web host, not a nonexistent
backend web API. The target rejects either flag being enabled in standalone
mode. Set `adopt` only for an explicitly reviewed existing **gateway-only**
project whose generated configuration matches the supplied baseline.

Deploy the backend first, then the web host, using separate evidence directories:

```sh
python -m tool.deployment_operator deploy \
  --host BACKEND_HOST --user BACKEND_USER --known-hosts /absolute/private/known_hosts \
  --spec /absolute/private/backend-spec.json --evidence /absolute/private/backend-evidence \
  --selection /absolute/private/backend-candidate.json
python -m tool.deployment_operator deploy \
  --host WEB_HOST --user WEB_USER --known-hosts /absolute/private/known_hosts \
  --spec /absolute/private/web-spec.json --evidence /absolute/private/web-evidence \
  --selection /absolute/private/web-candidate.json
```

The web spec uses `gateway: "external"`, `require_controller: true` and
`require_playback: true`. Its rendered `compose.dev.yml` must contain the
remote `RUSSOUND_BACKEND_HOST`/`RUSSOUND_BACKEND_PORT` and
`RUSSOUND_PLAYBACK_AGENTS` references described above. Docker service names
such as `russound-ser2net` do not resolve across hosts.

Each command requires its own live approval. Backend success returns required
checks `containers` and `gateway`; web success returns the existing container,
HTTP, backend and playback checks. Record operator-reported audible acceptance
separately. For updates add `--update` and the explicitly reviewed candidate;
for unchanged reruns omit the candidate as usual. Recover or resume a failed
operation against **that same host**, deployment ID and its own verified backup.
Do not roll back a healthy peer automatically after failure on the other host.
Preserve the prepared native services and all peer evidence.

**Not included:** splitting an existing combined web/gateway Compose project,
moving data between hosts, changing its network/credentials in place, or
activating native candidates. Those are layout/native migrations and remain
blocked by the existing layout checks. In particular, the currently working
combined Pi project cannot be converted merely by applying this backend spec.
Use a separately prepared backend or obtain a separate migration design and
maintenance approval before a physical split-host trial.

### Deploy, rerun and update

Create an operation evidence directory first, mode `0700`. Do not share it or
copy its backup manifests, credentials, source paths or content fingerprints
into public logs. For a verified local bundle:

```sh
python -m tool.deployment_operator deploy \
  --host 192.168.1.105 --user musicpi \
  --known-hosts /absolute/private/known_hosts \
  --spec /absolute/private/target.json --evidence /absolute/private/evidence \
  --bundle /absolute/private/bundle --manifest-sha256 sha256:MANIFEST_DIGEST
```

For Hub images, replace the last line with
`--selection /absolute/private/dev-candidate.json`, produced by the resolver
above. The target verifies pinned registry metadata, pulls only the selected
digests/platform, and checks actual root/config IDs and source labels. There
is no floating-tag or local-build fallback.

The terminal shows target identity, selected images, scope, interruption,
acceptance choices and required controller backup space. Type the exact
`approve OPERATION_UUID` phrase to proceed. Approval remains inside that live
SSH transaction; the target does not accept a disconnected digest as a reusable
authorization token.

On an ordinary rerun, omit both image-source options. The target reuses its
committed pin and verifies the unchanged runtime without stop/start, image
transfer, backup or recreation. Different candidates require **`--update`** plus
the explicit candidate/bundle and new approval. Settings, mounts, network and
credential changes are deliberately not smuggled into an image update.

Before interruption, the target validates layout/adoption, configuration,
service file access, credentials, daemon/platform, conflicting containers,
space and first-install listener/device prerequisites. After reservation it
installs protected inputs, stops only its project writers, checks native
writable descriptors and serial ownership, exports/verifies the old image
archives, and captures consistent private files. The controller independently
verifies every received backup blob **and the old Docker archive identities**
before acknowledging the backup. Only then can new image loading/pulling and
the controlled Ansible apply run.

Final checks inspect exact service/image identities, health and restart state,
container-loopback and published-host HTTP, application-reported backend
communication and configured playback-agent health. Controller reports are not
a substitute for S5 physical-controller/audio acceptance, fresh hardware fault
tests or a six-hour observation. A deliberately unrequested controller check
is reported as such, not as verified hardware.

### Failure, explicit recovery and retained evidence

On any uncertain result, **stop and inspect both hosts**. Do not retry deployment,
delete reservation files, prune images, invoke legacy playbooks, or assume a
timeout means remote work ended. The controlled Ansible deadline is ten minutes;
the SSH controller lifetime is bounded to one hour. Docker or target work may
outlive either client. Use the fixed target tooling's read-only status command
and inspect related processes, Docker activity and private evidence first:

```sh
ssh musicpi@192.168.1.105 \
  'cd /opt/mpdrussoundpi-deploy && sudo -n .venv/bin/python -m tool.deployment_state status'
```

For an interrupted operation with **no apply attempt**, the independently
reviewed original container/config/image identities must still match. Explicitly
restart and check only that original runtime, without restoring/discarding data:

```sh
python -m tool.deployment_operator resume-original \
  --host 192.168.1.105 --user musicpi --known-hosts /absolute/private/known_hosts \
  --evidence /absolute/private/evidence \
  --deployment russound-dev --operation FAILED_OPERATION_UUID
```

Once apply has been attempted, use the independently held backup. Read its
separately retained manifest identity from the private
`FAILED_OPERATION_UUID.backup-identity.json` controller evidence:

```sh
python -m tool.deployment_operator recover \
  --host 192.168.1.105 --user musicpi --known-hosts /absolute/private/known_hosts \
  --evidence /absolute/private/evidence \
  --deployment russound-dev --operation FAILED_OPERATION_UUID \
  --backup /absolute/private/evidence/FAILED_OPERATION_UUID \
  --backup-sha256 BARE_64_CHARACTER_MANIFEST_DIGEST
```

Supply the bare 64-character retained manifest digest. Recovery uploads into a new private
destination, verifies assets and requests a separate exact approval. It rejects
outstanding Ansible work, stops only the selected project, restores a new
isolated tree, loads the matching old images, and preserves the entire
post-backup tree under a unique `.failed-RECOVERY_UUID` sibling before activation.
It then checks the restored runtime before resolving pending state; the previous
committed selection remains unchanged. This restores matching old code **and
its own backed-up data**, not an assumed-compatible downgrade using newer data.

For a failed first install, recovery restores the prepared data and the original
absence of project containers. Its receipt explicitly checks absence/restoration,
not nonexistent HTTP or hardware. Other deployments and the external gateway
are left untouched.

Recovery and pre-apply resolution are also one-shot. Failed recovery retains
both trees, images, receipt/attempt evidence and blocked state; no overwrite,
second attempt, automatic cleanup or unlock is offered. Interrupted
finalization/persistence requires administrative investigation, not a forced
release. Backups, old images, failed trees and installed runtime files have
**no automatic expiry or pruning**.

### Explicit target preflight

For a **new Linux deployment**, opt into target inspection using the same
inventory and selected images:

```sh
ansible-playbook -i /path/to/preview-inventory.yml ansible/playbooks/deploy-dev.yml \
  -e russound_action=preflight
```

Unlike preview, this action connects to the selected target, discovers its
Python interpreter using normal Ansible configuration and runs a standard-library
probe. Controller input validation still runs first. Ansible may create its
normal temporary module files; the probe does not provision deployment files,
pull images, reserve the host, start/stop containers, reload services or
change proxy trust. It briefly binds and closes the requested listener to
check availability.

The probe checks local Docker/Compose availability and daemon platform;
rejects remote Docker endpoints and user-namespace remapping; and checks
existing projects, overlapping bind data, required directories, POSIX mode
permissions and listener availability. All bind directories must already exist.
Secrets require service ownership and mode `0700`. Symlink paths, shared-writable
ancestors and bind-directory ACLs require separate review and fail explicitly.
Managed serial inspection additionally requires root, the actual character
device/group, mode access and no observed open process descriptor or container
assignment. It never needs a device for external/dummy modes.

Existing Compose projects or data/device conflicts fail with a rerun/adoption
diagnostic, even if an existing container is stopped. This first preflight is
not yet a recorded-installation rerun check and should not be used as a
migration procedure for the running Pi.

Results explicitly report `safe_to_apply: false`: availability is only a
point-in-time observation. Directory checks do not prove an actual container
can write, saved configuration is not validated, available disk space is
reported rather than certified sufficient, and no backend/container/proxy
routing, image availability, physical controller or audio test occurs.
Nonzero probe results fail the play with redacted diagnostics.
`--check` does **not** suppress this explicitly requested read-only inspection;
use the default preview for zero target connections.

## Safe-apply implementation status

The internal [state layer](../../tool/deployment_state.py) implements a fixed
host-wide reservation and private pending/committed records. It uses exclusive
creation, file/directory synchronization and operation-bound phase transitions.
Process exit does not release a reservation. Uncertain writes retain evidence;
an uncertain final release may already have removed the reservation and is
never reported as a successful deployment. There is no operator unlock,
initialize, apply or finalize command in this module. Its only CLI action is
read-only status; the separate, explicitly approved tooling bootstrap prepares
the fixed root-owned state directory.

Local image candidates retain the verified bundle identity, source/platform/
tree metadata, archive descriptors and portable config digests; they are not
registry candidates and contain no local paths or credential fingerprints.
Their pending/committed selection additionally records `loaded_images`, the
exact IDs inspected on the target. An OCI-root exporting ID may become the
verified config ID on a classic Docker store; no arbitrary replacement ID is
accepted. Registry record schemas and behavior are unchanged.

The internal [reserved image loader](../../tool/deployment_load.py) consumes
already-transferred, private, checksum-verified bytes. It requires the state
owner, a local non-rootless/non-userns daemon on the selected platform, and a
matching reservation, candidate and intent at `backup-complete`. Before any
load it persists a one-shot image-load marker. Archive descriptors stay open
through streaming into Docker; identity/content is checked again afterward.
Unrelated operation-tag collisions fail, and load diagnostics stay in exclusive
private evidence files outside state/bundle storage. Both images must load and
pass identity/source/platform inspection before target IDs are recorded.

Partial load, timeout, process loss, file replacement or uncertain persistence
retains pending state and the marker: **no automatic retry, unload, rollback or
reservation release**. Docker may still be working after its client exits.
Loading alone cannot advance the operation to `apply-complete`, and a local
operation cannot reach that phase without recorded loaded IDs. Finalization
still requires the independent service/check completion receipt.
The loader neither provisions state nor stops/starts services, invokes SSH,
pulls images, builds, or edits Compose inputs. An internal state/intent match
does not establish human approval or a valid backup by itself; the invoking
runner must custody that evidence before recording the phase. There is no
operator load-only command and `russound_action=apply` remains blocked. The
reviewed operator workflow above owns this boundary; its separate pinned-Hub
loader uses a one-shot marker and validates pulled identities without changing
the registry record schema.

The internal [artifact handoff](../../tool/deployment_transfer.py) binds an
in-memory review digest to the selected candidate, operation/intent and explicit
SSH host/user/port, remote tooling/destination and existing host trust. Its
caller must obtain approval for that exact review; copying its digest is not
proof that a person approved it. Source bytes and trust-file identity are
rechecked. SSH ignores ambient client configuration, requires strict existing
host-key trust, disables forwarding and uses a private snapshot of the reviewed
known-hosts bytes. It never accepts a new host key automatically. An optional
existing private identity file is used only for SSH authentication, never
copied to the target or placed in records. Remote paths are shell-quoted.

The receiver requires already installed trusted tooling, Python 3.10+, the
existing state owner/reservation and matching candidate/intent before backup.
The current infrastructure CLI uses the fixed root-owned state directory:
this standalone artifact utility does not bootstrap/escalate by itself.
The operator workflow uses explicit trusted tooling/bootstrap and `sudo -n`
over its own duplex session; it does not upload controller code at deployment.
Before receiving artifact files, it durably records an exclusive transfer
marker. Incoming bytes use bounded length framing and operation-owned fixed
filenames, not tar extraction. Files go into a new private operation directory
and undergo full archive/config/source/checksum verification. A separate
durable verified flag and matching receipt are required before the controller
reports success; an unverified transfer marker blocks the image loader.
The transfer itself neither advances service phases nor loads Docker images.

Transport diagnostics and receipts are private and exclusive. The controller
spools one temporary wire copy of the complete pair in its private evidence
directory, so allow additional disk space for that copy (up to two 8-GiB
archives plus metadata). A timeout, disconnect, partial copy, changed input,
uncertain write or lost receipt keeps both ends for investigation: no
automatic retry, destination overwrite, cleanup, service change or reservation
release occurs. Even a failed controller result can leave a fully verified
target bundle; inspect both sides before deciding how to recover.

The internal [completion validator](../../tool/deployment_completion.py) runs
Ansible with a controlled notification callback and verifies private, allowlisted
events after process exit. It requires the exact expected task set, a complete
terminal record, zero failures/rescues/ignored errors and a separate successful
callback acknowledgement after syncing the evidence. It does not expose task
names, result payloads, command arguments or loop values. It neither applies a
deployment by itself nor dispatches finalization.

This first validator accepts only unconditional, synchronous required tasks;
required handlers must actually run. Loops, retries, asynchronous work,
conditional required tasks, failure/unreachable overrides, delegated required
tasks, forced check-mode tasks, skips and missing handlers block completion
rather than receiving guessed outcomes. These restrictions apply to the
internal runner, not to legacy playbooks. Invocation has a ten-minute deadline;
timeout does not prove target-side work stopped. Pending state and evidence
must remain blocked for investigation. Support for richer patterns remains
gated on their event mapping and tests.

### Internal target-local transaction runner

The [development runner](../../tool/deployment_runner.py) now connects the
existing primitives in one single-use, in-memory transaction:

1. Verify the local bundle, run platform preflight, and snapshot the selected
   private inputs, self-contained apply playbook and inventory.
2. Present non-secret scope and require explicit approval of that exact review.
   Recheck custody and preflight, then reserve target state before interruption.
3. Install new operation-specific runtime inputs; stop only approved writers
   through the platform integration and repeatedly verify quiescence.
4. Capture file recovery roots, require independent off-host verification, and
   retain the backup identity in private evidence before recording the boundary.
5. Load the verified bundle through the reserved loader. Supply actual target
   image IDs and installed input paths as private Ansible extra variables;
   never substitute an exporting engine's ID without target verification.
6. Require complete controlled Ansible evidence, then separately require the
   exact independent runtime-check set. Reverify input custody before
   finalization. Failed updates preserve the previous committed selection.

Any uncertain stop, backup, load, apply, check or persistence result leaves
reservation, pending state, backup, staging and installed runtime files intact.
There is no automatic retry, rollback, unlock or cleanup. Successful runtime
inputs remain installed after commit; the runner does not delete recovery data.

**This is an internal integration boundary, not an operator command.**
The [concrete Compose integration](../../tool/deployment_compose.py) now
implements its platform hooks, and the [operator driver](../../tool/deployment_operator.py)
owns approval, same-session image receipt and off-host custody. The target uses
one reservation and the real local Docker daemon, not a controller state replica.
Root-owned protected Compose/Ansible inputs are consumed by root tooling;
service-owned bind files and credentials are validated separately for the actual
numeric container identity. An automatically accepting callback is still not
human authorization, and internal APIs are not deployment shortcuts.

The internal [input snapshot layer](../../tool/deployment_inputs.py) copies
explicitly selected private regular files into a fresh private operation
directory, with read-only files and an in-memory approval seal. Source paths
and content fingerprints remain in the private manifest, never in the preview
or deployment record. Source content, identity, ownership and permissions are
rechecked; changes require a new preview/approval. Installation consumes the
snapshot bytes into a **new** stable private runtime file, never overwrites an
existing destination and verifies the installed owner/group/mode/content.
Mode checks include service traversal of runtime ancestors; reading as the
deploying root user is not treated as sufficient.
Cleanup removes only the operation's known files after independently resolved
work. Failure retains staging and any partial installed file for investigation.
Tests prove a separately started reader still works after staging removal.
This initial API accepts source modes `0400`/`0600`, uses at most 64 inputs of
16 MiB each, and has no operator CLI, cross-process reload or transfer protocol;
the internal runner preserves custody only within its single live invocation.
The operator keeps that live invocation under its duplex SSH approval session;
there is no detached approval reload or resume token.

The internal [file-backup layer](../../tool/deployment_backup.py) captures
explicitly selected file/directory roots only after a caller-supplied
writer-quiescence check. It repeats that check and source integrity comparisons;
it does not stop writers itself, and a reservation is not a writer fence.
Contents are streamed into exclusive private blobs; the final private manifest
records relative layout, original source paths, ownership, modes, modification
times, byte lengths and checksums. These private paths/checksums must not be
copied into a public report or deployment record. Keep the manifest identity
separately, and only after capture returns successfully. Reload/verification
requires that identity rather than trusting a checksum found inside the backup.

Verification rejects corrupt, partial or unexpected payloads before creating a
restore destination. `restore_new` writes only to a new isolated tree, never
onto original sources, backup storage or an existing destination; restored
bytes and metadata are checked. It does not activate that tree, clear pending
state or release the reservation. Failed writes leave the partial destination
and backup for investigation. Backups have **no automatic expiry or deletion**.
Operator-controlled live restore requires separate approval, writer/daemon
quiescence, code/data compatibility checks and accounting for post-backup edits.

Current boundaries:

- Linux extended-attribute inspection is required. ACLs, extended attributes,
  filesystem flags, symlinks, hardlinks, special files, special permission bits,
  shared-writable paths and unsupported ownership fail explicitly rather than
  being silently lost. Source objects must belong to root or the capture
  account, or to the explicitly selected service owner when capture runs as
  root. Ancestor ownership follows the same safe-path policy for that owner.
  Other mixed-service ownership requires separate review and fails explicitly.
- At most 64 selected roots, 10,000 entries, depth 64 and 8 GiB total contents;
  the private manifest is bounded to 8 MiB. Sparse files are stored as bytes,
  without preserving sparse allocation.
- SQLite is copied with **all writers stopped**, including any WAL/SHM files.
  Tests recover committed WAL content after unclean process exit. This is not
  an online SQLite backup or application/schema compatibility certification.
- The Compose integration exports actual old runtime IDs under unique recovery
  tags and verifies their archived root/config/platform identities. The controller
  repeats archive verification independently before acknowledging off-host
  custody. The generic file-backup API alone does not establish this.
- Use the reviewed operator recovery actions, not a standalone file API, for
  service activation or resolving pending state.

Local tests cover crashes/concurrency, persistence failures, successful and
failed handlers, ignored/rescued failures and unreachable targets. They do
not establish target filesystem/reboot durability or physical readiness.
The earlier opt-in container recovery test uses a
unique local Compose project and Linux data volume: it runs current web/dummy
source in an already cached Python-capable image and stops its own writers,
backs them up, observes a real failed Ansible image update, restores a new tree,
checks restored web-to-dummy routing and unchanged rerun, and confirms pending
remains blocked. Its synthetic unavailable candidate is deliberate fault
injection, not successful registry approval or adoption.
The newer operator integration fixture exercises actual strict SSH, same-layout
adoption, application image deployment, unchanged pins, a failing image,
independently held recovery assets, restoration and resolved pending state.
It also tests wrong host keys, lost off-host acknowledgement, explicit
pre-apply original-runtime restart, preserved operator edits and first-install
absence restoration. It uses disposable Linux volumes and projects, not the Pi.
Remaining work belongs to broader configuration/logging cleanup, additional
mode/layout migrations and S5 live hardware/audio/observation acceptance.
Do not use the internal Python APIs as a deployment shortcut.

## Existing installations and legacy entry points

The old `docker-compose.yml`, `docker-compose.dummy.yml` and
`deploy-russound-docker.yml` remain for compatibility;
they are **not aliases** for the new files. Do not mix their variables,
state paths or ordered file lists with the new workflow. Legacy Docker
provisioning can build on the target and stop native services; play-level
defaults also override inventory. The old `deploy-russound-web.yml` installer
is now a non-mutating refusal with a link to the native workflow below; it no
longer updates a checkout/venv or restarts services in place.
This compatibility surface is temporary in the repository plan: the final
cleanup step retires superseded deployment scripts and examples after the
replacement paths and migration/recovery acceptance are complete. It does not
authorize deleting target data, backups, local-only images or recovery
evidence.

For source-only native web candidate staging, use
[`stage-russound-web.yml`](../../ansible/playbooks/stage-russound-web.yml):

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/stage-russound-web.yml \
  --tags stage \
  -e russound_web_revision=FULL_40_OR_64_CHARACTER_COMMIT_SHA
```

This stages a clean, exact checkout and isolated dependency environment under
the deployment user's private candidate directory. It validates existing web
configuration but does not touch the active checkout, persistent state,
credentials, system packages, systemd units or running services. Candidate
staging requires Git, Python venv support and package-index access from the Pi.
An existing revision candidate is never overwritten; a partial candidate must
be inspected and a fresh revision selected. It is not an activation or
recovery workflow.

## Prepared native web and gateway

**Implementation with local lifecycle tests and isolated Pi staging evidence;
live native cutover, rollback, reboot and endurance acceptance remain pending.**
Use the reviewed tooling installation/upgrade procedure above to install these
committed tools first. Never copy unfinished code into the live tooling checkout.
This workflow manages only `russound-web.service` and the dedicated
`mpdrussoundpi-serial.service`, running as the existing nonroot account. It does
not modify the distribution `ser2net.service` or `/etc/ser2net.yaml`.
That distribution service must already be inactive and disabled. Agent,
supervisor, MPD, PipeWire, WirePlumber, proxy, credentials and OS packages are
preserved, not provisioned or activated here.

### Prerequisites and candidate staging

- Prepared Linux ARM64/AMD64 host, root-reviewed deployment tooling, Git,
  `/usr/bin/python3` with venv support and installed `/usr/sbin/ser2net`.
  Application runtime requires Python 3.11+. The prepared host's Python binary,
  version, architecture and ser2net binary are sealed into each candidate.
  Recovery requires the same unchanged OS/stdlib/shared-library installation;
  this is not fresh-machine or OS/package disaster recovery.
- Existing numeric service UID/primary GID, serial character device/GID,
  configured controller zones and working authenticated playback agents.
- Existing private data tree with `config/russound_config.json`,
  `data/russound_state.json`, `logs/` and `secrets/`. The same ownership,
  permission, metadata and stopped-writer backup restrictions as Compose apply.
  No application/schema conversion or credential generation is performed.
- Release/runtime directories under `/opt` and the data restore area must
  share a filesystem. Allow space for old releases, private stopped-writer
  backups, independently retained controller copies and isolated recovery.
  Nothing expires automatically.

Stage a full application commit already available in the trusted tooling
checkout (no target branch checkout/update):

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/stage-native-deployment.yml --limit musicpi --tags stage \
  -e native_application_revision=FULL_40_CHARACTER_COMMIT_SHA
```

Staging exports only committed web/runtime source, creates a new root-owned
release under `/opt/mpdrussoundpi-native/UUID`, installs bounded web dependencies
in a separate copied-Python venv, runs `pip check`/imports and seals every
source/environment byte and mode. Public code/dependencies are service-readable
even with umask 077; no credentials belong in a release. Links and unsupported
metadata are refused rather than lost during backup. Staging never starts a
service, changes active data, installs OS packages or overwrites an earlier
release. Retain the emitted candidate JSON as a private mode-0400/0600 controller
file. Failed/partial release directories remain for inspection; stage a new
UUID rather than repairing one.

### Explicit native specification and activation

Create a private JSON specification with exactly these fields (example values
must match the actual prepared host):

```json
{
  "schema_version": 1,
  "deployment": "russound-dev",
  "data_dir": "/home/musicpi/.local/share/mpdrussoundpi/prepared/data",
  "uid": 1000,
  "gid": 1000,
  "publish_host": "0.0.0.0",
  "publish_port": 8000,
  "serial_device": "/dev/ttyUSB0",
  "serial_gid": 20,
  "serial_options": "19200n81",
  "backend_port": 6666,
  "playback_agents": [{
    "agent_id": "musicpi",
    "url": "https://192.168.1.105:8443",
    "token_file": "/run/secrets/musicpi-playback-token",
    "tls_ca_file": "/run/secrets/musicpi-proxy.crt"
  }],
  "from_compose": null
}
```

`from_compose: null` means a prepared first native installation with **no**
unrecorded native units, or a same-layout update of an already recorded native
installation. Existing legacy-native units require a separate migration.
Listeners must be distinct unprivileged ports; the web binding is an explicit
IPv4 address. Supported serial framing is `19200n81` or `9600n81`.
The serial listener is always **127.0.0.1 only**. Native web overrides its
backend host/port to that listener without rewriting saved configuration.

For a **Docker-to-native cutover**, use the same deployment ID/data/account,
and set `from_compose` to the complete JSON object of the already recorded,
healthy managed-gateway Compose specification. This is an object, not a path.
The supplied Compose file must still describe the actual runtime exactly.
Web listener, data root, device/GID and serial framing must match; both
controller and playback acceptance must be required. `restart: always` is
refused because stopping those containers does not fence them across reboot.
Unrecorded Docker adoption must be completed through the Compose workflow
first. Do not edit Docker configuration, move state, relax TLS or rotate
credentials to make a cutover pass.

The playback registry deliberately retains `/run/secrets/...` identifiers in
the specification. Validation uses the shared protected-bind parser; the
native unit maps those identifiers to the same files beneath
`data_dir/secrets/`. No tokens/certificates are copied or embedded in units.

```sh
.venv/bin/python -m tool.deployment_operator native-deploy \
  --host 192.168.1.105 --user musicpi \
  --known-hosts /absolute/private/known_hosts \
  --evidence /absolute/private/native-evidence \
  --spec /absolute/private/native-spec.json \
  --selection /absolute/private/native-candidate.json --update
```

As with Compose, strict reviewed SSH trust, private controller evidence and
interactive approval are required. The review identifies source/environment,
origin, account, serial framing/device, listeners, playback identities and
interruption. `--update` is required when replacing an existing committed
selection, including Docker-to-native cutover. It does not approve the review.
The reviewed backup capacity includes a 64 MiB margin for ordinary live log
growth during approval. Repeated preflight checks retain that capacity, reject
growth beyond it, and recheck available target space before stopping writers.

After approval, the shared host-wide reservation blocks other native/Compose
deployments and tooling switches. Only the selected old web/gateway writers
are stopped. A consistent backup captures data plus either the actual old
Docker image archives or the complete previous native source/venv/runtime.
The controller independently verifies and retains those bytes before the
target installs units or starts a candidate. Controller-transfer failure
leaves writers stopped and pending state intact, not a success or automatic
rollback. Native units use the journal and bounded application defaults; no
global journal/daemon retention changes occur.

Independent checks require exact units/release/gateway configuration, enabled
and active services without restarts, host HTTP routing, configured physical
controller connectivity and healthy authenticated playback routing. They are
not audible or all-room acceptance. Only then is native state committed.
Use `from_compose: null` on subsequent native operations and keep all other
settings unchanged. Rerunning the same candidate verifies health without
restarting services or taking another backup. Settings/data/network/schema
migrations are refused instead of being hidden in ordinary updates.

### Explicit recovery and a reversible native trial

For a failed operation **after a verified backup**, or to reverse the latest
successful native trial, use its independently retained backup and separately
retained manifest identity:

```sh
.venv/bin/python -m tool.deployment_operator native-recover \
  --host 192.168.1.105 --user musicpi \
  --known-hosts /absolute/private/known_hosts \
  --evidence /absolute/private/native-recovery-evidence \
  --deployment russound-dev --operation ORIGINAL_OPERATION_UUID \
  --backup /absolute/private/native-evidence/ORIGINAL_OPERATION_UUID \
  --backup-sha256 SEPARATELY_RETAINED_MANIFEST_SHA256
```

This needs fresh approval, matching pending/latest state and no outstanding
orchestration. It restores to an isolated tree, preserves post-backup live
trees as `.failed-RECOVERY_UUID`, restores matching old source/environment
or Docker images plus data, verifies the original runtime, then restores its
previous committed record. First-install recovery restores prepared data and
the original absence of managed native units; it does not pretend an absent
web server passed HTTP checks. A failed/interrupted recovery retains the
reservation and one-attempt marker for investigation; do not delete locks,
retry automatically or restore over newer data.

For a **pre-apply** interruption such as lost off-host acknowledgement, when
the original units/container identities are still verifiable, use
`native-resume-original` with the same SSH/evidence/deployment/operation
arguments, without backup arguments. This restarts only the verified original
runtime, preserves current data/operator edits, and refuses after any native
apply attempt. If identities changed, use reviewed backup recovery instead.
Ordinary Compose apply cannot switch a committed native deployment back to
Docker; only the explicit, latest-operation recovery described above covers
the recorded cutover baseline.

The earlier six-hour/audio acceptance belongs to the restored Docker layout.
Neither it nor isolated native candidate tests certify native activation,
reboot, physical playback or native endurance. Those remain explicit target
acceptance gates in the implementation plan.

The Pi's extra Compose override and local-only web image need explicit
preservation. Continue to treat adoption as a separately approved migration.
The [implementation plan](../plans/general_open_simplify-deployment-configuration.md)
records remaining safety, native/local-build intents and integration gates.

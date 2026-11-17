# Plan: simplify deployment and configuration

Status: **open; S0–S4 implementation gates closed; S5 final handover remains open.
Superseded web/gateway repository paths retired; native lifecycle/reboot
checks, the operator-approved four-hour observation and representative native
audible acceptance passed. Final layout/handover remain open** (2026-11-17).
The Waitress/debug change below is authorized
separately; this plan does not authorize deployment, service restarts, hardware
tests, or changes to a live installation.

## Goal

Reduce the number of ways to configure and deploy the solution, make the
remaining choices explicit, and provide one consistent, documented precedence
policy across components. Simplify the Ansible entry points so an operator can
understand which parts run where and which images or source revisions are used.
Preserve existing configuration and state through any migration.

## Proposed end state: operator overview

This is the **target**, not a claim that the full workflow exists. The new
Compose profiles, verified development-image candidate resolution,
controller-only development preview and explicit read-only first-install
target preflight are implemented. Explicit controller local-build artifacts and
offline local-build Ansible preview are also implemented;
see the [container guide](../documentation/container-deployment.md) for actual
commands and boundaries. Reviewed automated apply, committed image pins and
explicit recovery now exist for prepared same-layout Linux development deployments.
Standalone gateways on prepared hybrid backend hosts now share that reviewed
workflow; the separate web host uses external-gateway mode. Prepared native
web/gateway staging, reviewed managed-Docker cutover and recovery are implemented
and their representative Pi trial passed. Further native acceptance was
authorized with a one-hour rather than six-hour observation, paused, and later
resumed after controller/audio recovery fixes. Native release-switch/recovery
and reboot checks passed; the operator-approved four-hour observation passed.
Representative audible acceptance passed; the final operating layout remains open.
Production remains blocked without approved stable images/tooling; automated
same-host/split-host migration and full fresh-machine provisioning are excluded.
See the authoritative [support matrix](../documentation/container-deployment.md#support-and-acceptance-boundaries).
Local candidate state and
reserved target-local image loading and reviewed SSH artifact handoff are
implemented internally; human approval/input custody and full apply
integration across controller and target are implemented by the new operator
driver and concrete Compose integration. The target-local transaction connects
approval, snapshots, backup, image load/pull, controlled Ansible apply,
independent checks and finalization. Use the reviewed operator command, never
the internal APIs or the still-blocked preview-play apply switch.

### Choose how to run it

| Operator choice | Default source and behavior | When to change it |
|---|---|---|
| **Docker development** | Published Hub development images for web and ser2net; detailed, bounded logs. No build. | Select an explicit local build only for source changes. Build on the Ansible controller from identified source and transfer verified images to the target. |
| **Docker production** | An approved matching pair of stable Hub images; minimal operational logs. | Select a different approved stable pair deliberately. Stable publication is not yet available: report the blocker instead of using dev or building. |
| **Docker local build** | Explicit dev opt-in, using the same development deployment profile. | Supply a clean, identified source revision; refuse ambiguous or dirty source without deleting or resetting changes. |
| **Prepared bare metal** | Sealed native web and dedicated loopback gateway, preserving native Pi playback services. | Use the reviewed native workflow only; matching recorded managed-Docker cutover/recovery is supported. Representative native listening passed; final handover remains open. No fresh-machine/full-stack installer or legacy-native adoption. |

Two Compose files express **development** and **production**, with optional
gateway/dummy overlays and a standalone backend profile. Development previews,
reviewed container activation/recovery and sealed native staging/activation
have distinct entry points; **release** remains a blocked intent, not an
implemented deployment play. The default Pi layout remains hybrid: web and
gateway in containers; agent, proxy, MPD and audio on the Pi. Preserve external
gateways, separate web hosts and the hardware-free dummy backend. No image
fallback or unattended update is implied. Include a separate managed-gateway
overlay only when the deployment manages the gateway.

Future [guided first-time setup](./general_idea_guided-first-time-setup.md#future-gateway-discovery)
will use gateway mDNS announcements to suggest discovered instances when no
configuration overlay is supplied. This is a later feature, not a dependency
or implementation task for the current deployment rework.

### Configure only what the operator needs

| What the operator chooses | Proposed default | Where it belongs |
|---|---|---|
| Deployment mode and image pair | Dev: Hub development pair. Production: approved stable pair when available; otherwise fail clearly. Resolve and record exact revisions/digests at deployment. | One deployment inventory/configuration, not browser settings. Reuse an existing pin on ordinary reruns; updates are explicit. |
| Web access | Port 8000, reachable on the trusted LAN; no router exposure. Use an existing TLS reverse proxy for access beyond that boundary. | Deployment configuration. Explicitly configure host/origin restrictions and trusted proxy hops when applicable; do not infer trusted networks. |
| Russound connection | Managed gateway on the same host, serial `/dev/ttyUSB0`, `19200n81`, internal port 6666. | Deployment configuration for gateway placement and device; web config/editor owns controllers, zones, inputs and shortcuts. An external gateway requires an explicit endpoint. Verify the device and permissions rather than guessing hardware. |
| Playback | Optional: no agent configured by default. When enabled, use the existing native Pi agent on loopback port 8081 behind the TLS proxy on 8443; manual operations remain explicit opt-in. | Audio admin configuration for hardware/output mapping; web deployment configuration for an agent connection. Do not start playback, create credentials or guess a mapping without consent. |
| User choices and playback state | Start from an explicitly reviewed initial setup; preserve existing data on every rerun. No example room, station or hardware name is assumed correct. | Web editor owns rooms/inputs/shortcuts; Pi admin file owns hardware and seed stations; runtime state owns favorites, selected sources, gain and play intention. |
| Logs | Dev: DEBUG diagnostics; production: warnings/errors plus essential health and security events. Protocol frame audit **off** unless separately enabled. Web files: 10 MiB plus five backups; audit: 5 MiB plus three. Docker: per-container `10m`, three files. Native services use the host journal without changing its global retention. | One deployment profile per mode. Avoid secrets and raw stream URLs; debug diagnostics remain subject to privacy review and redaction. |
| Secrets and trust | No default credentials, certificate, trusted source CIDR or DNS resolver is guessed. | Protected files and explicitly supplied network/trust settings; never in public config or logs. Fail with actionable prerequisites when playback integration is enabled but they are absent. |

For settings that genuinely have multiple runtime sources, the agreed rule
is **explicit CLI > environment > saved configuration > built-in default**;
sources that do not exist for a field are omitted. Ansible variables and
Compose interpolation are upstream deployment layers, not a second runtime
priority rule. Defaults must be valid and useful, while security-sensitive and
hardware-specific inputs without a safe universal default are required
explicitly. Blank optional values are treated as unset; blank required values
and other invalid inputs fail explicitly. Design migration guidance for
existing fallback behavior. Show only safe, allowlisted effective values and
their source.

### Safe deployment path

1. Select the intent and review the small set of nondefault inputs; preview
   component placement, images, settings and expected interruptions.
2. Preflight architecture, space, image availability, configuration,
   permissions, hardware and trust **before** stopping a working service.
3. Obtain explicit approval for the maintenance scope, including any writer
   interruption. Reserve the host and record pending before disruption; back
   up configuration, state, credentials and deployed image/source identities
   privately, using an online SQLite backup or stopping writers as approved.
4. Apply only the approved components, retain existing state and verify web,
   gateway/controller, agent and audio separately.
5. On failure, stop and report what changed; ask the operator before rollback.
   Verify binary/data compatibility and restore a matching backup when needed;
   cross-version downgrade is not assumed safe.

Migration applies when an existing installation must move to a new layout or
data format, not merely when this plan is agreed or implemented. Rehearse it
beforehand; apply it to the Pi only in a separately approved maintenance
window. Ordinary reruns using the same layout must not trigger migration.
During an approved migration, inform the operator that existing protocol audit
will be disabled; back up the existing configuration first. Audit can be
explicitly enabled again afterward. This does not disable audit on ordinary
reruns or make a change to the Pi now.

### Cleanup included in the rework

- **Logging:** remove import-order-dependent setup; make each component's
  diagnostics predictable, redacted and bounded under the agreed profiles.
  Keep protocol audit separately opted in.
- **Python dependencies:** make the intended audio and agent dependencies
  explicit and reproducible. Prepare and validate a separate isolated
  environment before changing a service; do not delete, modify or replace
  the working Pi environment during inventory or ordinary web-only updates.
- **Generated deployment inputs:** replace ambiguous defaults and ad hoc
  overrides with clear operator inputs and recorded image identities, while
  preserving existing configuration, state and the running outage-fix image
  until an explicitly approved transition.

Detailed field-by-field evidence, current exceptions and still-open checks are
retained in the collapsible S0 inventory below.

## Requested deployment model

Provide two clear Docker Compose deployment files (final names to be decided):

| Mode | Default image source | Logging | Optional source build |
|---|---|---|---|
| Development | Docker Hub development images | Advanced diagnostic logging, with bounded retention and redaction | Yes, explicit opt-in to build locally from source |
| Production | Docker Hub stable release images | Minimal operational logging; warnings/errors and necessary health/security diagnostics remain visible | No implicit build |

Split Ansible deployment into understandable subparts or entry points for
**Docker release**, **Docker dev**, **Docker local build**, and **bare metal**.
Shared, narrowly scoped tasks may be reused rather than copied. A local build
is a development option, not an accidental fallback when a registry pull fails.
The images should be versioned and compatible across components, with image
source, revision/digest, architecture and pull policy explicit.

Stable Docker releases are not yet available. Production must fail clearly
until an approved compatible stable release exists; do not silently substitute
development images or build from source. Development defaults to published dev
images. Local-build mode builds on the Ansible controller and transfers verified
images to the target. Precisely define selection, source revision, target
platform, artifact verification and dirty-checkout behavior.

Define the supported component placement explicitly: web, serial gateway,
playback agent, proxy and MPD/audio services. The current agent/audio stack is
native on the Pi; “Docker deployment” must not imply all components are
containerized. Preserve the external serial gateway, separate web host and
hardware-free dummy layouts without mistaking the dummy for a physical gateway.

## Initial complexity and important constraints

The legacy source descriptions in the inventory below are historical evidence,
not current deployment instructions. Superseded files were removed after the
prepared-host acceptance trials; see the retirement record at the end.

- The former `docker-compose.yml` declared both
  local `build` and local `image` names. The earlier baseline Docker playbook
  builds on the target; subsequent local work adds an optional registry
  override and flags for published images. Native web deployment is a
  separate playbook. These changes are not evidence of a released feature.
- Configuration is split among persisted application config/state, environment,
  CLI flags, Compose interpolation, Ansible variables, systemd units and
  agent/audio configuration. Some are deployment inputs or secret references,
  not interchangeable application settings. The
  [effective-configuration plan](./web_done_effective-config-overrides.md)
  documents verified examples and depends on accurate source/precedence data.
- `RUSSOUND_DEBUG` / `--debug` now control diagnostic logging while Waitress
  serves HTTP in both modes. Flask's development server and debugger are not
  enabled by these flags. Protocol-audit logging is separately configured;
  define its privacy and retention independently.
- Production “minimal logging” must not suppress actionable errors, security
  events or health failures. Define concrete log levels, destinations, size
  limits, rotation and retention for **each** component and for both modes.
  Avoid credentials, secret paths and raw stream URLs in logs.
- Pi deployments currently include existing config/state, private tokens,
  playback intentions, TLS trust, USB device mappings and audio services.
  Switching layouts can interrupt playback or hardware control; no design may
  overwrite those assets or stop unrelated services implicitly.

## S0: initial inventory

<details>
<summary>Detailed current-state inventory, evidence and remaining checks</summary>

This inventory covers the working tree and a read-only Pi snapshot on
2026-11-12. The source-level matrix and representative probes below are
an inventory, not exhaustive execution of every setting; unresolved risks
and their later-stage disposition remain recorded. Reconcile
earlier deployment work with the design before reuse. Never record
token contents, private keys or full stream URLs.

### Existing paths and component placement

| Path | Current behavior and important boundary |
|---|---|
| Former hardware Compose (`docker-compose.yml`) | Local web and ser2net images with build definitions; serial device/group passthrough; web config, data, logs and read-only secrets bind mounts; web port 8000 by default. A Compose environment value may be interpolation only or may be passed to a container. |
| Former dummy Compose (`docker-compose.dummy.yml`) | Locally built web plus dummy backend for hardware-free development; separate dummy state and port. Its mounts and passed settings differ from hardware Compose. It is not a physical-controller test. |
| Former Docker Ansible play (`deploy-russound-docker.yml`) | Earlier baseline clones/updates the target checkout, generates `.env`, builds there and can use an external gateway. It stops native web and, when managed, native ser2net. Subsequent local changes add explicit published-image references, a registry override, pull policy and optional controller check; they also change missing-device behavior. Review them separately, not as already shipped. |
| Former native web play (`deploy-russound-web.yml`) | Target checkout and Python venv; systemd web service; optionally installs and restarts native ser2net. Its default Git version is `main`; CLI settings are generated in the unit. |
| [Audio setup](../../ansible/playbooks/setup-musicpi.yml) | Native Pi MPD/audio, supervisor and native ser2net; tagged setup can restart audio and alter services. This is not a web-only update. |
| [Agent](../../ansible/playbooks/deploy-playback-agent.yml), [proxy](../../ansible/playbooks/deploy-playback-agent-proxy.yml), [egress](../../ansible/playbooks/deploy-playback-egress.yml) | Independent native plays. Agent/egress source comes from the controller checkout, unlike target Git updates; agent starts only when requested. Proxy requires TLS/CIDR configuration; egress activation can interrupt MPD. The proxy also has subsequent local changes. |
| Existing Pi acceptance | The [integration record](./general_done_mpd-web-integration.md#6-deployment-acceptance) describes native playback/agent/proxy and containerized web/gateway, plus a locally derived web image. The read-only snapshot below confirms identities and coarse health only. |

Development images for both published components exist for `amd64` and
`arm64`; the [release guide](../documentation/docker-image-release.md) says
stable/candidate publication is not enabled. The native agent is not an image.
The [containerization plan](./mpd_new_agent-containerization.md) is separate.

### Initial settings inventory

| Settings / owner | Current sources and examples | Inventory still needed |
|---|---|---|
| Web listener, paths and server | [Server CLI/environment](../../web/russound_server.py): explicit `--host`, `--port`, `--config`, `--state` override their environment defaults; Compose passes container paths and listener values; native systemd passes CLI options. | Enumerate all server options, defaults, invalid/empty handling and restart effects; include debug/server choice and Waitress options. |
| Web backend and persisted UI config/state | [Endpoint resolver](../../web/config_types.py) and saved web config; nonempty host and valid port overrides can select TCP, including from serial. This initial inventory predates the S3 validation changes recorded in the [effective-config plan](./web_done_effective-config-overrides.md). | Map every editor/API field, fallback, path, save behavior, consumer and compatibility requirement. Do not apply a blanket precedence claim. |
| Agent connections and security | `RUSSOUND_PLAYBACK_AGENTS` supplies a JSON list; Compose mounts token/certificate files separately. Web host/origin and trusted-proxy settings are process environment. | Record validation, defaults, file ownership, non-secret provenance, restart/recreate requirements and which deployments pass each variable. |
| Serial gateway | [Gateway entrypoint](../../docker/entrypoint-ser2net.sh) reads device, options, bind, port and optional connector from environment; native ser2net uses its configuration file. Compose supplies a subset plus host device/GID. | Map direct-entrypoint and Compose inputs separately, port exposure, ownership, validation and external-gateway behavior. |
| Playback and audio | [Admin config and runtime state](../documentation/mpd-headless-deployment.md): output hardware, station seeds and watchdog settings in config; favorites, intentions, selections and gain in state. Agent CLI and generated systemd units add identity, token path, listener and operations switches. | Inventory every schema field and CLI flag, defaults, validators, XDG paths, systemd environment, generated MPD/PipeWire files and their readers/writers. |
| Proxy and egress | Ansible variables render Caddy listener, certificate, CIDRs and operations policy; egress requires explicit resolver addresses and activation. | Map generated files, private assets, trust, migration and restart effects, including Docker/NAT source addresses. |
| Deployment and logging | Ansible inventory/play vars and extra vars generate files; Compose interpolates shell/`.env` into service definitions. Runtime logs, protocol audit, container stdout and systemd journals are distinct. | Inventory all variables and exact Ansible/Compose precedence, paths, logging destinations/rotation/redaction and whether settings reach the process. |

S0 records a reviewable per-setting matrix: owner,
current inputs and priority, default, type/validation (including absent,
empty and malformed), secret classification, writable source, restart effect,
consumers, proposed mapping and compatibility/migration test. Include
environment-only, CLI-only and file-only settings rather than implying all
have equivalent inputs. Compare committed code, local changes, documentation
and live deployment identity without exposing private data; flag gaps rather
than inferring behavior.

### Read-only Pi snapshot, 2026-11-12

- Host `musicpi`, `aarch64`; target checkout at
  `8f6bf092609bf910eb529684d364d191af5a3ab6`, with no tracked changes
  reported. This does not establish whether untracked files exist.
- Web: `mpdrussoundpi-web:source-outage-ui-20261112`, local image ID
  `sha256:06067cd031893d152298b874e15b6aacf790d32e5d037b07ae9c420f3b6c4d5d`.
  Ser2net: `itcornernet/mpdrussoundpi-ser2net:dev-8f6bf092609bf910eb529684d364d191af5a3ab6`.
  Both containers reported healthy and shared `mpdrussoundpi_default`; web
  publishes port 8000, gateway port 6666 is internal. From inside web,
  `russound-ser2net:6666` accepted a TCP connection. Web has writable bind
  mounts at `/config`, `/data`, `/logs` and a read-only bind mount at
  `/run/secrets`. The local web Compose override exists. This local web image
  is not the newer Hub development image.
- Native proxy, egress, agent, supervisor and MPD meta-control services were
  active; native web and ser2net services were inactive. Web's rooms API
  reported controller connected and its playback API reported the `musicpi`
  agent `ok`. These point-in-time observations are not a physical audio,
  outage, migration or rollback test.
- Among checkout `lib/*.py` helpers, the installed agent copy of
  `outputconfig.py` differs and `playbackwatchdog.py` is absent; the remaining
  common helpers compared equal and no extra installed `lib/*.py` helpers were
  found. The installed `playbackagent.py` and `requirements-agent.txt` match
  the target checkout. This does not establish whether the absent helper is
  required by the installed version or whether other installed files differ.
  Do not equate the Pi checkout revision with every deployed native file.
  Identify and preserve the actual deployed sources during migration.
- The agent installation root also contains a venv and
  `russound_debugging.log`; there were no other non-Python helper files
  outside the excluded `__pycache__`. This is a names-only inventory, not a
  comparison of dependencies, cached bytecode, logs or the venv contents.
- The installed agent venv reports Python 3.13.5, excludes system site
  packages and has Flask 3.1.3, Waitress 3.0.2 and python-mpd2 3.1.1.
  The existing audio venv also reports Python 3.13.5 and python-mpd2 3.1.1
  but includes system site packages. The agent manifest does not pin package
  versions. Record the actual runtime and system-package dependency boundary
  before replacing either environment; equal python-mpd2 versions alone do
  not establish compatibility.
- The Pi has three generated MPD configs and user units, each with a
  `50-egress.conf` drop-in, plus the meta, supervisor, agent and egress-slice
  units. The PipeWire sink fragment, WirePlumber card fragment and both USB
  helper scripts exist, are not symlinks and have managed-file headers.
  These are existence/header checks, not content validation or restore tests.
- A later read-only check confirmed web bind-mount sources under the Pi
  checkout's `docker/config`, `docker/data`, `docker/logs` and
  `docker/secrets` directories, with secrets mounted read-only. Docker reports
  three Compose inputs for web: the checkout Compose file, a registry override
  and the separate outage-UI override under the private deployment directory.
  These are live deployment inputs, not evidence that the controller's
  registry template is identical to them. Rendering the Pi's base
  and registry inputs without the outage override selects the older Hub
  development web image; including the outage override selects the currently
  running local outage-fix image. The gateway image remains unchanged.
- Web's configured agent uses HTTPS; its token and CA references point inside
  the read-only secrets mount. A read-only certificate comparison found that
  the mounted CA certificate matches the Pi proxy's configured certificate.
  A subsequent short TLS handshake from the running web container succeeded
  using that CA and URL hostname. The matching server-side peer observed
  during the handshake had the same IP as the web socket and was in the
  configured Caddy source CIDRs. This verifies the current Pi route only,
  not NAT or peer behavior for a separate web host; it is not an authenticated
  application request or a continuous availability test. From the controller
  on the LAN, a separate HTTPS connection verified the proxy certificate and
  IP hostname, and an unauthenticated health request received HTTP 401. This
  shows the current controller source reaches the agent authentication gate;
  it does not verify an authenticated separate-host web deployment.
- Web and gateway report Docker `json-file` logging with no per-container
  options; the daemon reports `json-file` and has no `/etc/docker/daemon.json`.
  This does not rule out daemon command-line settings or prove a retention
  limit. The Pi's journald configuration includes persistent storage and a
  `SystemMaxUse=200M` setting. Agent, supervisor, proxy and egress unit
  metadata reports stdout to the journal and stderr inherited. These are
  host-wide settings, not a per-component logging contract.

### S0 follow-up: deployment ownership and restart boundaries

The following describes the working tree at inventory time, including the
then-uncommitted changes, not the desired implementation. All seven playbooks passed
`ansible-playbook -i ansible/inventory.yaml <playbook> --syntax-check` on the
controller on 2026-11-12. Syntax checks do not execute tasks, render all
templates, verify prerequisites, or establish idempotence.

| Generated or managed item | Current owner and lifecycle | Safety or design consequence |
|---|---|---|
| Docker `.env` | Docker play renders it in the target checkout with mode `0640`, `no_log` and diffs disabled. Compose reads it for interpolation; the play rewrites it on rerun. | Local edits are not durable. A no-connection Ansible probe using the actual Docker play vars and a temporary inventory verified that inventory values for published port, serial device, managed gateway and debug lose to play defaults, while extra vars win. Facts and `set_fact` introduce other precedence layers; test remaining intended variables individually. |
| Docker registry override | The Docker changes render a mode `0600` override in a private target directory only when published images are selected. The Pi also has a separately created outage-UI override. | Preserve and reconcile both live overrides before migration. Do not treat either as released source or silently drop a higher-priority override. |
| Docker service transition and checkout | The Docker play stops/disables existing native web and, when managed, native ser2net before updating the checkout and invoking Compose. It recursively sets checkout ownership and can seed an absent web config from the example. | Failed checkout, build, pull or startup can occur after services were stopped. Preflight and recovery need a different boundary; review recursive ownership and seed behavior before adopting this path. |
| Native web and ser2net | Native play writes the systemd web unit, optionally writes `/etc/ser2net.yaml`, reloads systemd, then unconditionally restarts web and the managed gateway on every run. | A no-change rerun can interrupt service; the unit passes listener/config/state as CLI and the agent list as environment. Preserve existing config/state and explicitly test transition from Docker. |
| Playback agent | Agent play copies its entry point, dependency manifest and all controller `lib/*.py` into a separate target installation, writes a private token and user unit, and reloads the user manager. When explicitly started, changed files, token, unit, venv or dependencies cause a restart. | Controller source and Pi checkout can differ; copying helpers does not remove extra installed files. Record source identity and preserve actual installation, credentials and ledger before any update. |
| Proxy trust and configuration | Proxy play renders a root-managed Caddy config and system unit, and fetches the public certificate to the **controller** checkout's `docker/secrets` directory. When startup is requested, changes to config, unit or certificate/key ownership cause restart. | Fetching to the controller does not itself populate a separate Docker target's secrets mount. Verify certificate placement, SAN, actual peer/CIDR and trust per layout; do not copy private keys into the web mount. |
| Egress and audio | Egress installs root helpers and service plus user MPD slice/drop-ins, reloads user units, restarts enforcement and configured MPD instances. Audio `init,new` can install/start native ser2net, stop supervision, regenerate units and affect MPD/audio even when staged without playback. | Neither operation is a web-only deployment. Preserve unrelated firewall rules, state and services; require explicit scope and interruption approval. |

#### Generated-file lifecycle review

The following is source-level behavior, not a claim that the current Pi
files have these exact owners or permissions. Where a mode or group is not
specified, existing metadata, umask and Ansible module behavior are
environment-dependent; inspect the actual file before migration.

| Producer and generated asset | Current write/ownership | Active effect and disposition |
|---|---|---|
| Docker play: checkout, `docker/config`, `data`, `logs`, `secrets` | Target checkout is recursively assigned to `deploy_user` and same-named group; bind directories `0755`, secrets directory `0700`. Seed `russound_config.json` is copied only if absent, mode `0644`; existing state is not deliberately replaced. | Git and ownership changes follow stopping native web/gateway. An active container can continue using bind mounts during writes. Preserve exact existing files, account/group and local image; do not recursively change an unrelated tree. |
| Docker play: `.env`, registry override | Target `.env` mode `0640`, deploy user/group, replaced by template; optional private override directory `0700` and file `0600`. | Next Compose `up` may recreate services; current third Pi override is not part of this play (F9). Treat rendered and active configuration separately and verify ordered files before update. |
| Native web: checkout, venv, persistent config/state, unit | Checkout recursively owned by deploy user/group; persistent directories `0700`, venv runs as deploy user; system unit mode `0644`. Config and state are separate durable paths and are not seeded by this play. | Unit is daemon-reloaded; native web is unconditionally restarted. Native ser2net config `/etc/ser2net.yaml` mode `0644` and service are also replaced/restarted if managed. Test custom paths, group and migration across layouts. |
| Agent install: source, venv, secret, state, unit | Source and manifest copied as audio user with `0600`, agent/secret/state/unit directories `0700`; token copied `0600`; `lib/*.py` copies do not delete older extras. Venv and ledger contents have their own runtime metadata. | Copies and token replacement occur even with start false; user manager is reloaded and linger may be enabled. Start true starts or restarts based on registered changes. Active process can retain old token/code; stage separately and validate both versions. |
| Proxy install: directories, certificate/key, Caddyfile, system unit | `/etc/caddy/certs` root:caddy `0750`, Caddy data caddy:caddy `0750`; pair root:caddy `0640`, generated only if neither exists; config root:caddy `0640`, unit root:root `0644`. Public cert fetched to controller secrets directory, then set `0644`. | File writes and certificate generation can happen with start false; system manager reloads. Start true starts or restarts on configuration/unit/pair metadata changes. No separate web-host trust file is created automatically. Preserve key and active trust. |
| Egress install: helpers, slice, drop-ins and service | Root helper directory/files `0755`/`0644`; user slice and drop-ins owned by selected playback user, mode `0644`, directories `0755`; system unit root:root `0644`. | Early approval assertion gates this play, but after that it writes before validating the final unit, reloads the user manager, restarts/enables enforcement, then restarts discovered MPD instances. Explicitly protect unrelated firewall and units and handle failure after partial writes. |
| Audio/MPD setup: PipeWire, WirePlumber, Pulse and MPD files | Audio-owned generated fragments and USB policy scripts have managed headers; Pulse include preserves unrelated text and mode. MPD configs and user units are written as the audio user without a managed-header check; creation mode depends on umask. | Audio install restarts audio services; cleanup uses `try-restart` for affected running services. MPD install reloads manager but does not remove stale units; start/stop/cleanup use broad filename matching (F25). Inspect ownership and identity before applying or deleting. |
| Agent removal | Stops/disables fixed user unit if selected unit file exists; deletes that unit file and reloads manager. | Keeps source, venv, token, ledger, configuration and lingering. A custom unit path can disagree with the actual loaded service (F15); removal is not credential deletion or full rollback. |

### S0 follow-up: logging differences

- The current web implementation defaults to INFO, or DEBUG when debug is
  selected, and emits stdout; a configured web log also rotates at 10 MiB
  with five backups. Protocol audit is a separate file handler, rotating at
  5 MiB with three backups when configured. The Docker-seeded example can
  enable it, as described in F6.
- The current agent and supervisor configure INFO logging and reduce the MPD
  library logger to WARNING; the Pi directs their service output to journald.
  Gateway/container output, proxy and egress logs have separate controls.
  No single profile currently enforces the agreed development/production
  levels across all components.
- A local isolated-import probe found `audiosetup` starts with a DEBUG
  `StreamHandler`: its imported audio helper configures logging before the
  script's later file `basicConfig` call. Importing `mpdsetup` instead created
  the relative debug file with a `FileHandler`. This is import-time behavior
  in the current local source, not verification of every deployed invocation.
  Define and test logging initialization explicitly rather than relying on
  the scripts' apparent `basicConfig` statements.
- The dummy web Compose service does not bind-mount `/logs`, while the web
  image defaults its file log there and declares `/logs` as a Docker volume.
  Compose can therefore create an anonymous volume rather than use the
  hardware stack's host log directory. Anonymous-volume reuse and removal
  depend on the command and deployment lifecycle; verify permissions and
  retention instead of assuming hardware Compose behavior.
- Dummy TUI mode writes an unrotated `.log` alongside its state and also
  sends logs to the TUI; without TUI it uses a stream handler. The separate
  playback observation recorder rotates `observations.jsonl` at 10 MiB with
  ten backups; this is not the protocol-frame audit's 5 MiB/three policy.
  The manual capture TUI uses its own unrotated `russound_capture.log` by
  default. These are source observations, not verified deployed retention.
- Redaction and retention still need per-component verification; do not infer
  privacy from the log level or claim that journal, Docker and rotating-file
  limits control one another.

### S0 follow-up: audio dependency isolation

The current [audio setup play](../../ansible/playbooks/setup-musicpi.yml)
creates a venv only if its Python executable is absent and installs
`python-mpd2` without a version constraint. An existing venv with system site
packages enabled is therefore not replaced or made isolated by a rerun.
The [agent manifest](../../mpdrussoundpi/russound/requirements-agent.txt)
separately lists Flask, Waitress and python-mpd2 without version constraints.

On 2026-11-12, a fresh controller-side macOS Python 3.13 venv with system
site packages excluded could not run `mpdctrl.py --help` because `mpd` was
missing. After installing `python-mpd2==3.1.1` (the observed Pi version),
`--help` succeeded for audio setup, MPD setup, audio readiness, MPD control,
supervisor, playback audit and stream-level measurement. The temporary venv
was removed; no Pi service or configuration was touched. This proves only
argument-parser startup/imports for these commands on the controller, not
Pi architecture, package compatibility, system commands or playback.
The same day, read-only `--help` checks for audio readiness, MPD control,
supervisor, playback audit and stream levels also passed on the Pi using the
existing isolated agent venv and checkout sources, with bytecode writes
disabled. This is not a replacement audio venv or a runtime playback test.

The Python venv does not replace host services and executables. Source
inspection identifies systemd/user D-Bus, `mpd`, `pactl`, and, for USB selector
checks, `pw-dump`; stream measurement separately needs `ffmpeg`. The egress
service uses `nft` and must remain separately managed. S2/S3 must verify
actual package requirements and supported versions rather than assume that
the current inherited system packages are all unnecessary.

### S0 follow-up: CLI action boundaries

These are source-inspection results, not commands executed against the Pi:

| Entry point | Current action and persistence boundary |
|---|---|
| [Web server](../../web/russound_server.py) | Listener/config/state and debug flags govern a long-running process; `--daemon` double-forks and redirects standard streams. Invalid environment integers currently warn and fall back. No deployment or migration is initiated. |
| [Playback agent](../../mpdrussoundpi/russound/playbackagent.py) | Requires config, identity and token path; starts a server and observer. `--operations-db` explicitly enables write operations and opens a persistent ledger; remote binding requires `--allow-remote`. |
| [Audio setup](../../mpdrussoundpi/russound/audiosetup.py) | `--install` and `--cleanup` are mutually exclusive. Install requires config and writes managed audio configuration; cleanup removes owned configuration. Both acquire a setup lock. |
| [Audio readiness](../../mpdrussoundpi/russound/audioready.py) | Takes a required JSON path and waits for configured hardware profile and sinks. It is a readiness check, not an installer; failure exits nonzero. |
| [MPD setup](../../mpdrussoundpi/russound/mpdsetup.py) | Install generates config and units; start/stop/restart and enable/disable act on services; cleanup and kill are disruptive. Flags are not globally mutually exclusive: install may precede a service action, but start/stop/restart exit before enable/disable and therefore suppress them (F20); kill/cleanup take precedence and exit. It requires a positional config path even for cleanup/kill. |
| [MPD control](../../mpdrussoundpi/russound/mpdctrl.py) | Exactly one action is required. Status, route check and config revision are observations; start/pause/stop, set/apply gain and route recovery alter runtime or saved intent. Gain setting requires an explicit output. |
| [Supervisor](../../mpdrussoundpi/russound/mpdsupervisor.py) | `--initialize` writes missing intentions and exits; `--once` initializes then starts/reconciles MPD services once; normal mode continues reconciliation and may run the watchdog. Both flags together select initialization, not reconciliation. |
| [Egress](../../mpdrussoundpi/russound/playbackegress.py) | `--wait` checks existing enforcement. Normal mode requires user and UID, starts the protected slice, validates/applies nftables rules and writes enforcement state; it is not a read-only check. |
| [Audit](../../mpdrussoundpi/russound/playbackaudit.py) and [stream levels](../../mpdrussoundpi/russound/streamlevels.py) | Audit records observations to an output directory, or `--install-service` generates a user unit without starting it. Stream-level measurement reads sources and may write a new explicitly named config; it refuses an existing output or the input path. Neither is a deployment dry run. |

Additional source review on 2026-11-13:

- The agent takes a required positional config, `--agent-id` and
  `--token-file`; defaults to `127.0.0.1:8081` and rejects a port outside
  1–65535 or a remote bind without `--allow-remote`. `--operations-db`
  enables the operations manager; the hidden legacy TuneIn enable flag and
  disable flag conflict if both are set. Treat a token path as sensitive and
  never include token contents in the inventory.
- Audio setup requires exactly one of `--install` and `--cleanup`;
  `--audio-processor` accepts `pulseaudio` or `pipewire`. Installation needs
  a JSON path; cleanup does not. MPD setup's positional JSON argument is
  required even for cleanup/kill, and its actions have the precedence stated
  above; both setup paths can affect real services.
- MPD control requires exactly one action; `--set-gain` also requires an
  output ID. `--status`, `--check-route` and `--config-revision` are
  observational actions; other actions can change playback, gain, routing
  or saved intention. `--output` is optional for other actions.
- Stream measurement defaults to 900 seconds and a -1 dBTP ceiling, rejects
  durations below 10 seconds or nonfinite values, and rejects a positive or
  nonfinite ceiling. Audit defaults to 72 hours, requires a finite positive
  duration and explicit output path, and treats `--once` and
  `--install-service` as mutually exclusive.

### S0 follow-up: earlier local deployment work

The changes reviewed here were uncommitted at the time of the original
inventory and are now included in baseline commit `f03e06a`. They are useful
prior work, not the approved final design or evidence of a safe live
redeployment:

- The registry mode adds explicit image references, a private Compose
  override, `--no-build`, selectable pull policy and optional controller
  response checking. Reuse its separation of pull and build only after
  reconciling first-deployment digest pinning, explicit updates, compatible
  pairs and the actual Pi's additional override. Its current default remains
  target build and its published-image pull policy defaults to `always`;
  neither is the agreed normal-redeployment contract.
- The current published-image task names the base and registry override but
  not the Pi's outage-UI override. A rerun against that layout could therefore
  replace the running outage-fix image with the registry-selected image. The
  explicit two-file versus three-file render above confirms the selection
  difference; no deployment was performed to test replacement.
- The new character-device preflight runs before service changes, and proxy
  work adds multiple allowed CIDRs and Caddy template validation. Retain their
  intent while testing invalid input, changed network layouts and failures;
  syntax and static template tests alone do not prove a live transition.
- Existing deployment tests inspect task structure, conditions and rendered
  arguments. Extend them with real Ansible/Compose rendering, default agent
  parsing, correct platform/image identity and failure-boundary cases during
  implementation; do not assume these tests prove idempotent deployment.
- Preserve and reconcile each part during S4. Do not copy the running Pi's
  ad hoc overrides into the new design without checking identities and state.

### S0 detailed matrix: web and container inputs

Source of truth for the following rows is the local
[web server](../../web/russound_server.py),
[config parser](../../web/config_types.py),
[agent registry parser](../../web/playback_status.py),
the former hardware `docker-compose.yml`, and
the former native `russound-web.service.j2` template.
Unless noted, runtime environment is read at process start; changing it
requires restarting the native service or recreating the container. `CLI > env`
means an explicitly supplied CLI argument, not an Ansible or Compose override.
Values may be sensitive even when not credentials; do not expose arbitrary
environment values in a future UI. Target mappings remain decisions, not
implemented behavior.

| Input / owner | Current source, default and validation | Writable source, consumer and compatibility test |
|---|---|---|
| Web `--host` / `RUSSOUND_WEB_HOST` | CLI > nonblank environment > `0.0.0.0`; blank env ignored. Native unit passes CLI; Compose fixes container host to `0.0.0.0`. | Deployment operator; web listener. Test bind/exposure and CLI/env conflicts; do not confuse host published port with container port. |
| Web `--port` / `RUSSOUND_WEB_PORT` | CLI integer > valid env port 1–65535 > `8000`; invalid env warns and falls back; CLI uses integer parsing. Compose fixes internal port to 8000. | Operator; web listener and image healthcheck. Test blank, malformed, boundaries, conflicts and healthcheck agreement. |
| Web `--config` / `RUSSOUND_CONFIG` | CLI > nonblank env > no config path. A missing/invalid config enters setup-required state; saves require a path. Hardware Compose and image use `/config/russound_config.json`; native unit passes a persistent path. | Config editor/API writes saved web JSON atomically; preserve on migration. Test missing, unreadable, invalid and writable files and native/container path mapping. |
| Web `--state` / `RUSSOUND_STATE` | CLI > nonblank env > `web/russound_state.json`; Compose and image use `/data/russound_state.json`; native unit uses a separate state directory. | Web persists observed state; preserve it independently of config. Test mount ownership and migration with writers stopped. |
| Web `--debug` / `RUSSOUND_DEBUG` | Flag or env; blank false; env is true only for `1`, `true`, `yes`, `on` case-insensitively. Other values become false. Debug enables DEBUG logging; Waitress is used in both modes. No CLI false override exists. | Operator; process startup. Test false/true/malformed, conflicts, logging, Waitress and no Flask debugger. |
| `--waitress-threads` / `RUSSOUND_WAITRESS_THREADS` | CLI integer > env integer > 24; malformed env warns/falls back. Actual thread count is at least 5. | Operator; Waitress and stream limit. Test blank/noninteger, low values and precedence. |
| `RUSSOUND_MAX_EVENT_STREAMS` | Env integer, default `0`/blank means derived from thread count; malformed warns/falls back. Nonzero value is capped against `threads - 4`. | Operator; event-stream admission. Test zero, negative, malformed and cap behavior before changing validation. |
| `RUSSOUND_TCP_USER_TIMEOUT_SECONDS` | Env integer default 30; malformed warns/falls back; positive value sets socket option where supported, nonpositive omits it. | Operator; Waitress connections. Test platform support, zero, negative and invalid values. |
| `RUSSOUND_ALLOWED_HOSTS`, `RUSSOUND_ALLOWED_ORIGINS` | Comma-separated env lists; blank gives empty lists. Empty allowed-host list accepts any Host. | Operator/security policy; web request guards. Test proxy and origin/host behavior, blank entries and denied requests. |
| `RUSSOUND_TRUSTED_PROXY_COUNT` | Env integer default 0; malformed warns/falls back; positive enables ProxyFix for that many hops. | Operator/security policy; web request attribution. Test zero, negative and proxy spoofing; never broaden trust silently. |
| `RUSSOUND_LOG_FILE` | Nonblank env enables a file in addition to stdout; missing/blank means stdout only. Web rotation is 10 MiB with five backups. Image defaults to `/logs/russound-web.log`; hardware Compose sets it, dummy inherits the image default. | Operator; web logger. Preserve logs/mount permissions; test rotation, failure, sensitive-data redaction and interaction with container log rotation. |
| `RUSSOUND_BACKEND_HOST`, `RUSSOUND_BACKEND_PORT` | Each valid nonblank env override independently wins over saved endpoint; malformed/out-of-range port warns and is ignored. Either valid override forces TCP; missing fields use saved endpoint/defaults (`127.0.0.1:6666`). | Operator/deployment for env, editor for saved config; connector consumes effective endpoint. Test serial-to-TCP switch, host-only/port-only, invalid values and stored settings surviving override removal. |
| `RUSSOUND_PLAYBACK_AGENTS` | Blank/unset means no agents; nonblank must be a nonempty JSON array with unique IDs, URL and token-file reference; CA path optional for HTTPS. Non-loopback HTTP is rejected. JSON/validation errors reject startup. | Operator controls deployment registry, not editor; web reads file references at runtime. Secret files and URLs need restricted handling. Test absent, `[]`, malformed, duplicate, TLS and token permissions. See finding F1 below. |
| Web `--daemon` | CLI-only opt-in, POSIX double fork, no env counterpart. | Direct/manual use; test systemd/container lifecycle and decide whether to retain. |

#### Compose, gateway and dummy-specific settings

Compose `${VAR:-default}` treats unset **and empty** as default; it is a
deployment interpolation layer, not automatically a process environment.
Shell variables can override project `.env` for interpolation. Explicit Compose
`environment` then supplies selected values to each process; image `ENV`
supplies other defaults. Ansible variable precedence and generated `.env` are
separate layers. Do not put secret contents into `.env`.

On 2026-11-12, a controller-only `docker compose config --format json`
probe with a temporary non-secret env file verified that the hardware web
published port defaults to 8000 for unset and empty values, that an env-file
value is used, and that a shell value overrides it. Rendered hardware Compose
passes the agent variable and dummy Compose does not; both fix the internal
web port at 8000. No containers were started. This covers these specific inputs,
not every Compose or Ansible precedence case.

| Input / owner | Current default and consumer | Migration/test requirement |
|---|---|---|
| `RUSSOUND_UID`, `RUSSOUND_GID`, `RUSSOUND_SERIAL_GID` | Compose user IDs default `10001:10001`, serial group `20`; Ansible resolves user IDs and serial device GID. | Preserve access to config/state/logs and serial device; test nondefault IDs and missing permissions. |
| `RUSSOUND_PUBLISH_PORT` | Compose host web port defaults to 8000; maps to fixed container port 8000. | Test host collisions and changes separately from runtime listener. |
| `RUSSOUND_SERIAL_DEVICE`, `RUSSOUND_SERIAL_OPTIONS`, `RUSSOUND_SER2NET_BIND`, `RUSSOUND_SER2NET_PORT`, `RUSSOUND_SER2NET_CONNECTOR` | Gateway entrypoint defaults to `/dev/ttyUSB0`, `19200n81`, `0.0.0.0`, `6666`, generated serial connector. Hardware Compose passes the first three and fixes gateway port to 6666; it does not pass connector. Entry script warns if device missing/inaccessible. | Operator/hardware; Compose maps device and group. Test generated config, device, group, in-network reachability, actual controller and no-controller case separately. `CONNECTOR` is a direct-entrypoint option, not a Compose interpolation input. |
| `RUSSOUND_DUMMY_PUBLISH_PORT`, `RUSSOUND_DUMMY_HOST`, `RUSSOUND_DUMMY_PORT`, `RUSSOUND_DUMMY_STATE`, `RUSSOUND_DUMMY_TUI`, `RUSSOUND_DUMMY_TMUX_SESSION`, `RUSSOUND_DUMMY_DEBUG` | Dummy Compose publishes 6666 by default, fixes host/port/state; TUI defaults `true`, session `dummy`, debug `false`. Entrypoint seeds absent state; only literal `true` enables debug/TUI. | Development only; preserve dummy state and test absent/empty/false inputs. Dummy web mounts differ from hardware web; verify intended logging and secrets before unifying. |
| Compose `RUSSOUND_ALLOWED_HOSTS`, `RUSSOUND_ALLOWED_ORIGINS`, `RUSSOUND_TRUSTED_PROXY_COUNT`, `RUSSOUND_PLAYBACK_AGENTS`, backend and debug | Hardware Compose passes these explicitly; dummy Compose passes backend/debug but not agent list or host/origin controls. | Test rendered service environment, not just `.env`; test each supported layout for missing values and secret mounts. |

### S0 detailed matrix: persisted configuration and native services

Defaults and validation below describe current sources, not approved future
behavior. Restart/reload needs depend on the consumer; migration must preserve
both stored values and the version that understands them.

| Group / writable source | Current shape, validation and consumers | Mapping and compatibility coverage |
|---|---|---|
| Web `backend` JSON | `host`/`port` or `device`/`baud`; default endpoint `127.0.0.1:6666`, baud `19200`; poll interval default 2 s, minimum 1 s; optional `protocol_audit_log_file`. Host/device must be nonempty; port 1–65535, baud 1–4000000. | Editor/API write config; server/connector read it. Preserve unused serial fields during overrides and test endpoint changes, config saves and invalid values. Audit is opt-in separate file, 5 MiB plus three backups; assess protocol data sensitivity. |
| Web `controllers`, `zones`, `inputs`, `shortcuts` JSON | Lists of objects; controller/input IDs and zone-related numbers 1–6, controller ID/zone count required, controller/input IDs unique; input playback binding requires exactly valid `agent_id`/`output_id`. Names are strings, `enabled`/`visible` booleans. Editor supplies defaults for absent slots. | Editor/API own changes; backend and UI consume. Migrate stable IDs, bindings, labels, visibility and shortcuts; test round-trip and inactive/saved fields. `notes` are file-only and omitted from API responses. |
| Pi `outputmappers.audio_devices` | Admin JSON: device `name`, `card-profile`, `sinks`; sink `name`, `master_channel_map`, optional `channel_map` (default `[ FL FR ]`), `remix`, `sink_description`; optional USB `hardware-selector` has exactly `vendor`/`product`. Selector requires `analog-surround-51`, unique mappings and stereo channel pairs. | Audio setup generates PipeWire/PulseAudio/WirePlumber configuration; agent/supervisor also validate hardware. Preserve identifiers, maps and generated ownership; test missing/ambiguous USB hardware and no route substitution. |
| Pi `outputs` | Required nonempty list: `id`, `label`, `port`, `sink`, optional `default_station`, `default_gain_db` (default 0); IDs, ports and sinks unique; port 1–65535 and sink must exist. | Admin JSON; MPD setup, agent, supervisor and CLI consumers. Preserve IDs and ports; test invalid/duplicate/missing fields, defaults and state migration. |
| Pi `stations` | Defaults to `[]`, max 20 unique keys; URL station has key/name/type/url, TuneIn has key/name/type/station_id with matching TuneIn key. Names are printable and at most 120 characters. | Seeds favorites, not an ongoing override of saved state. Do not reveal raw stream URLs. Test seed versus existing runtime choices, unavailable streams and migration. |
| Pi `watchdog` | Optional object; defaults: enabled true, probe 10 s, stall 120 s, unresponsive 3, restart limit 3, window 1800 s, grace 90 s, escalation 3600 s. Unknown fields rejected; integer counts 1–100, seconds finite and bounded 1–86400 (grace may be zero). | Supervisor reads; preserve current tuning, test every field, absent, malformed and unknown fields and cross-version parser support. |
| Playback state and operations ledger | `${XDG_STATE_HOME:-~/.local/state}/mpdrussoundpi/playback.json` stores favorites, per-output source, cached URL, gain and intended playback; agent operations ledger is private SQLite when enabled. Config seeds missing state, not vice versa. | Backend/agent/CLI/supervisor are writers or consumers. Snapshot SQLite online or stop writers; test same- and cross-version restore and schema compatibility. Treat state and backup as sensitive. |
| Agent CLI | Required JSON path, `--agent-id`, `--token-file`; default `--host 127.0.0.1`, `--port 8081`. `--operations-db` enables operations; `--disable-tunein-selection` disables that operation; remote binding requires `--allow-remote` and secure transport. | Ansible renders unit; agent validates ID/token and port. Preserve token separately, test read-only/operations mismatch with proxy and remote bind refusal. |
| Audio and MPD management CLI | `audiosetup.py` install/cleanup and optional processor override; `mpdsetup.py` install/start/stop/restart/enable/disable/cleanup; `mpdctrl.py` output/actions/debug/gain; supervisor init/once. See respective source `--help`. | Administrative operations, not interchangeable configuration sources. Record effects and require explicit consent; test idempotence, service isolation and interruption boundaries. |
| Proxy/egress | Proxy play requires hostname and allowed CIDR, default 8443; optional extra CIDRs and operations, existing certificate/private-key pair. Egress play requires explicit activation and resolver IPs and applies MPD cgroup rules. | Ansible renders root/system units and Caddy config. Preserve keys, trust and firewall ownership; test NAT CIDRs, denied sources, TLS and readiness independently. |

#### Field-level check: web saved configuration

These are checks of the local source, not a live configuration export.
File loading uses [`validate_russound_config`](../../web/config_types.py);
the [editor save handler](../../web/russound_controller.py) adds stricter
requirements. Empty strings below mean actual strings, not missing keys.
Config is operator-owned through the file or editor; state is separately
persisted. Compatibility checks are proposed for implementation, not passes.

| Field | File-load behavior and default | Save/effect and migration check |
|---|---|---|
| Root object; `backend` | Root must be a nonempty object. Missing `backend` uses default endpoint; an empty object is accepted. Unknown keys are not rejected by this validator. | Initial editor save requires a backend. Preserve unknown fields until a deliberate compatibility decision; test missing/empty/invalid files. |
| `backend.host`, `backend.port` | Optional nonblank string and integer 1–65535 respectively. Both are needed for a saved TCP endpoint; an otherwise valid partial host or port falls back to `127.0.0.1:6666` unless a device is selected. Environment overrides are independent. | TCP editor save requires both. Verify partial config, CLI-independent environment overrides, blank/invalid inputs and restoring the saved endpoint after removing overrides. |
| `backend.device`, `backend.baud` | Device, if present, must be nonblank; baud, if present, must be an integer 1–4000000; default baud is 19200. When device is present the connector uses serial. | Editor save selects serial using device and removes saved host/port; selecting TCP removes device. Test mode transitions and retention of non-editor fields. |
| `backend.poll_interval_seconds` | Optional finite number >= 1; default 2 seconds. Bool, nonnumeric, nonfinite and lower values are rejected. | Editor save applies interval to the watcher without restarting; test boundaries and immediate application. |
| `backend.protocol_audit_log_file` | Optional nonblank path string; missing disables this separate audit logger. The shipped example **does** specify a path; see F6. | A saved path change triggers backend reconnection; ensure writable destination, rotation, privacy and explicit policy. The config editor's connection fields do not provide an audit-path field. |
| `controllers[].id`, `.zone_count` | Both required integer 1–6, controller IDs unique; absent collection defaults to empty. | Initial editor save requires at least one controller. Test topology changes and zone membership; controller scope changes do not require process restart. |
| `zones[].controller`, `.zone`, `.keypad_id` | When present, each integer 1–6; file validator does not require these fields or check duplicate addresses. Some consumers default missing addresses/keypad to 1. | Editor slots must be unique, in a configured controller's zone count; keypad defaults to 1. Removing zones prunes shortcut targets. Test duplicate/missing/out-of-scope values and preserved physical addresses. |
| `zones[].name`, `.enabled`, `.visible` | Name, when present, must be a string; booleans, when present, must be boolean. Zone model defaults enabled/visible to true. | Editor uses slot `enabled` to include/remove zones; blank name becomes a generated name, visibility is persisted. Test disabled/hidden zones and their state without silently changing hardware scope. |
| `inputs[].id`, `.name`, `.enabled` | ID required integer 1–6 and unique; optional name is a string and enabled is boolean. Missing list defaults empty. | Editor uses enabled slots to add/remove inputs and falls back to `Source <id>` for a blank name. Test identity and disabled bindings/shortcut references. |
| `inputs[].playback_output.agent_id`, `.output_id` | Optional object must contain exactly both nonempty IDs matching `[a-z][a-z0-9_-]{0,63}`; missing binding is allowed, null binding is not valid in a stored input. | In a save request, null removes the binding; omission preserves it. Verify mapping round-trip, nonexistent agents/outputs and removal without exposing credentials. |
| `shortcuts[].id`, `.name`, `.source`, `.zone_addresses` | File validator checks optional name type, source integer 1–6 and address list entries' controller/zone integers 1–6. It does **not** require a nonempty ID/name, source, targets or uniqueness. | On an explicit shortcut save, IDs and names must be nonblank, IDs and target addresses unique, source enabled and at least one enabled target. Omission preserves shortcuts but prunes removed zones; `[]` deletes them. Test both validation paths. |
| `notes` | Example contains file-only explanatory strings; not a typed runtime setting, and omitted from config API responses. | Preserve across config saves/migration; do not make explanatory example notes into trusted effective values. |

#### Field-level check: Pi admin configuration and runtime state

The [Pi parser](../../mpdrussoundpi/russound/lib/outputconfig.py) reads the
admin file; [audio identity](../../mpdrussoundpi/russound/lib/audioidentity.py),
[sink generation](../../mpdrussoundpi/russound/lib/sink.py),
[station parser](../../mpdrussoundpi/russound/lib/stations.py) and
[state parser](../../mpdrussoundpi/russound/lib/playbackstate.py) impose
additional rules. Agent/supervisor read config, while audio/MPD setup generates
files and can restart services; regenerate/restart only with explicit approval.

| Field | Current validation/default and consumer | Compatibility/restart check |
|---|---|---|
| Root `outputmappers.audio_devices` | Both objects/list are required; each device must provide a list of sinks and every sink a string name. Root `streams` legacy format is rejected; arbitrary other top-level keys are not rejected by this parser. | Preserve unknown keys and old data until a migration policy is agreed; validate with **each** setup consumer, not parser alone. |
| Device `name`, `card-profile`, `sinks` | Audio setup requires name/profile strings and a sink list. When any USB selector is configured, safe unique names and unique sink names across devices are required. No runtime environment override. | Preserve names/profile and test selected and legacy device formats; audio setup regenerates configuration and may restart audio. |
| Device `hardware-selector.vendor`, `.product` | Selector is optional; when present it requires exactly both IDs, integer 0–65535 or 1–4 hexadecimal digits with optional `0x`. Requires `analog-surround-51`; duplicate selectors rejected. | Test absent, malformed, missing/duplicate hardware and ambiguous cards. A change affects generated WirePlumber policy and hardware routing. |
| Sink `name`, `master_channel_map`, `channel_map` | Audio generator requires name and both channel-map strings; `channel_map` defaults to `[ FL FR ]`. For USB selectors name must be safe/unique, input exactly stereo and master map one of FL/FR, FC/LFE, RL/RR with no overlaps. | Parser and generator validation differ for legacy devices; test each path and actual routing before applying changes. Preserve sink identity and channel placement. |
| Sink `remix`, `sink_description` | Optional strings or null. `remix` is parsed but currently not used by the generator; `sink_description` is optional display text. | Test missing/empty/invalid values and preserve existing fields; do not claim changing `remix` changes audio behavior. |
| `outputs[].id`, `.label`, `.port`, `.sink` | All required: ID matches `[a-z][a-z0-9_-]{0,63}`, label nonblank/printable, port integer 1–65535, sink a configured name. ID, port and sink each unique; unknown output fields rejected. | ID owns state and unit identity. Test missing/invalid/duplicates and old-to-new mapping; MPD port/sink changes require regenerated units and controlled restart. |
| `outputs[].default_station`, `.default_gain_db` | Optional station key must exist in station list; absent/null means no station. Gain defaults 0, finite numeric <= 0, bool rejected. | Seeds only missing runtime output state: existing station/gain/intention win. Test reset/migration and avoid overwriting user choices. |
| `stations[].type`, `.key`, `.name` | List defaults empty, maximum 20, unique keys. `url` keys match `[a-z0-9][a-z0-9_-]{0,63}`; TuneIn keys must be `tunein:<station_id>`. Names nonblank, printable, max 120 characters. | Test duplicates, malformed values and existing favorites. Changing seed does not rewrite saved favorites. |
| `stations[].url`, `.station_id` | URL type requires exactly key/name/type/url; HTTP(S), valid host/port, no whitespace/fragment, <=1200 chars. TuneIn requires exactly key/name/type/station_id, ID `s` plus 1–12 digits. Missing/extra keys rejected. | Treat URL and cached resolution as sensitive; test URI validation and public-only egress separately. Never expose raw URLs in the inventory. |
| `watchdog.enabled` | Optional boolean, default true; whole object absent or null uses defaults. Unknown object keys rejected. | Test explicit false, null, malformed and compatibility with the deployed parser version. |
| `watchdog.probe_interval_s`, `.stall_threshold_s`, `.restart_window_s`, `.restart_grace_s`, `.escalation_window_s` | Defaults 10, 120, 1800, 90, 3600 seconds respectively. Finite numeric 1–86400; grace permits 0. Bool rejected. | Test missing, boundaries and nonfinite values; supervisor snapshots watchdog config at startup, so apply changes with controlled supervisor restart. |
| `watchdog.unresponsive_probes`, `.restart_limit` | Defaults 3 each; integer 1–100, bool and float rejected. | Test boundaries and restart behavior; preserve existing tuning and coordinate with watchdog follow-up. |
| Runtime `favorites`, `outputs` | Missing state file/keys seed from admin config; explicit empty favorites remain empty. Top-level unknown state keys are rejected; unknown output IDs are ignored and omitted on next write. | Backup and preserve state independently; test migration with removed/new outputs and do not infer intended play from config once saved. |
| Runtime output `station`, `resolved_url`, `managed_song_id`, `gain_db`, `intention` | Exactly these five keys required. Station null requires `stop`; intention is `play`, `pause` or `stop`; URL may be null or valid source URL; song ID null or ASCII digit string <=10 chars; gain finite <=0. | Do not coerce malformed old state to a fresh default. Test schema downgrade and preservation of selection, cached URL and intention with writers stopped. |

#### Generated files and logging checked

| Owner | Current output and caution |
|---|---|
| Docker play | Target `.env`, optional registry override from subsequent local work, config seed only if absent, and bind directories for config/data/logs/secrets. A template rerun replaces `.env`; it is not the authoritative input. |
| Audio setup | User PipeWire `pipewire.conf.d/remapped-sinks.conf`, WirePlumber `wireplumber.conf.d/card-modes.conf`, optional managed USB scripts, or PulseAudio managed fragment/include. Ownership headers and symlink checks protect audio files. |
| MPD setup | Per-output `~/.config/mpd/mpd_<id>.conf`, user `mpd_<id>.service`, meta-control and supervisor units. Egress additionally installs per-unit drop-ins and `mpdstreams.slice`; preserve those during regeneration/rollback. |
| Native agent/proxy/egress | Agent user unit/token and optional operation database; proxy system unit, Caddy config and TLS pair; egress system unit/helpers and transient scope state. Backup private files separately and avoid logging their contents. |
| Logging | Web INFO/DEBUG to stdout plus optional 10 MiB/five-backup file; protocol audit is a distinct 5 MiB/three-backup file. `audiosetup.py` and `mpdsetup.py` attempt to configure a relative `russound_debugging.log` without rotation; the import-order probe above shows the audio script's file setup does not take effect in that case. Neither Compose file declares a container log driver/limit; Docker and journal retention depend on host settings. |

Field checks on 2026-11-12: the selected-interpreter snippet confirmed that
file validation accepts an empty backend, a host-only backend (resolved to
the default endpoint), an incomplete shortcut and an unknown web root field;
it rejects an empty root. The Pi parser accepted an unknown top-level field
and null watchdog, but rejected an unknown watchdog field and a missing output
label. These checks do not prove every consumer accepts the same input.
Focused existing tests for output config, playback state, audio identity, web
controller and backend passed: **117 tests**. The test run also emitted
unclosed-socket `ResourceWarning`s during connection-failure tests; those
warnings were not investigated or fixed in this inventory.

### S0 detailed matrix: Ansible entry points

These are play-level defaults and required inputs, not a complete Ansible
precedence specification. Inventory host variables, facts, play variables,
`set_fact`, extra vars, tags and generated files are separate mechanisms;
do not assume editing a generated file is durable. A changed input can cause
recreation or a service restart. Sources:
the former Docker (`deploy-russound-docker.yml`) and
native web (`deploy-russound-web.yml`) plays,
[agent](../../ansible/playbooks/deploy-playback-agent.yml),
[proxy](../../ansible/playbooks/deploy-playback-agent-proxy.yml),
[egress](../../ansible/playbooks/deploy-playback-egress.yml),
[audio](../../ansible/playbooks/setup-musicpi.yml), and
[agent removal](../../ansible/playbooks/remove-playback-agent.yml).

| Inputs / owner | Defaults and downstream effect | S2 migration and verification |
|---|---|---|
| Inventory `musicpi`, `ansible_host`, `ansible_user`, `ansible_python_interpreter` | Inventory selects target and SSH account; plays target the `musicpi` host pattern and commonly set `/usr/bin/python3`. Deploy user defaults to Ansible user. | Support non-default inventory, separate controller/target paths and missing host errors; never equate a localhost syntax check with a matched target deployment. |
| Web `repo_url`, `repo_version`, `app_dir`, `deploy_user` | Both web plays default to repository URL and `main`. Docker paths derive from `ansible_user`, while native paths derive from `deploy_user` and assume `/home/<user>`; native also uses the user name as its group (F14). Git updates are not forced. | Resolve the account, home and primary group consistently; test distinct SSH/deploy users, explicit paths, dirty checkout refusal, missing revision and state preservation. |
| Docker `russound_publish_port`, `russound_serial_device`, `russound_serial_options`, `russound_ser2net_bind`, `russound_manage_serial_gateway` | Defaults 8000, `/dev/ttyUSB0`, `19200n81`, `0.0.0.0`, true; device and group feed generated `.env`. Gateway management false requires an external backend hostname. | Test device/group validation before service changes, external gateway and native service ownership; earlier baseline and subsequent missing-device behavior differ. |
| Docker `russound_backend_host`, `russound_backend_port`, `russound_debug`, `russound_playback_agents` | Defaults `russound-ser2net`, 6666, false, `[]`; generated `.env` supplies Compose. `russound_uid`, `russound_gid`, `russound_serial_gid` are resolved facts. | Test actual generated/parsed environment including F1, override precedence and external gateway. Do not persist credentials in generated env. |
| Docker `russound_use_published_images`, `russound_web_image`, `russound_ser2net_image`, `russound_published_pull_policy`, `russound_registry_override_dir`, `russound_verify_backend` | Defaults false, empty image references, pull `always`, private override directory, false. When enabled, explicit image refs and `--no-build` are used; optional rooms API check tests controller response. | Added after the earlier baseline. Review before reuse; test missing ref, `always`/`missing`/`never`, failed pull, compatible image pair, target architecture, zero unintended builds and staged verification. |
| Native web `russound_host`, `russound_port`, `russound_playback_agents`, `russound_manage_serial_gateway` | Defaults `0.0.0.0`, 8000, `[]`, true; systemd unit passes listener via CLI and agent JSON via environment. Native ser2net is optionally installed and restarted. | Test exact unit-to-runtime behavior including F1 and nondefault host; avoid unexpectedly modifying another gateway. |
| Agent `playback_agent_id`, `playback_agent_port`, `playback_agent_start`, `playback_agent_enable_operations`, `playback_agent_enable_tunein_selection`, `playback_agent_token_source` | Defaults `musicpi`, 8081, false, false, true; token source is required controller-side private file. Installation can occur without activation; unit listens on loopback. The operations flag's nonempty string `false` is truthy in the service template; see F11. | Test absent/invalid token, actual Ansible boolean inputs, read-only vs operations, recreation and preservation of token/ledger. Cross-host credential rotation is deferred. |
| Agent `playback_agent_home`, `playback_agent_dir`, `playback_agent_venv`, `playback_agent_secret_dir`, `playback_agent_config`, `playback_agent_state_dir`, `playback_agent_unit_dir`, `playback_agent_source`, `playback_agent_environment` | Derive from audio user's home and controller checkout. Paths are asserted absolute/systemd-safe, but the service and task environments and unit directory can diverge (F15). | Test non-default account/path, controller source identity, existing user bus and actual loaded unit; do not capture token contents in logs. |
| Proxy `playback_proxy_hostname`, `playback_proxy_allowed_cidr`, `playback_proxy_additional_allowed_cidrs`, `playback_proxy_port`, `playback_agent_port`, `playback_proxy_start`, `playback_proxy_enable_operations` | Hostname and primary CIDR required; port 8443, agent 8081, additional CIDRs `[]`, start false and writes false. The operations flag's nonempty string `false` is truthy in the Caddy template; see F11. Additional CIDRs are in the baseline. | Test SAN/CIDR validation, Docker NAT source and both agent/proxy write gates, including typed and string inputs; keep old access until explicit cutover. |
| Proxy `playback_proxy_caddy_binary`, `playback_proxy_caddyfile`, `playback_proxy_unit`, `playback_proxy_certificate`, `playback_proxy_private_key` | Point to system Caddy binary, unit/config and certificate pair; play requires Caddy and checks the pair. An overridden unit path can disagree with the fixed activation name (F16). | Preserve and privately back up key/trust; test unit identity, existing and absent pair, renewal boundary, permissions and isolated rollback. |
| Egress `playback_egress_activate`, `playback_egress_resolvers`, `playback_egress_user`, `playback_egress_install`, `playback_egress_source` | Activation defaults false; the play checks resolver input is defined and nonempty but does not check it is a list of IPs; see F12. Default user `musicpi`; source copied from controller. | Validate typed lists and IP literals before target changes; test empty/invalid resolvers, missing nftables, existing unrelated firewall rules and MPD interruption; require explicit approval for activation. |
| Audio `mpd_russoundpi_repo`, `mpd_russoundpi_dest`, `mpd_russoundpi_scripts`, `mpd_russoundpi_config`, `venv_dir`, `mpd_russoundpi_update_repo`, `mpd_russoundpi_start_playback` | Target checkout/config and venv derive from audio user's home; update and start default true. `init,new` staging guide sets start false but also manages native ser2net (F13); `update` requests OS upgrades. | Test source matching, persistent calibrated config, tagged scope and stop/start effects; staging still affects audio and gateway services and must not be presented as no-impact. |
| Agent removal `playback_agent_unit_dir`, `playback_agent_environment` | Removes the agent unit after stopping/disabling it; retains files and credentials. | Test absence/idempotence, private assets and unrelated services. Removing a unit is not complete data deletion or rollback. |

#### Per-parameter validation and implementation disposition

The following inventory covers every play-level declaration in the seven
current plays, plus their externally supplied required inputs and generated
facts. Rows describe source inspection unless a finding records a probe.
They are not assertions that arbitrary malformed values were executed. An
empty string, null and a wrong type can produce different Ansible/module
errors: where no explicit early check exists, the precise downstream error is
**unverified**, not a promised fallback.

Common current precedence: these play `vars` beat same-named host inventory
variables; extra vars beat play vars and facts. `default(...)` without its
second boolean argument substitutes only an undefined value, not an empty
string. Required values with no play default can come from inventory or extra
vars. Generated `set_fact` values and standard Ansible connection variables
have their own precedence; an extra var can even override a generated name.
The tested sample and its limits are in F8. Native CLI/runtime, Compose shell
interpolation, `.env`, image environment and JSON config are distinct later
layers, not repetitions of Ansible precedence.

**Target conventions:** absence uses the relevant documented default;
optional blank is unset; required blank, null where unsupported, wrong type
and invalid syntax produce a field-specific error before mutation. Boolean
inputs must be normalized once from typed booleans and explicitly documented
CLI spellings, not generic truthiness. Ports are integral, not booleans,
1–65535 (proxy's existing unprivileged policy 1024–65535 unless explicitly
redesigned). Existing state and secrets are never rewritten to make a value
valid. Every row below inherits those rules; select focused tests for
representative missing, malformed, conflict and boundary cases, particularly
where known findings affect safety or compatibility. Treat
path and host text as potentially sensitive; report the field and source
without printing credentials, raw URLs or secret contents.

| Parameter(s) | Current handling of absence, blank and invalid input | Smallest understandable rework / test |
|---|---|---|
| `ansible_host`, `ansible_user`, inventory host pattern `musicpi` | Inventory supplies address/user; `musicpi` selects hosts in all plays. No play asserts that selection matched a host. Empty connection values and unreachable hosts are left to Ansible. | Display matched target and SSH identity; fail on zero or ambiguous matches before apply. Test inventory aliases, blank identity and unreachable host without mutating a target. |
| `ansible_python_interpreter` | Web/agent/remove plays define `/usr/bin/python3`; audio/proxy/egress rely on Ansible selection. Agent and audio venv creation separately hardcode `/usr/bin/python3`. No common preflight checks agreement. | Make interpreter choice an advanced per-host input, validate target and venv interpreter separately and report both. Test absent, bad path, wrong runtime and override precedence. |
| `deploy_user` | Docker/native default to `ansible_user` if undefined, otherwise `musicpi`; blank is not defaulted. Account/group lookup or file/systemd operations fail later; Docker assumes same-named group for files. | Resolve non-root account, actual home, UID and primary group once (F14); reject missing account and unsafe identity before ownership changes. Test distinct login/account/group and unusual home. |
| `app_dir` | Docker derives from SSH user, native from deploy user, both assuming `/home`; explicit blank/wrong path has no early check. Both recursively change ownership. | Derive from selected account or accept an explicit validated target path; no recursive ownership on an unreviewed tree. Test differing users, nonstandard homes, unsafe paths and existing data. |
| `repo_url`, `repo_version` | Both web plays default to public Git URL and `main`. No blank, credential-bearing URL, revision or availability preflight; Git runs after package/service operations. | Keep these advanced native/source inputs, not image selectors; reject malformed/blank and embedded credentials, resolve an identified revision before disruption, refuse dirty source. Test offline/missing revision and explicit overrides. |
| Native `venv_dir`, `russound_config_dir`, `russound_state_dir` | Derive from assumed deploy-user home; no early blank/path/systemd-quoting checks. Native unit passes paths as unquoted command fields; venv creation is command-string based. | Derive from resolved account, validate path safety and consumer permissions, quote arguments correctly and preserve existing config/state. Test spaces, percent signs, traversal, missing and unwritable paths with disposable units. |
| `russound_host` | Native default `0.0.0.0`, inserted unquoted in `ExecStart`; no play assertion for blank, address or type. Probe remains loopback even for another bind (F18). | Expose a bind only for native web, validate it and derive a usable probe; fail on incompatible/unreachable binding before restart. Test wildcard, loopback and nondefault bind. |
| `russound_port`, `russound_publish_port` | Native port and Docker host-published port default 8000, but neither play explicitly checks type/range before changes. Native passes raw CLI; Docker writes raw `.env` then probes that value. Runtime CLI parses an integer; Compose validates its own published mapping. | Show these as one web access choice appropriate to the selected mode; reject non-integral, blank and out-of-range values before apply. Test boundaries, collisions, overridden shell environment and probe agreement. |
| `russound_manage_serial_gateway` | Default true in both web plays; conditions use Ansible `bool`. String `false` works there, but invalid spellings depend on Ansible version and are not explicitly rejected. Native installs/restarts ser2net; Docker stops native gateway only when true. | Replace operator-facing boolean combinations with one gateway placement choice; map compatible legacy values explicitly and reject ambiguous spellings. Test managed/external scope and preservation of unrelated services. |
| `russound_serial_device` | Docker defaults `/dev/ttyUSB0`; managed mode checks it exists and is a character device, not ownership/access or safe serialization; external mode skips the stat. Blank/invalid paths fail stat/assert, but exact module errors are unverified. | Require safe absolute character-device path only for managed gateway; verify intended process access and exclusivity before disruption. Test absent, wrong type, permissions, unplugged device and ignored/contradictory external settings. |
| `russound_serial_options`, `russound_ser2net_bind` | Defaults `19200n81`, `0.0.0.0`; written unquoted into `.env`, consumed in generated ser2net YAML. No play syntax, escaping or reachability validation; empty Compose values revert to defaults. | Offer serial settings only for managed gateway; validate supported connector/bind values, serialize safely, check web reachability and no unintended exposure. Test whitespace, quoting, malformed options and listener scope. |
| `russound_backend_host`, `russound_backend_port` | Docker defaults internal hostname/6666; external assertion only excludes the internal hostname. Blank host passes (F21); blank port is passed to Compose and runtime treats it unset. Nonblank invalid port is warned and ignored by runtime, potentially masking saved settings. Native play has no corresponding variables. | Derive endpoint from placement; require a nonblank external host and validate its port, with 6666 as the proposed default. Display effective value/source and preserve saved JSON. Test F5/F17/F21 and actual parser with blank, partial, serial, malformed and saved TCP configurations. |
| `russound_debug` | Docker default boolean false, rendered with `lower` into `.env`; runtime recognizes only `1/true/yes/on` as true and treats other nonblank strings as false. Native play offers no equivalent. | Map documented legacy input to the chosen profile without silently accepting misspellings; preserve existing debug behavior until migration. Test actual Compose and native process settings and explicit CLI false precedence. |
| `russound_playback_agents` | Both web plays default to an actual `[]`. S0 found that templates encoded it as JSON `[]`, rejected by runtime (F1); the first implementation iteration renders it blank. Non-list or malformed entries still have no play preflight; token/CA paths are consumed by web. | Empty actual list means unset, valid entries preserve IDs/URLs and private file references; reject wrong types and malformed entries before mutation. Test Ansible → generated unit/Compose → runtime, secret-file access and paths on each host. |
| `russound_use_published_images` | Docker default false; `bool` selects published versus target build. No explicit invalid-boolean assertion. Current default target build conflicts with agreed future controller-build intent. | Make source channel an explicit dev/release/local choice; legacy flag maps only when unambiguous. Test false/true spellings, invalid values and no hidden build/fallback. |
| `russound_web_image`, `russound_ser2net_image` | Empty defaults; length check applies in published mode, gateway reference only if managed. Whitespace passes (F22); YAML uses JSON quoting. No preflight verifies registry, architecture, digest or compatible pair. | Keep image pins as derived advanced selection, not duplicate editable aliases; require matching verified artifacts for chosen layout before disruption. Test unset/blank/whitespace, invalid references, digest, arch and no gateway in external mode. |
| `russound_published_pull_policy` | `always` by default; membership in `always`, `missing`, `never` is asserted even if published mode is false. `always` can pull a changed tag. | Decouple artifact acquisition from selection and ordinary rerun; translate legacy policies explicitly or fail with guidance. Test invalid/unused policy, offline pins and explicit update. |
| `russound_registry_override_dir` | Defaults under SSH user's assumed home; only used for published mode, rendered override mode `0600`. No path, symlink or conflict check before changes. | Derive private deployment-owned paths, record actual ordered Compose inputs and refuse unsafe/ambiguous migration (F9/F14). Test custom path, preexisting overrides and preservation of local-only image. |
| `russound_verify_backend` | Default false; `bool` gates later `/api/rooms` connected check. Invalid spellings are not explicitly validated; false still performs web HTTP check. | Report web and controller checks as separate outcomes, with explicit policy and no false success when controller verification is skipped. Test disconnected, unavailable and requested/omitted checks. |
| `russound_uid`, `russound_gid`, `russound_serial_gid` | Docker play sets facts from passwd/device, or serial GID 20 when external; extra vars have higher precedence. Template writes values directly; no explicit type/range or ownership consistency check. | Treat these as internal derived values, not competing inputs; reject user-provided conflicts or validate an explicit advanced mapping before file changes. Test mismatches, missing group and serial access. |
| `playback_agent_id` | Defaults `musicpi`; early regex requires lowercase safe ID, rejects empty/malformed. The health checks compare returned ID. Wrong-type behavior depends on Ansible templating. | Keep one identity shared with web registry; preserve existing bindings and test blank, malformed, duplicate and changed identity. |
| `playback_agent_port` | Agent and proxy independently default 8081; each checks `int` range but renders original value (F19). No cross-play equality check. | One agent endpoint feeds agent unit and proxy upstream; strict port normalization before rendering. Test divergent inventories and noninteger strings. |
| `playback_agent_start` | Default false; `bool` gates activation/health, not installation, source/token replacement, venv changes or manager reload. Invalid spellings are not explicitly validated. | Use clearly named staging versus activation intent with a genuinely non-mutating preview. Preserve active source/token until approved activation; test existing service and changed token without starting. |
| `playback_agent_enable_operations`, `playback_agent_enable_tunein_selection` | Defaults false/true. Operations uses raw truthiness (F11); TuneIn disabling uses lowercase string membership only when operations are truthy, and unrecognized values do not disable it. | Explicit read-only/operations choice and optional TuneIn setting; normalize and validate both, coordinate proxy gate, reject inconsistent selections. Test typed/string true/false, invalid and active-vs-rendered. |
| `playback_agent_token_source` | Required defined controller-side path, but no separate blank/path/permission check. File lookup strips its value and checks at least 32 printable ASCII characters; copy installs the original file mode `0600`. The web client imposes a separate maximum (F24). Errors are hidden by `no_log`. | Validate a private readable regular file and the exact installed bytes with consumer-compatible rules without exposing them; preserve existing credentials and defer rotation. Test missing, blank, oversized, newline, permissions and differing controller/target paths. |
| `playback_agent_home` | Defaults to gathered `user_dir`; derived paths use it, but it is not itself in the play's path-validation loop. Blank/nonstandard override propagates to derived paths, unless those are separately overridden. | Resolve home from the selected audio account; do not make a second editable home compete with account identity. Validate paths and conflict before change. Test custom home and overrides. |
| `playback_agent_dir`, `playback_agent_venv`, `playback_agent_secret_dir`, `playback_agent_config`, `playback_agent_state_dir`, `playback_agent_unit_dir` | Absolute/systemd-safe regex and traversal checks reject blank, relative, spaces and `%` for these six paths; existence and ownership are not fully established before copies. Unit path can diverge from manager lookup (F15). | Derive safe defaults, permit only supported advanced paths and verify owner, file type and loaded unit; quote safely if broader paths are supported. Test invalid and existing paths, symlinks, permissions and rollback. |
| `playback_agent_source` | Defaults to controller checkout; not covered by the six path checks. Entry point/manifest are copied and helper glob is used; absence or empty glob fails or is incomplete downstream, exact behavior is unverified. | Verify identified complete controller source and compatibility before any copy; use a staged versioned tree. Test missing source/helper, local changes and version mismatch without touching active agent. |
| `playback_agent_environment` | Default mapping is UID-derived runtime, bus and Pulse settings for Ansible tasks; XDG runtime directory and bus are checked, but unit hardcodes its own values (F15). Wrong/missing keys fail during evaluation; no full mapping validation. | Derive one runtime mapping, use it consistently or reject unsupported overrides. Test missing keys, wrong type, correct bus, service unit and manager environment. |
| `playback_proxy_hostname` | Required, <=253 chars; a restrictive regex rejects one-character names (F23) and IPv6, but can accept some invalid DNS/IP forms. Certificate name check happens after directory/certificate work. | Validate supported hostname/IP and certificate SAN before activation, state clearly whether IPv6 is supported. Test valid/invalid names and SAN mismatch without changing trust. |
| `playback_proxy_allowed_cidr`, `playback_proxy_additional_allowed_cidrs` | Primary required character regex; additional defaults `[]`, checked as a non-string sequence whose entries match the regex. Syntax admits invalid networks (F19), and a matching-key dictionary passes preflight but fails template rendering (F27); absence/empty primary fails assertion. | Require explicit valid IP/network list and verify the actual web-side peer separately. Test list types, empty primary, malformed CIDRs, IPv4/IPv6, NAT and denied clients. |
| `playback_proxy_port` | Default 8443; `int` range check 1024–65535 but original rendered (F19); no early listener collision check. | Strictly validate port and collision, share proxy URL with web connection and preserve explicit TLS trust. Test bounds and mismatched web URL. |
| `playback_proxy_start`, `playback_proxy_enable_operations` | Defaults false; start gates activation, not certificate/config/unit writes. Operations is raw truthiness (F11). | Explicit preview/stage/activation and coordinated operation policy; validate boolean spelling and test both gate combinations. |
| `playback_proxy_caddy_binary`, `playback_proxy_caddyfile`, `playback_proxy_unit` | Default binary/config/unit paths; binary is stat-checked executable, other paths lack explicit path validation. Unit activation uses a fixed service name regardless of destination (F16). | Derive fixed service identity and config paths or validate supported overrides consistently. Test missing binary, paths and actual installed/loaded unit before activation. |
| `playback_proxy_certificate`, `playback_proxy_private_key` | Default certificate/key paths; both must exist together or neither. Pair generation, permissions, SAN/expiry, self-signed trust and modulus are checked after directory creation; path safety and exact error on malformed overrides are not preflighted. Public certificate is fetched to controller, not automatically to a separate web host. | Treat trust/key paths as advanced protected inputs; inspect pair and SAN before changing live files, stage any new pair, explicitly provision web trust. Test partial pair, mismatch, expiry, unsafe paths and separate host without disclosing keys. |
| `playback_egress_activate` | Default false; first assertion requires `bool` true. False fails rather than staging; unrecognized spellings are not explicitly rejected. | Make egress activation a separate approved action with a read-only preview; test omitted, true/false and invalid inputs. |
| `playback_egress_resolvers` | Required defined/nonempty but no list/IP check; string becomes characters in unit (F12). Runtime validates IPs later. | Require a nonempty typed list of IP literals before writes; test IPv4/IPv6, invalid and duplicate entries and actual unit argv. |
| `playback_egress_user` | Defaults `musicpi`, checked by lowercase account regex and passwd lookup; UID must be non-root. Blank and invalid names fail; selected account can differ from actual audio account. | Derive from the selected audio identity or fail on mismatch; test missing, root and different account before filter installation. |
| `playback_egress_install`, `playback_egress_source` | Default root target directory and controller checkout source. No early path, quoting, source completeness or ownership checks; target path is inserted unquoted into systemd commands/drop-ins. | Use a validated root-owned fixed install location and identified source; reject unsafe paths and test source completeness, quoting, existing drop-ins and unrelated firewall preservation. |
| `playback_egress_uid`, `playback_egress_home` | Derived `set_fact` from passwd; extra vars can override facts. UID receives only a later positive integer check; home determines user-unit locations. | Derive privately from verified account, reject conflicting externally supplied facts, validate owner/path. Test mismatched UID/home before writing units or restarting MPD. |
| `mpd_russoundpi_repo`, `mpd_russoundpi_update_repo` | Repo defaults public URL, update defaults true under `new`; Git has no explicit revision and does not force changes. Boolean uses `bool`, no explicit invalid-input check. | Identify source revision before use; expose update as a deliberate action, keep saved config and do not silently update on rerun. Test offline, dirty and controller/target revision mismatch. |
| `mpd_russoundpi_dest`, `mpd_russoundpi_scripts` | Defaults derive from gathered audio-user home and checkout; scripts can be independently overridden. No early validation that scripts match repository revision. | Use one verified source location or report explicit external source; reject mismatch. Test missing scripts and conflicting overrides before service stops. |
| `mpd_russoundpi_config` | Defaults to audio user's persistent config; first run copies seed if absent, never overwrites it. No upfront complete audio/MPD validation before service stops and generation. | Preserve existing config; validate with all affected consumers before disruption. For a new host require reviewed hardware settings, not a silently assumed seed. Test missing, malformed, incompatible and existing calibrated config. |
| Audio `venv_dir` | Defaults `~/russound-venv`; creates only if executable absent, installs unbounded `python-mpd2`; an existing system-site-enabled venv remains so. No explicit path/isolation preflight. | Prepare separate validated isolated environment; retain active one until separately approved switch. Test wrong path, inherited packages, dependencies and rollback. |
| `mpd_russoundpi_start_playback` | Defaults true; `bool` controls staged stop versus start/reconcile; inventory default can lose to play var (F8). False still modifies audio, state, services and native ser2net (F13). | Separate preview, staging and activation with accurately reported side effects; no unrelated gateway management. Test each action and invalid boolean without live playback. |
| Remove play `playback_agent_unit_dir`, `playback_agent_environment` | Same names have separate defaults from deploy play. The unit path receives syntax checks; environment is passed to the manager. Removal stops/disables the fixed unit and deletes only the selected file; it retains code, state and token (F15). | Resolve the actual loaded unit and shared environment once; describe retained files and require explicit stop approval. Test mismatched install/removal settings, no unit and already stopped. |

This table establishes **source-level coverage**, not proof that all
combinations have been executed. Tests named in the final column are
implementation acceptance criteria, not completed tests. In particular,
Ansible `--check` is not a substitute for a verified no-mutation preview:
several current tasks explicitly skip validation in check mode, and normal
execution writes files independently of activation flags.

### S0 findings and remaining work

The observations below describe the S0 baseline. See the implementation
iteration log for subsequent fixes and validation; unresolved portions of a
finding remain open even if a related symptom has been fixed.

1. **F1 — empty-agent default is rejected.** Both web deployment plays default
   `russound_playback_agents` to `[]`; the
   former `russound-docker.env.j2` environment template
   and `russound-web.service.j2` native template render
   that list as `RUSSOUND_PLAYBACK_AGENTS=[]`. The web parser rejects a
   nonblank empty array. A focused selected-interpreter check rendered both
   templates with the default list, then passed the rendered Docker value
   `[]` to the parser, which raised `ValueError: RUSSOUND_PLAYBACK_AGENTS must
   be a non-empty JSON array`; blank returned `None`. A default web deployment
   may therefore fail startup
   when no agent is configured. The existing
   [deployment test](../../tests/test_deployment_workflows.py) checks the
   default and a nonempty rendered list separately, not default-to-runtime
   integration. Add an end-to-end rendering/parser regression when resolving
   this; render an actual empty list as an unset optional value in both
   templates while retaining parser rejection of malformed input.
   No live service was changed.
   A follow-up local probe rendered both current Ansible templates, passed
   the Docker environment through `docker compose config`, and fed the
   resulting values to `PlaybackAgentConfig.from_environment()`. The default
   empty list was rejected for **both** deployment paths; a valid single-agent
   list was accepted by both, and a malformed string was rejected by both.
   No container was started. Preserve rejection of malformed inputs and
   configured agents while fixing the no-agent default.
2. **F2 — debug server coupling fixed locally.** Previously `RUSSOUND_DEBUG`
   selected Flask's development server with its debugger. The web server now
   uses Waitress in both modes; the flag continues to enable diagnostic logs.
   The local server/playback regression suite passed 87 tests, including
   debug server selection and missing-Waitress checks. An isolated local
   debug-mode process logged Waitress startup and returned HTTP 200 for `/`
   and `/api/playback/health`. The remaining logging/privacy contract is still
   open; this change has not been deployed to the Pi.
3. **F3 — deployment baseline differs from earlier source and Pi.** Registry
   selection and additional proxy CIDRs were local changes at review; the Pi uses a
   one-off local image and its installed agent differs from the checkout.
   Review and reconcile these changes explicitly rather than deleting,
   overwriting or silently claiming they are released.
4. **F4 — no stable production artifacts.** Stable publication is not enabled;
   the latest development pair does not make production deployment available.
5. **F5 — reporting can disagree with resolution.** The configuration API
   lists nonblank invalid backend port overrides even when the resolver ignores
   them; see the [effective-config plan](./web_done_effective-config-overrides.md).
6. **F6 — the example turns on protocol audit.** Runtime omission disables
   audit logging, but the Docker play seeds
   [`web/config_example.json`](../../web/config_example.json), which includes
   `backend.protocol_audit_log_file`. A new seeded deployment can therefore
   write raw protocol audit records without an explicit per-installation
   choice. The operator chose to disable existing protocol audit during an
   approved migration after being informed; new deployments default it off.
   Back up the prior setting and do not change the live installation before
   approval. Coordinate opt-in with
   [guided setup](./general_idea_guided-first-time-setup.md).
7. **F7 — file and save validation differ.** File-load validation accepts
   incomplete shortcuts and partial TCP backend settings that editor saves
   reject; the partial endpoint can resolve to the default instead. Pi root
   unknown fields are accepted although output/watchdog unknown keys are
   rejected. Define compatibility and actionable reporting before tightening
   validation, rather than silently dropping fields or rejecting existing files.
8. **F8 — documented host overrides can be ineffective.** The README said
   Docker play defaults could be overridden per host or on the command line.
   The play defines those defaults in `vars`, which override same-named
   inventory host variables; extra vars do override them. The README has now
   been corrected and playback guides warn about staging.

   A subsequent controller-only Ansible probe used each real play's `vars`,
   synthetic host inventory, no fact gathering, and assertion-only tasks. For
   every selected literal default it verified the play value beat a different
   inventory value and an extra var beat the play value: 16 Docker, 6 native
   web, 5 agent, 10 proxy, 3 egress and 3 audio variables. These included
   `mpd_russoundpi_start_playback`, `playback_agent_start`,
   `playback_proxy_start`, `playback_egress_activate`, gateway management and
   image selection. For example, inventory-only audio staging `false` loses
   to play default `true`; explicit `-e ...=false` is required. This did not
   execute deployment tasks, contact a target, or test templated defaults,
   dictionary values, facts, downstream files or every precedence layer.
   Move intended operator defaults to an appropriate lower-priority layer in
   the rework and test real per-host and extra-var overrides end to end.
9. **F9 — rerun may omit the live web image override.** The current local
   published-image task does not include the Pi's separate outage-UI Compose
   override. Rendering the task's two files instead of the Pi's three selects
   a different web image. Preserve the running image and fix when designing a
   migration; do not use that task as a no-change Pi redeploy.
10. **F10 — audio logging depends on import order.** An isolated local import
    of the audio setup module configured a DEBUG stream handler instead of its
    intended relative file handler, because the imported helper configured
    the root logger first. MPD setup's import created the relative file.
    Explicitly configure and validate each command's logging during S3.
11. **F11 — string `false` enables operation gates.** In the current
    [agent unit template](../../ansible/templates/playback-agent.service.j2)
    and [proxy template](../../ansible/templates/playback-agent.Caddyfile.j2),
    the operations variables are tested for truthiness without boolean
    conversion. A controller-only Ansible core 2.21.5 play rendered both
    actual templates with `-e playback_agent_enable_operations=false` and
    `-e playback_proxy_enable_operations=false`: the unit contained
    `--operations-db` and Caddy included its write route. Supplying typed JSON
    boolean `false` rendered neither. The first fixture attempt failed due to
    a missing synthetic `ansible_facts.user_uid`; after adding that fixture
    fact, both comparisons passed. No Pi service was changed. The read-only
    default is an actual boolean false; the issue is the string override.
    Both gates must be enabled for proxied writes, and active services may
    differ from newly rendered files until restarted. The
    [playback guide](../documentation/playback-agent-deployment.md) now warns
    about this input. S4 must normalize and validate accepted boolean inputs
    for both templates, reject invalid values, and test the actual Ansible
    paths and running-gate behavior; do not treat syntactic success as proof
    that write operations are disabled.
12. **F12 — a resolver string passes egress preflight.** The current
    [egress play](../../ansible/playbooks/deploy-playback-egress.yml) checks
    only that `playback_egress_resolvers` is defined and nonempty. A real
    controller-only Ansible template probe confirmed that the nonempty string
    `192.0.2.1` satisfies those checks and causes the
    [unit template](../../ansible/templates/playback-egress.service.j2) to
    render nine `--resolver` arguments, one per character; a typed single-item
    list renders one. The filter's `ipaddress.ip_address` validates values
    during service execution, after the play has already copied files and
    written units/drop-ins. The probe did not start or modify any services.
    The [playback guide](../documentation/playback-agent-deployment.md) now
    warns to use a typed list. S4 must reject wrong types and invalid IP
    literals before any target change and exercise actual rendered units.
13. **F13 — audio staging also manages an unrelated gateway.** The
    [audio play](../../ansible/playbooks/setup-musicpi.yml) tags its native
    ser2net config and start tasks `init`; the documented `init,new` staging
    command selects them even with `mpd_russoundpi_start_playback=false`.
    Its native [ser2net config](../../mpdrussoundpi/ser2net/ser2net.yaml)
    names `/dev/ttyUSB0` and localhost port 6666. This can conflict with a
    container gateway's device or another selected gateway. Task selection
    and source were inspected; no conflicting service was started in this
    check. Separate audio from gateway ownership in the rework; a playback-only
    intent must not install, write, start or restart ser2net. Test both a
    container-gateway host and an external-gateway layout. The playback and
    headless guides now warn about the current staging command.
14. **F14 — default paths disagree with a changed deploy user.** In the
    former Docker play (`deploy-russound-docker.yml`),
    `deploy_user` controls ownership, but `app_dir` and
    `russound_registry_override_dir` default from `ansible_user`, not
    `deploy_user`. In the former native play (`deploy-russound-web.yml`)
    those paths derive from `deploy_user`, but assume `/home/<user>` and
    the unit uses the username as both user and group. Merely changing the
    deployment user can therefore select an unexpected home or fail for a
    different primary group. Source inspection only; no alternate account
    was provisioned. Derive defaults from the selected account's actual
    passwd entry and primary group, or require explicit paths; show resolved
    ownership in preview. Test different SSH and deploy users, nonstandard
    homes, groups and explicit paths without recursively chowning unrelated
    files.
15. **F15 — agent task environment is not its service environment.**
    `playback_agent_environment` supplies Ansible's user-systemd operations,
    while the [agent unit](../../ansible/templates/playback-agent.service.j2)
    separately hardcodes runtime, bus and Pulse paths from the discovered UID.
    An override can thus affect the play but not the running agent. Likewise
    `playback_agent_unit_dir` selects where a unit is written or removed,
    while service operations use a fixed unit name and the user's systemd
    search path. Do not promise arbitrary overrides work. In the rework use
    one validated runtime mapping and a systemd-recognized unit location, or
    reject unsupported overrides with migration guidance. Test rendered
    unit, manager environment, nondefault paths and removal together.
16. **F16 — proxy unit path and service name can diverge.** The proxy play
    writes `playback_proxy_unit`, but starts the fixed
    `mpdrussoundpi-playback-agent-proxy.service` name. A custom unit filename
    or location does not change the service being operated on. Source
    inspection only; no service was activated. Make the unit identity a
    single validated value, or reject unsupported path overrides. Test
    install, reload, activation and removal with the same resolved identity.
17. **F17 — endpoint defaults can hide saved configuration.** Direct
    hardware Compose (`docker-compose.yml`) leaves backend environment
    overrides blank by default, whereas the Docker play writes
    `russound-ser2net:6666` into `.env`; a valid override selects TCP over
    the saved serial endpoint and can make editor changes appear ineffective.
    Shell interpolation can in turn override the generated `.env`. This is
    not evidence of an actual current Pi mismatch. Preview the effective
    endpoint and its source; use the selected gateway placement as the one
    deployment-level owner, preserve saved configuration, and clearly report
    when it is overridden. Test direct Compose, managed/external modes,
    shell conflicts, partial inputs and removal of the override.
18. **F18 — native health probe assumes loopback binding.** Native web
    accepts `russound_host` but its play always probes `127.0.0.1`, which
    need not reach a service bound only to another address. An HTTP response
    alone also cannot establish that it came from the newly deployed process
    or that the controller is reachable. Source inspection only. Validate
    the chosen bind and probe address before disruption, identify the
    intended service and test loopback, wildcard and specific-address binds;
    report HTTP and controller readiness separately.
19. **F19 — proxy/agent preflight can accept malformed values.** Agent and
    proxy port assertions compare values after Ansible's `int` filter but
    render the original values into their units/configuration. A local
    Ansible probe showed that the string `8081.5` becomes `8081` under that
    filter, so it passes these range checks even though it is not an integer
    port. The proxy's CIDR assertion uses a character regex; the same probe
    showed `999.999.999.999/99` matches it despite not being an IP network.
    Templates and task order were inspected; no Caddy, agent or Pi execution
    was performed. Caddy validation occurs only after certificate and
    directory operations, so later validation is not a no-mutation preflight.
    Validate exact integer ports and IP/CIDR syntax before any target change,
    render the validated values and test malformed, boundary, IPv4/IPv6 and
    wrong-type inputs through the actual plays. Do not equate syntactic CIDR
    validity with proof that the web client's actual source is authorized.
20. **F20 — MPD setup silently ignores combined actions.** The
    [MPD setup CLI](../../mpdrussoundpi/russound/mpdsetup.py) accepts
    multiple flags, but its start, stop and restart branches exit before
    reaching enable/disable. Thus `--start --enable` starts services without
    enabling them; `--start --stop` selects start, and `--enable --disable`
    selects enable. `--install` can precede one of those actions, while
    kill/cleanup exit earlier. The earlier S0 wording incorrectly said
    start and enable could both follow install; it is corrected above. A
    disposable local invocation of the actual CLI with an empty temporary
    home and mocked `subprocess.run` recorded a supervisor start but no
    enable for `--start --enable`; no service command ran. The other
    combinations are source inspection only. Specify valid
    combinations, reject contradictory or silently ignored flags and retain
    intentional install-plus-action use with explicit compatibility tests.
21. **F21 — blank external gateway passes preflight and can select TCP.**
    The Docker play only compares `russound_backend_host` to the literal
    internal service name when gateway management is false; it does not
    require a nonblank host. A controller-only Ansible probe evaluated the
    actual assertions and rendered the real environment template with blank
    host and the default port: the assertion passed and the output contained
    a blank host and `RUSSOUND_BACKEND_PORT=6666`. A separate local runtime
    call with those environment values and a saved serial device resolved
    to `127.0.0.1:6666` with no device; with both overrides blank it retained
    the serial device. No container or target was run. Require a valid
    explicit host and valid port for external deployment before mutation
    (6666 is the proposed port default); preserve
    and report saved configuration, and test blank/whitespace, partial and
    malformed endpoints through real play, Compose and runtime paths.
22. **F22 — published image assertion accepts whitespace.** With published
    mode selected, the Docker play checks reference `length > 0`, not a
    stripped, syntactically valid reference. A controller-only Ansible
    assertion probe confirmed that a whitespace-only web reference passes.
    No registry or deployment was attempted. Reject blank/invalid references
    before service changes; verify existence, target architecture, digest
    and compatibility as required by D3. Validate pull policy only when it
    is applicable, and never turn a normal rerun into an implicit update.
23. **F23 — proxy hostname check excludes a valid single-character name.**
    The proxy hostname regex requires separate first and last characters.
    A controller-only Ansible check showed `x` fails it. Whether such a name
    resolves and matches a certificate is a separate deployment-specific
    question. Replace the ad hoc pattern with an explicitly documented
    DNS-name/IP policy and SAN verification; test one-character names,
    IPv4/IPv6 support as applicable, malformed and nonexistent names without
    broadening the existing trust boundary.
24. **F24 — token length checks disagree between agent and web.** The
    agent play checks at least 32 printable ASCII characters, and the agent
    app has no upper token-length check, while the web playback client reads
    up to 4097 characters and rejects a stripped token longer than 4096.
    A controller-only
    Ansible assertion with a synthetic 4097-character token passed the
    agent play's two token expressions. A local Python check with synthetic
    files showed both components accepted 4096 characters; the agent app
    accepted 4097 while the web client rejected it. Neither deployed
    component was started and no actual token was read. Define and test one
    shared supported credential format and size, check both host-side files
    before activation without revealing their content, and preserve existing
    credentials under the deferred-rotation decision.
25. **F25 — MPD service operations discover by filename, not config.**
    [MPD setup](../../mpdrussoundpi/russound/mpdsetup.py) discovers every
    user unit named `mpd_*.service`, excluding its two meta/supervisor names,
    for start/stop. The supplied JSON is not consulted to select those units.
    Cleanup also removes matching unit and config filenames without a managed
    header check. Install writes configured units but does not remove older
    ones. The separate `--killmpd` path also uses a name-pattern process kill
    and logs a failure rather than re-raising it. Thus stale or independently
    managed matching units can be stopped, started or deleted, and the kill
    path has a broader process scope. A local invocation with two disposable
    unit files, mocked `subprocess.run`, and a temporary user-unit directory
    confirmed both a configured-looking and unrelated matching unit were
    selected for start and removed by cleanup. The process kill was only
    source-inspected; no actual service was touched. Restrict future
    operations to verified owned units and
    explicitly reviewed configured/stale identities; preserve unrelated
    files and report any unowned collision. Test missing, stale and unowned
    matching files in a disposable user-unit tree and preserve egress
    drop-ins through approved migrations.
26. **F26 — private DNS resolver exception is UDP-only.**
    [Egress rule generation](../../mpdrussoundpi/russound/playbackegress.py)
    exempts configured resolver addresses on UDP port 53 before rejecting
    blocked destination networks; it does not exempt TCP DNS. A local
    `ruleset()` probe using synthetic private IPv4 and IPv6 resolvers
    confirmed both are in the blocked sets and only have UDP exceptions.
    TCP fallback to such resolvers can therefore be rejected by the generated
    rules; no nftables rules or live DNS requests were executed. An initial
    probe used a brittle expected CIDR representation and failed; the
    corrected check tested membership in the actual blocked networks.
    Review and test an explicit narrowly scoped TCP/UDP DNS policy, including
    private resolvers, without broadening unrelated egress; physical/network
    validation remains S5.
27. **F27 — proxy CIDR dictionary passes sequence checks.** Ansible treats a
    dictionary as a sequence. A local fixture using the proxy play's actual
    assertions confirmed that a dictionary whose key resembles a CIDR passes
    them; rendering the real Caddy template then fails when it concatenates
    the dictionary with a list. The failure was deliberately rescued and
    asserted in the fixture; no target was contacted. The real play writes
    directories and may generate a certificate before rendering the Caddyfile.
    Require a typed list of individually validated IP networks before target
    mutation and test dictionaries, strings, empty lists and invalid members.
28. **F28 — other CLI mode combinations silently discard options.**
    [Supervisor](../../mpdrussoundpi/russound/mpdsupervisor.py) accepts
    `--initialize --once` but exits after initialization; the
    [egress CLI](../../mpdrussoundpi/russound/playbackegress.py) accepts
    `--wait` alongside installation parameters and ignores them. These are
    source-inspected branches, not live invocations. As with F20, make
    exclusive actions explicit, reject conflicting combinations with clear
    errors, and retain documented single-action commands. Test with mocked
    service/firewall calls to prove rejected options do not change state.

### S0 simplification: one owner and an honest effective value

The rework must make common choices easier, not add another independently
editable copy of each setting. The following are requirements for the
existing D1–D9 contract, not claims about the current plays or a proposal to
silently remove supported interfaces:

| Operator decision | Authoritative input and conflict handling | Required regression |
|---|---|---|
| Placement and gateway | One explicit deployment choice determines managed, external or dummy gateway and the corresponding web endpoint. Audio-only and web-only operations do not acquire another component. Keep gateway transport/device settings relevant only when managed. | Test each layout and rejected contradictory inputs, including F13 and an existing container gateway. No unrelated service or device is touched. |
| Effective endpoint | Deployment-selected endpoint is visibly distinguished from the saved editor configuration; retain the saved value so removing an override reveals it. An explicit override is not silently replaced by a default or an uncontrolled Compose shell value. | Test F5/F17, serial-to-TCP, host-only/port-only, blank/invalid values and source reporting through real runtime and editor/API paths. |
| Listener and verification | Distinguish the host-published web port, process bind and internal gateway port. Derive checks from the selected listener and show HTTP, controller and playback checks separately. | Test F18, port conflicts and health check against the intended process; HTTP alone never reports controller success. |
| Agent and proxy | Treat agent listener and proxy upstream as one coordinated connection; make read-only versus write permission explicit at both gates. A staged change is not presented as active. | Test differing ports, F11/F19, one gate enabled, both enabled and stale running configuration; test TLS, authorization and denied sources. |
| User, paths and units | Resolve account identity and supported paths once; show generated destinations and required restarts. Eliminate or reject misleading unit and environment overrides, with a compatibility notice rather than silent reinterpretation. | Test F14–F16 and the actual installed/running unit, including distinct SSH and deploy accounts. |
| Images and reruns | Use the selected compatible pinned pair and ordered Compose inputs as the deployment identity; a normal rerun does not mean an update or a fallback to a different image. | Test F3/F4/F9 and shell/override conflicts, explicit update and unavailable prior artifacts. |
| Boolean and list inputs | Accept documented typed values and explicitly supported CLI spellings; reject invalid or ambiguous inputs before mutation. Keep secrets in protected files, not additional configuration copies. | Test F1/F8/F11/F12 with omitted, typed, string, malformed and empty values through real templates and consumers. |
| Logging and audit | A clear profile controls normal diagnostics; protocol audit is a separate explicit opt-in. Show effective state and protect logs; existing audit is changed only under the agreed migration boundary. | Test F2/F6/F10, false/true debug precedence, retained security events, rotation, redaction and migration backup. |
| Staging, activation and recovery | Name operations by their actual effects. A start/activate false value must not imply a no-write preview; preview must be genuinely non-mutating and interruption requires approval. | Test F20, changed files, active process versus staged config and recovery after partial failure for agent, proxy, egress and audio. |

Where existing flags cannot express the agreed precedence (for example
`--debug` has no explicit false), retain compatibility for existing
invocations and specify an unambiguous false override before claiming the
new precedence applies. Do not silently reinterpret legacy flags, introduce
automatic credential rotation, or replace state with seed values. Document
unsupported or deprecated settings and migration steps alongside their
replacement. The actual choice of a new operator interface and the technical
implementation remain subject to the existing design and test gates.

#### Implementation sequence and operator experience

The table below converts the inventory into work for S2–S4. It does not
authorize a cutover or commit to a new command syntax.

| Step | Implementation outcome | Completion evidence |
|---|---|---|
| 1. Reduce the questions | For an ordinary deployment, ask for target, dev/release/local mode and managed/external gateway. Ask for an external host only for external mode, proposing port 6666; ask for serial device only for managed hardware. Playback integration, trust and DNS resolvers appear only when that separately managed component is selected. Defaults and impact are visible. | Table-driven tests across all agreed placements: irrelevant options are not required, conflicting supplied values fail with a useful field name, and no secret values enter output. |
| 2. Resolve once | Normalize typed values and document supported legacy spellings; resolve account, paths, gateway endpoint, agent/proxy ports and image identity once per operation. Derive generated environment and units from that result; reject inconsistent duplicate inputs instead of maintaining multiple independent copies. | Use focused tests for representative defaults, overrides, conflicts, invalid values and high-risk boundaries. Exercise real Ansible inventory/extra vars and downstream consumers for affected paths; expand coverage when new failures are found. |
| 3. Preview and preflight | Show target, component scope, selected source/digests, effective endpoint and provenance, relevant paths, active versus staged changes and expected restarts. Validate types, serial access, identity, source, paths, image/channel, credentials and network syntax before target mutation; do not treat preview as Ansible check mode. | Verify no mutation for representative invalid cases and preview. Exercise known findings through actual consumer paths where applicable; test systemd/Compose escaping and no secret disclosure. |
| 4. Apply only the chosen scope | Separate web, gateway and playback ownership; use pinned images for normal reruns, preserve state/credentials, stage changes and only activate approved services. A normal rerun is neither an update nor credential rotation. | Disposable integration tests verify exact generated files, service operations and active configuration; inject failures and confirm unrelated services/data stay unchanged. Cover F9/F11–F20 and F24; verify cross-host routing separately at S5. |
| 5. Replace the confusing surface | Publish one concise operator guide with supported choices, defaults and examples; list advanced/legacy mappings separately with actionable diagnostics for unsupported or conflicting old options. Correct misleading current guides now, and retire legacy options only with a documented compatibility/deprecation decision. | Run examples through the actual supported entry points and review rendered results; verify old valid invocations, explicit migration errors and removal of stale instructions. |

The implementation must carry F2's existing Waitress/debug fix forward and
apply F3/F4 release/source constraints rather than treating those findings as
configuration-validation bugs. The matrix is a regression backlog, not a
requirement to expose every legacy Ansible variable as an operator option.
Do not silently remove advanced capabilities that are needed for supported
layouts; document whether each remains supported, is derived, or needs a
separately approved replacement.

### S0 recheck: task selection and guide comparison

On 2026-11-13 all seven current playbooks passed controller-side Ansible
syntax checks. A separate `--list-tasks` comparison showed that the audio
play invoked without tags selects the same listed tasks as `--tags init,new`;
`--tags update` selects the dynamic update include, whose source performs an
apt cache update and `dist` upgrade. These are selection/syntax checks only:
conditions, dynamic included tasks, handlers, permissions and target effects
were not executed. The documented staging command explicitly passes
`mpd_russoundpi_start_playback=false`; it still stops/reconfigures audio and
MPD as recorded above.

Another `--list-tasks` check explicitly selected `init,new` and listed the
native ser2net copy/start tasks (F13). A static enumeration of the seven
plays' `vars` sections found 71 declarations across the plays (including
repeated names), all represented by the entry-point matrix above. Required
inputs with no default, generated facts and other Ansible precedence layers
are separate; enumeration does not validate their types, downstream rendering
or behavior and does not by itself close the per-setting audit.

On 2026-11-13 the per-parameter section was cross-checked mechanically
against all 71 play-level declarations; none was missing. A controller-only
fixture reproduced the Docker play's first four preflight expressions and
rendered the actual environment template (F21/F22). `docker compose config`
separately confirmed that a blank host and nonblank port reach the rendered
web environment; a local runtime resolver check established the serial-to-TCP
effect. The Ansible token check and local agent/web test used synthetic
credentials only (F24). Existing deployment workflow and agent deployment
suites passed **11 and 9 tests**, respectively; those tests are largely
static and do not prove the fixes or a live deployment. No service was
started. Other per-parameter behaviors above are source-inspected unless
explicitly identified as a probe, and the future tests are still required.

The subsequent S0 lifecycle review inspected all seven plays' writes,
ownership and service transitions, the audio and MPD generators, CLI branch
order, runtime log initializers, and the main README/playback/headless guide
commands. The generated-file table above records known modes and expressly
marks environment-dependent metadata rather than guessing it. README
endpoint wording and the headless MPD stop warning were corrected; F25–F28
record additional findings.

Controller-only Ansible 2.21.5 probes rendered the actual agent, proxy,
egress and native web templates using synthetic values, tested a two-resolver
list, representative bad paths, empty/invalid resolver shapes, and numeric
casting. A second fixture modeled real play defaults and a synthetic
`set_fact`: inventory lost to play-level derived home, environment mapping
and additional-CIDR list, while extra vars won for those fields, a required
resolver list and the derived UID. These are representative precedence cases,
not actual target facts or proof of every permutation. A deliberately rescued
template failure established F27; Ansible's zero exit in a rescued play is
not evidence that the rendering succeeded.

The existing local suites for MPD setup, audio setup, supervisor and egress
passed **11, 31, 9 and 7 tests** respectively. Their simulated failure
messages are expected test output. All seven playbooks passed syntax checks
again. These results do not prove idempotence, target ownership, complete
configuration validation, firewall behavior or physical playback.

The README's two web play commands still encounter F1 unless a nonempty agent
list is provided, and its published-image example cannot reproduce the Pi's
third outage override (F9). The playback guide correctly distinguishes the
controller-fetched certificate from a separate web host's secrets and warns
that an agent-only install can replace source or token without restarting the
active process. The proxy also writes and validates configuration and can
create a certificate even when `playback_proxy_start` is false; that flag
gates service activation, not all changes. The agent play creates its user
unit directory with mode `0700` even if it already exists, which can change
the directory's prior permissions. These observations are not proof of
safe idempotence or successful deployment.

### Remaining work and disposition

| Item | Next gate | Required evidence or result |
|---|---|---|
| Current configuration matrix and precedence | S1/S2, then S3/S4 | Use the source-level table and known exceptions for design and focused compatibility tests. Existing probes cover selected cases, not every variable/type or downstream consumer. |
| Audio logging and Python environments | S2/S3 | Remove import-order-dependent logging, specify reproducible dependencies and rehearse a separately created isolated audio environment; preserve the active venv until a verified, separately approved transition. |
| Earlier registry and proxy work | S2/S4 | Reconcile each feature with pinned images, the outage override, CIDR validation and safe cutover. Add executable regression coverage before reuse. |
| Running Pi image, state, secrets and generated files | S2, before any cutover | Capture an approved private, consistent backup and exact deployed identities; inspect ownership and compatibility, including venv/system dependencies, MPD drop-ins and audio fragments. Do not equate this inventory with a backup. |
| Stable production image pair | S2/S4 | Design production to fail clearly without an approved stable pair; absence is a release blocker, not a reason to block dev-only design. |
| Separate-host authenticated routing, hardware and audio | S5 | Test representative deployments with separately provisioned credentials, TLS and actual peer/CIDR, then controller and playback readiness. Current unauthenticated LAN probe is not a pass for this gate. |
| Migration and rollback | S2/S5 | Rehearse on isolated data, verify version/schema compatibility, and obtain separate Pi maintenance approval. On failure stop and ask, rather than automatically restoring. |

**S0 disposition, 2026-11-13:** The operator accepted the existing inventory
and representative probes as sufficient and chose to stop further exhaustive
analysis. S0 is closed on that basis, not because every setting, combination,
ownership or actual target effect has been verified. Carry the known findings
into the design and focused implementation regressions; investigate and fix
newly discovered issues as they arise. Unknowns that affect a proposed change
must still be checked before that change, especially before a live cutover.
This decision does not waive backup, approval, safety checks, or later S5
acceptance.

Authenticated separate-host routing, physical hardware/audio and a
six-hour observation are S5 acceptance, not prerequisites for an honest
source-level S0 inventory. Compare installed dependencies and generated audio
assets before migration, and finalize how earlier local work is incorporated
at S2/S4. None of those later gates is satisfied by this inventory.

</details>

## S1: decisions and approval gate

The operator has agreed the following direction. It is a design contract,
not authorization to deploy or restart services. S1 is closed as an
implementation baseline at the operator's direction; remaining details and
compatibility impacts are handled during implementation as they arise.

| ID | Topic | Agreed direction and remaining details |
|---|---|---|
| D1 | Supported placement | **Agreed:** preserve the Pi hybrid, separate web host, external gateway and hardware-free dummy layouts. Include the separate managed-gateway Compose overlay only for a managed gateway. Bare-metal web manages native web and, only when selected, native ser2net; playback services remain separate. Dummy is development-only, built explicitly on the controller, with pinned dev web. **Remaining:** specify migration paths and dummy artifact verification. |
| D2 | Local build location and source | **Agreed:** build on the Ansible controller from a clean, identified source revision and transfer verified images to the target. Refuse dirty or ambiguous source without resetting it. **Remaining:** define source identification, target platform, artifact verification and network/base-image pulls. |
| D3 | Image identity and updates | **Agreed:** resolve Hub development images on first deployment, record exact digests and reuse the pins on ordinary redeployments; updates are explicit. Matching verified source revision and target platform suffice for dev pairing; a durable CI pair record is not required. Never fall back across channels. Stable still requires an approved release. **Remaining:** define resolution mechanics and stable selection once releases exist. |
| D4 | Runtime precedence and invalid inputs | **Agreed:** explicit CLI > environment > saved configuration > built-in default where those sources exist; blank optional values are unset, blank required values and other invalid configuration fail explicitly. Preflight incompatible inputs before service changes, preserve existing data and require reviewed correction instead of silently rewriting it. Render an actual empty agent list as unset in both plays. **Remaining:** document Ansible and Compose separately, field-specific types and necessary exceptions. |
| D5 | Logging contract | **Agreed:** development DEBUG; production warnings/errors plus essential health and security events; protocol audit off unless explicitly enabled. Web file rotation is 10 MiB plus five backups, audit 5 MiB plus three. Configure Docker `max-size: 10m` and `max-file: 3` per container; native logs use the host journal without changing global retention. During an approved migration, inform the operator and disable previously configured audit after backup; later opt-ins survive ordinary reruns. **Remaining:** define other component destinations and test redaction. |
| D6 | Migration and rollback | **Agreed:** a failed migration stops and reports changes; the operator decides whether to restore, rather than automatic rollback. Require a verified private backup with an off-Pi copy before Pi cutover. Encryption is not required for now; retain backups until explicitly removed. Verify compatibility before restoration. Pi cutover requires separate approval. **Remaining:** define backup verification, data-schema rules and runbook; coordinate cross-version rehearsal with [image updates](./general_idea_user-triggered-image-updates.md). |
| D7 | Python dependencies | **Agreed:** use separate component dependencies and bounded version ranges, not fully resolved locks. Do not unconditionally upgrade packages on ordinary reruns. Prepare a separate isolated audio environment and retain the working one until a verified, separately approved switch. **Remaining:** define and test supported ranges, platform coverage and security-update procedure; fresh installations can resolve different versions over time. |
| D8 | Deployment record | **Agreed:** use a private versioned target-side record under `/var/lib/mpdrussoundpi/deployments/<deployment-id>/`; record committed and pending selections, block normal reruns after interruption or unreadable state and require explicit recovery. No secrets belong in the record. **Revised by operator 2026-11-13:** replace the previously agreed long-held OS lock with a persistent host-wide reservation and operator-led recovery. The design is not implemented or validated. |
| D9 | Agent updates and credentials | **Agreed:** stage agent changes separately; do not modify active agent files or credentials until explicit activation. **Revised by operator 2026-11-13:** cross-host agent/web credential rotation is deferred from this rework; do not claim the host-local reservation coordinates hosts. The current agent play does not provide staged activation or safe rotation. |

### Design decision and impact summary

D1–D9 above remain the authoritative operator decisions. The following
records their consequences and the later design choices made for this plan;
none asserts that the proposed tooling already exists.

| Decision | Impact |
|---|---|
| Hybrid, separate host, external gateway and dummy remain supported (D1) | Web-only work leaves playback services alone; managed gateway is a separate overlay and dummy never counts as physical acceptance. |
| Controller builds and explicit image pins (D2–D3) | Local builds require clean identified source and verified transfer; normal reruns keep exact pins; explicit updates do not mix revisions. Production remains blocked without a stable release. |
| Runtime precedence and validation (D4) | Explicit CLI, environment, saved config, then default where applicable; optional blank is unset, invalid input fails; actual empty agent list renders unset without accepting malformed data. |
| Logging and audit (D5) | Bounded dev/production logging and redaction; audit is off by default and migration disables an existing audit only after the operator is informed and backup is taken. |
| Migration and backup (D6) | Preview and maintenance approval precede any writer interruption; verify a private off-Pi backup including the Pi's local-only image and actual overrides. Recovery is operator-approved, not automatic. |
| Component dependencies and agent staging (D7, D9) | Isolated bounded dependency environments; retain the working audio environment; agent source and credentials remain active until approved activation. Cross-host credential rotation is deferred. |
| Host-wide persistent reservation (D8) | Participating operations serialize across deployment IDs; a crash normally leaves a marker for manual investigation. No fence against manual tools, daemon work or the exceptional absent-marker outcome. |
| Approved private input snapshots | Compare source bytes and permissions before disruption and again before apply. Services consume verified stable installed copies, not temporary staging; uncooperative live writers block unsafe overwrites. |
| Apply, checks and finalization | Required tasks, handlers and awaited jobs must succeed with a complete structured result; checks follow apply, and finalization is separate and at most once. Lost responses and durability failures are unknown, not success. |
| Retry and loop result identity (self-review, 2026-11-13) | Distinguish intermediate attempts and individual loop items from terminal outcomes; unknown or incomplete evidence blocks finalization. S4 must prove actual callback behavior or prohibit unsupported required patterns. |
| MPD and egress ownership (self-review, 2026-11-13) | Both operations target validated, owned configured units and drop-ins; unowned or ambiguous legacy assets require explicit adoption or migration. Egress remains separately approved and must not weaken existing protection silently. |
| Acceptance and observation | Report unavailable physical checks as incomplete; software deployment may be recorded separately, but hardware acceptance remains open. A six-hour observation is a later separately approved activity, not a startup test. |
| Proportionate inventory (operator decision, 2026-11-13) | Close S0 with documented unknowns rather than pursue exhaustive permutations. Address known defects with focused regression tests; investigate new issues as they arise without relaxing migration safeguards. |
| Preserved legacy and staged gates | Current playbooks and live Pi are not made safe by this design. S1 and S2 are closed as a baseline; S4 integration and S5 live acceptance remain separate. |

## S2 design baseline: cleanup and deployment

This design translates agreed directions into implementation work and does not
approve a live cutover. Resolve material compatibility and configuration cases
as they arise before the affected changes are made.

| Area | Proposed design | Evidence required before accepting |
|---|---|---|
| Logging | Configure logging at executable entry points rather than on helper import. Use explicit component/profile levels and destinations; retain warnings/errors and health/security events in production, redact sensitive data and keep protocol audit separately opted in. Bound files and containers separately; native journal retention remains host-managed. | Isolated import and subprocess tests for audio setup, MPD setup, agent and supervisor; assert exact levels, handlers, destinations, no duplicate output, redaction, audit opt-in and rotation. Verify migration announces and disables existing audit after backing up its setting; later explicit opt-in survives ordinary reruns. |
| Native Python environments | Describe the Python dependencies for each component and build a fresh isolated audio venv in a separate location; keep any required OS binaries or system libraries explicit. Verify imports, CLI preflight and dependency resolution without accessing or restarting playback. Agent and audio need not share one venv. | Test fresh installation without system site packages on supported target architecture; compare needed versions, execute safe checks and rehearse switching a disposable unit. Do not remove the old venv or alter live units until a separately approved migration has succeeded and rollback is understood. |
| Image selection | Resolve a compatible Hub development pair once and persist the exact repository digests plus architecture and revision evidence. A normal rerun uses the recorded pair and must not silently switch to a moving tag. Release mode requires an approved stable pair; local-build mode builds on the controller from a clean identified revision and transfers verified target-platform artifacts. | Render and test first deployment, rerun, explicit update, missing image, mismatched pair and architecture; verify artifact integrity on transfer. Reconcile the current local outage-fix image and all three Pi Compose inputs before any change. |
| Layout and inputs | Offer separate dev and production Compose entry points and four Ansible intents with shared tasks. Preserve hybrid, separate-web-host, external-gateway and dummy layouts. Place operator defaults where host inventory can actually override them; render only inputs the selected component needs. | Verify real Ansible and Compose precedence, no-agent default, external gateway, dummy service, secret mount/trust and each layout's rendered images and networking. Document unsupported or required inputs explicitly. |
| Preflight and recovery | Preview selected components, old/new images, generated inputs and expected interruption. Validate configuration, platform, available space, hardware, network/trust and backup feasibility before stopping services. On failure report changed items and stop; only an operator-approved compatible restore follows. | Exercise failure before and after the change boundary on isolated fixtures; verify private backups, online SQLite backup or stopped writers, preserved units/drop-ins and explicit incomplete hardware status. Rehearse restore without assuming cross-version schema compatibility. |

### Deployment design: candidate inputs and boundaries

These are proposed mechanics for review, not new public commands or completed
implementation. The operator normally selects one intent and supplies only
nondefault settings:

| Intent | Candidate operator input | Selected components and source |
|---|---|---|
| Docker dev | Host/inventory; gateway placement; optional playback connection; existing deployment pin if present. | Web and optionally managed gateway use the recorded Hub development digest pair. Resolve and record a compatible pair only when no pin exists; updates require an explicit operation. |
| Docker release | Same layout inputs; approved stable pair. | Same components with stable digests; fail before changes when a compatible release is unavailable. |
| Docker local build | Same layout inputs; clean controller revision and target platform. | Explicitly build the required web/gateway images on the controller, verify and transfer them. Do not select a registry application image as a fallback. |
| Bare metal | Host/inventory, web listener and managed or external gateway; optional playback connection. | Native web and optionally native gateway. Existing native agent, proxy, MPD and audio remain separate and must not be changed by a web-only intent. |
| Dummy development | Development layout explicitly selects dummy instead of physical gateway. | Pinned dev web image and explicitly controller-built dummy from a clean identified revision; no serial device requirement. Show mixed sources and verify the transferred artifact. Dummy is not controller acceptance. |

#### Configuration handoff: initial implementation matrix

| Input | Existing handoff and issue | Target and required test |
|---|---|---|
| Optional playback agents | Both Ansible plays default to an empty list, but both templates render `[]` and the runtime rejects it (F1). A nonempty valid list works through both paths. | Render the **actual empty list** as a blank/unset environment value and continue to serialize nonempty lists; preserve explicit failures for invalid types and malformed entries. Exercise real Ansible templates, Compose interpolation, native-unit parsing and runtime startup. Confirm no secrets are embedded in output. |
| Runtime backend endpoint | Ansible-generated environment can override saved endpoint fields independently, and invalid ports currently fall back. | Apply agreed CLI > env > saved config > default where applicable, with blank optional values unset and malformed explicit inputs failing with actionable migration guidance. Test complete and partial overrides and removal without rewriting saved values. |
| Container-published vs internal web port | Compose interpolates the published port but sets the process port to 8000. Native web passes CLI port. | Keep host and container listener concepts separate; test absent, blank, malformed and boundary values, host collision preflight and health checks against the actual listener. |
| Deployment variables | Several play-level defaults override host inventory values (F8); extra vars override both. | Put user-facing defaults in a lower-priority source appropriate to the chosen entry point. Test each supported host override, extra-var precedence and invalid/blank values through rendered downstream files. |
| Agent/proxy operation flags | Ansible key-value `-e ...=false` is a nonempty string and both current templates treat it as true (F11). Typed JSON false and omitted defaults render read-only. | Normalize explicitly supported true/false inputs before templating; reject unrecognized values. Test omitted, typed and key-value true/false end to end; do not activate or rotate credentials without approval. |
| Egress DNS resolvers | The egress play checks only defined/nonempty input; a string is iterated character by character in the unit template (F12). | Require a nonempty list of valid IP literals before target mutation; reject strings, empty lists and invalid members. Test rendered argv and errors without restarting the actual filter. |
| Protocol-audit setting | Saved config and example seed can enable audit without an explicit new-install choice. | New installs default off. On an approved migration, announce and disable existing audit only after backup; later explicit opt-in survives reruns. Test file preservation and migration recovery. |

#### Implementation design for S0 findings

These are proposed corrections within the existing staged design, not an
instruction to run legacy plays on the Pi. Keep the operator surface small:
the normal inputs remain deployment intent, target, gateway choice, and
layout-specific prerequisites. Advanced paths and policies should be derived
where possible; legacy inputs must either map unambiguously or fail with
guidance. Do not silently drop supported layouts or settings.

| Findings | Proposed implementation and compatibility boundary | Focused evidence |
|---|---|---|
| F1, F5, F7, F17, F21: agent and backend configuration | Normalize the actual empty agent list to unset in both web templates; reject malformed lists. Resolve the selected gateway and explicit overrides once, show effective value/source, preserve saved JSON, and reject blank external host or invalid port before changes. Keep partial runtime overrides compatible with D4 and do not rewrite unknown saved fields. | Exercise no-agent, valid-agent and malformed-agent cases through real templates and parser. Check external blank host, default/nondefault port, saved serial and TCP, override removal, and editor/API reporting. |
| F2, F6, F10: logging and audit | Retain Waitress in debug mode; initialize logging at entry points, not helper imports. Apply D5 profiles and preserve audit during ordinary reruns; inform and back up before disabling existing audit during approved migration. | Test debug server choice, import order, rotation/redaction and audit default/approved migration. |
| F3, F4, F8, F9, F22: inputs and image identity | Place operator defaults below inventory priority, validate references and resolve compatible digests before disruption. Preserve ordered live Compose inputs and local-only image in backup; ordinary reruns reuse pins, explicit update changes them, stable mode fails without a release. Do not reuse the current two-file play as a no-change Pi redeploy. | Test actual inventory and extra vars, empty/whitespace refs, offline pins, failed resolution, external gateway without gateway image, and the Pi's extra override in a disposable fixture. |
| F11, F12, F19, F23, F24, F27: agent, proxy and egress validation | Normalize supported booleans and exact integer ports, require typed IP-literal resolver lists and typed IP/network CIDR lists, and validate a documented DNS/IP hostname policy. Coordinate agent listener, proxy upstream and both write gates. Validate credentials against both consumers using protected files; preserve existing token and defer cross-host rotation. Stage validation before writes or service changes. | Reproduce string `false`, decimal/bool port, bad IP/CIDR, CIDR dictionary, single-character hostname and token-size boundary using actual consumers/templates. Test read-only/write gates and reject failures without mutation or secret disclosure. |
| F13–F16, F18: component and service identity | Separate playback setup from gateway ownership. Resolve deploy account, home and primary group once; generate a consistent manager/service environment and one unit identity. Reject unsupported legacy path overrides with migration instructions. Derive a web readiness probe from its bind and separately report controller readiness. | Test container/external gateway isolation, distinct SSH/deploy users, custom paths and mismatched units, and loopback/wildcard/specific binds. Confirm a web-only operation does not touch playback. |
| F20, F28: ignored CLI options | Reject mutually conflicting action combinations with an actionable diagnostic before service or firewall operations; keep intentional install-plus-one-action and documented single-action commands. Inventory currently used invocations and describe compatibility changes instead of silently changing them. | Test combined and single actions with mocked service/firewall operations; failures must not mutate state. |
| F25: MPD and egress ownership and scope | Use the same validated output IDs and ownership checks for MPD setup **and** egress; neither may discover its mutation targets by a filename pattern. Stop, start, regenerate, remove or attach egress drop-ins only to established owned units. Legacy files without a marker require explicit inspection and approved adoption or migration; never auto-delete stale or unowned units/drop-ins. Replace or constrain the broad kill action and report failures. Existing playbooks retain their current behavior until replaced. | In a disposable user-unit tree test configured, stale, unowned and colliding units plus egress drop-ins. Prove unrelated units are untouched by both setup and egress, including on rejected adoption or service failure. |
| F26: DNS TCP fallback | Proposed egress correction: for each explicitly configured resolver IP, allow both UDP and TCP destination port 53 before the blocked-network rules, scoped to the existing protected MPD cgroup. Do not allow other ports or destinations, weaken the remaining public-only rules, or alter unrelated firewall tables. Apply only as a separately approved egress change; do not silently modify the running filter during web deployment. | Inspect generated IPv4/IPv6 rules with private resolvers, unrelated blocked addresses and non-DNS ports; test failures and preservation of other rules on a disposable Linux host. Verify actual DNS fallback and playback separately at S5. |

Use the finding-specific tests above as the minimum for the changed
behaviors, not a requirement to test every permutation before implementation.
For unknown conditions discovered during implementation, stop before unsafe
mutation, investigate and add a focused regression. Resolve any material
compatibility or policy choice when needed; this inventory does not imply
that these corrections or their tests already exist.

For egress, resolve the selected playback account and configured outputs, then
validate their exact unit identities, effective systemd paths and ownership
before creating files, changing rules or restarting anything. Check existing
drop-ins as well as units; an unowned, colliding, overridden or ambiguous
target blocks the affected operation with an actionable diagnostic. A
configured legacy unit or drop-in may be used only after explicit inspection
and approved adoption or migration. Report stale or unrelated matches without
changing them. Preview the exact affected units and filter change; egress
activation remains a separate approved operation, never a side effect of web
deployment. Do not remove protection from an already protected instance merely
because it is no longer selected; report and resolve such drift explicitly.
In disposable tests, cover matching unrelated units, missing configured units,
unit or drop-in collisions, effective-path overrides, legacy adoption refusal,
and failures before and during activation. Assert that preflight failures
change no files or rules, and that partial activation is reported for
investigation rather than presented as success or automatically removing
existing protection.

Common host settings such as port 8000 and the managed gateway's port 6666
remain defaults, not mandatory fields. Require explicit serial device access,
external endpoint, proxy trust, credentials and hardware identifiers when the
selected layout needs them. On the first deployment, report the resolved
source revision, platform and image digests before applying; do not record a
successful pin for a failed or partial image pair. Store sensitive values
separately from the non-secret image identity record.

The agreed Compose structure uses a separate managed-gateway overlay: include
it for a managed gateway and omit it for an external gateway. The development
and production entry points and a separate dummy overlay remain part of the
design. Each published profile must have no application `build` instruction;
controller-side build uses a separate explicit build mechanism. Ansible
chooses the appropriate ordered files and variables. Candidate file names
and responsibilities are specified in the blueprint below. A gateway
Compose profile inside an otherwise included file is not a substitute for
omitting the overlay, because interpolation still occurs.

**Local render experiment, 2026-11-12:** a throwaway Compose prototype with a
profile-disabled gateway still evaluated `${GATEWAY_IMAGE:?...}` and rejected
an external-gateway configuration lacking that unused image. Splitting the
gateway into an explicitly included overlay rendered web-only external mode
without a gateway image, rendered managed mode with both synthetic digest
references, and rejected a missing managed image or backend host. The probe
also checked that no web build definition was present and an unset optional
agent value reached web as blank. It used `docker compose config`, never pulled
images or started containers. Synthetic digests do not prove images exist,
verify actual runtime parsing, or establish that Compose alone enforces source
policy; those require implementation tests.

### Implementation blueprint and current boundary

The names and examples below define the implementation contract. Compose
profiles/overlays now exist and `deploy-dev.yml` renders a controller-only
preview; its apply action is explicitly blocked. Other new playbooks and
the safety runner are not implemented. This does not close the rehearsal
gate or authorize a Pi change. Legacy entry points retain their separate
behavior until replaced.

#### Entry points and composition

Use the following future repository paths. Keep current playbooks available
until the replacement and migration documentation are tested; a legacy
entry point must not silently forward an incompatible invocation.

| Intent | Entry point and status | Ordered Compose inputs | Application source |
|---|---|---|---|
| Development | `ansible/playbooks/deploy-dev.yml` exists, preview only | `compose.dev.yml`, then optional gateway **or** dummy overlay | Explicit web and managed gateway digests; identity resolution/verification still outstanding; dummy explicitly built on controller |
| Production | `ansible/playbooks/deploy-release.yml` not implemented | `compose.prod.yml` layout exists, then optional gateway overlay | Approved stable web and managed gateway pins; currently blocked |
| Development local build | `ansible/playbooks/deploy-dev-build.yml` not implemented | `compose.dev.yml`, then optional gateway overlay | Explicit controller-built web and managed gateway artifacts |
| Native web | `ansible/playbooks/deploy-native-web.yml` not implemented | None | Identified native web source and isolated web environment; optional native ser2net |

Use `compose.gateway.yml` only for a managed container gateway and
`compose.dummy.yml` only for dummy. Both define their own backend service;
neither is included for an external gateway. `compose.dev.yml` and
`compose.prod.yml` define web without a gateway dependency or application
`build`; the selected endpoint is explicitly supplied by Ansible. Web and
its selected container backend share a deployment-specific network. Gateway
port 6666 is internal unless explicitly exposed; web publishes host port 8000
by default to its container listener at 8000. Native web uses its own listener.
Both profile files use equivalent bind mounts and secret access. Apply
per-container `json-file` rotation (`max-size: 10m`, `max-file: 3`), not
daemon-wide configuration.

The Ansible entry points share a role or task library for validation, input
resolution, image selection/build, preview, record coordination, apply and
verification. Put documented user defaults in role defaults, not play `vars`;
explicit extra vars retain Ansible's higher precedence. No entry point
implicitly invokes playback, proxy or egress plays. Existing native services
are not stopped by an ordinary rerun; an approved mode transition explicitly
lists the services to stop and start. External and dummy modes never inspect
or require a serial device. Local builds require a clean controller checkout,
identified commit, chosen target platform and verified transferred artifact;
they never silently select Hub application images. Base-image network access
is a separately reported build prerequisite. Dummy's local artifact identity
is distinct from the pinned Hub web image.

The dummy overlay preserves dummy state separately and provides the same web
config/state/log/secret mounts and web security environment as other Docker
layouts. Its TUI and host port exposure are explicit development settings.
Its presence does not imply controller or audio acceptance. Fixed container
names and port collisions must be reviewed before supporting multiple
deployments on one host; do not claim concurrent instances work without tests.

#### Configuration mapping and compatibility

Preserve the following public input names initially. The names are not a
promise that every input is supported by every intent; reject unsupported
nonblank inputs instead of silently ignoring them.

| Current input | Future interpretation | Compatibility boundary |
|---|---|---|
| `russound_publish_port`, `russound_host`, `russound_port` | Docker host port, native host and native port, respectively | Preserve their distinct meanings; test listener/healthcheck and conflicts. |
| `russound_manage_serial_gateway`, `russound_serial_device`, `russound_serial_options`, `russound_ser2net_bind` | Managed versus external gateway, device, serial settings and gateway listener | Retain names; require valid device/permissions only for a managed hardware gateway. False requires an explicit external endpoint. |
| `russound_backend_host`, `russound_backend_port` | Explicit effective web TCP overrides | Blank optional input means unset; validate nonblank values before apply. Preserve saved web configuration and test removal and partial overrides. |
| `russound_playback_agents` | Optional list rendered for web; credential references only | Only an actual empty list becomes blank; nonempty list must validate. Test both templates through their real parsers, quoting and secret-file permissions. |
| `repo_url`, `repo_version`, `app_dir`, `venv_dir` | Existing native/source location inputs | Map explicitly for native mode; preserve paths and refuse ambiguous or dirty source. Docker image selection must not silently inherit `main`. |
| `russound_use_published_images`, `russound_web_image`, `russound_ser2net_image`, `russound_published_pull_policy` | Earlier published-image play controls | Do not reinterpret `always` as an explicit update or silently replace a recorded pin. Provide a diagnostic mapping to new intents or stop with migration instructions. |
| `russound_verify_backend` and proxy/agent/egress inputs | Controller check and separately managed playback controls | Preserve verification intent; never equate web HTTP readiness with controller/audio success. New web intent does not mutate proxy/agent/egress. |
| Existing web config/state and secrets | Existing bind mounts or native paths, independent durable assets | Preview exact source/destination paths and ownership; never overwrite existing destination or copy credentials into records/logs. Mode switch is migration, not an ordinary rerun. |

Web runtime compatibility: leave valid saved fields and unknown fields
intact, including file-only notes. Do not equate missing configuration
with malformed existing configuration: preserve setup-required behavior for
a genuinely absent first-install config, but stop before modifying an
incompatible existing file. Provide field-specific diagnostics without
printing sensitive values. Blank optional environment inputs are unset;
nonblank invalid explicit inputs fail rather than fall back. Keep the
existing explicit `--daemon` manual interface unless its deprecation is
separately approved; deployment units do not use it. Document any necessary
exception to source precedence and test config/editor round trips before
enforcing stricter saved-file validation.

The current Docker `.env` is generated and rewritten, whereas manual Compose
may use an operator-owned `.env`. Migration must identify the active ordered
Compose files, shell overrides and rendered effective values before choosing
new input; do not assume `.env` alone describes the live container. Preserve
the current generated file in the backup, not as the only authoritative
source. Existing CLI and playbook names remain documented as legacy until a
tested replacement and an explicit compatibility/deprecation notice exist.

The deployment entry point must control the environment passed to Compose and
provide an explicit generated interpolation file. Do not rely on that file
alone: inherited shell variables can override it. The exact allowlist and
handling of required Docker client settings need implementation and tests.

#### Deployment record, persistent reservation and recovery

This is a proposed single-operator workflow, not an implemented command.
Ansible and Compose perform the deployment. The operator selected a
**persistent host-wide reservation** in place of the earlier long-held OS
lock, SSH lock-holder, transient coordinator, leases and custom IPC.

The private target-side deployment record retains the last verified selection
and, during a change, a pending marker containing the candidate, operation
identity, coarse phase and backup reference when applicable. It includes the
image/source identities and timestamps described below, but no secrets or
raw stream URLs. Reject unsafe paths and unreadable or invalid records.

##### Proposed reservation and operation sequence

Use one fixed root-owned reservation directory in a private, root-owned
local deployment state directory on the target. Every participating entry
point and deployment ID uses the same path. Atomic exclusive directory
creation is the acquisition: if it already exists, fail and report the
existing reservation; never overwrite it or remove it based on age or
presumed owner death. Only verified finalization or approved recovery removes
it.
The reservation must remain after controller, SSH or target process loss,
including across reboot. Manual Docker, systemd and file changes bypass it.
Restrict permissions, reject symlinks and unsafe ownership, document the
supported local filesystem, and surface all creation and persistence errors.
This protects one target host only. Separate-web-host layouts remain supported
for deployment and routing, but coordinated credential rotation across hosts
is outside this rework. Do not start a cross-host rotation through these
entry points or treat a reservation on one host as protection for another.

1. Preview the actual state and obtain the required approval, including any
   backup interruption. Atomically create the host-wide reservation, then
   persist a non-secret owner/operation identity in it. An empty, unreadable,
   partially created or otherwise inconsistent reservation still blocks
   ordinary operations. Persist the creation on the supported filesystem
   before proceeding. Recheck the preview, relevant state and prerequisites;
   drift requires a new decision.
2. Persist the private per-deployment pending record before stopping writers,
   pulling or loading target images, or making any other deployment-affecting
   change. If the reservation or record cannot be safely persisted, stop
   without starting such work. Preserve the last committed selection. If
   creation is interrupted between the reservation and pending record, the
   reservation remains a conservative block requiring investigation.
3. Run only the approved Ansible/Compose work and checks. On detected failure
   stop issuing new work, report the observed state and retain reservation,
   pending, old artifacts and backup. A controller or client disappearing
   does not cancel already dispatched remote or daemon work. Do not rely on
   process death, timeouts or a single snapshot to establish completion.
4. **Separate finalization:** the controller starts a single final target-side
   finalization operation only after the apply invocation has returned
   successfully and all required checks have completed successfully, as
   specified below. It checks the reservation and pending operation identity,
   atomically commits the verified selection and clears pending, then removes
   only its own
   reservation as its last target action. No deployment task, handler or
   background work may be queued to run after finalization. Each record and
   reservation update must use appropriate file/directory `fsync` and surface
   errors; test on the supported filesystem. If reservation removal succeeds
   but its directory `fsync` fails, the reservation may already be absent:
   report an uncertain outcome and follow the exceptional recovery procedure
   below; do not claim other entry points remain blocked.
5. A lost finalization response is **unknown**, not failure or success. If
   finalization was already dispatched, it may finish after the controller
   disconnects and legitimately clear the reservation. Inspect the record,
   reservation and actual services before reporting an outcome. A crash
   between committed-record replacement and reservation removal leaves a
   blocked state requiring manual review. If the reservation is gone, never
   infer success from that fact alone; verify the committed identity and
   actual state. Do not automatically retry uncertain finalization.

The reservation is a persistent exclusion marker for participating
operations, not a daemon fence or proof of a stopped process. It is not
automatically released when SSH disconnects. Recovery must not begin while
prior work may still be active; an unresolved reservation intentionally
blocks ordinary operations even when the owner is gone.

##### Protected inputs and preview drift

Before approval, identify all configuration and protected inputs actually
needed for the selected operation, including credential, certificate, trust
and generated environment sources. Create an operation-specific private
snapshot on the controller and/or target as appropriate; protect it like the
originals, check ownership, permissions and safe file types, and reject
unsafe paths or symlinks. This private staging is preparation, not a change
to active services or configuration. Report its purpose and retain it only
as needed for the operation or investigation; remove the private staging
files according to the documented retention policy, without promising
forensic erasure. Restrict writes to the staging files for the lifetime of
the operation, verify any copy transferred between controller and target
against the approved snapshot, and stop if snapshot integrity is uncertain.

The redacted preview identifies the selected inputs and intended use without
printing sensitive paths, contents, raw URLs, secret hashes or low-entropy
credential fingerprints. Approval covers these staged bytes, not a mutable
path alone. After reservation and before the first disruption, compare each
source to its private snapshot and revalidate ownership and permissions,
then repeat the check immediately before apply after backup. If any input is
missing, unreadable, replaced or changed, stop and request a new preview and
approval. Do not silently substitute the new content. The apply step consumes
the approved snapshot, rather than rereading a mutable source;
verify any resulting installed copy against it without disclosing contents.
The comparison is private and must not expose digests in logs, the deployment
record or error messages.

The snapshot is temporary **deployment input**, not a runtime mount, unit
argument, secret-file reference or service dependency. For each file actually
needed at runtime, install an approved copy to a stable, private target path
with the intended owner and permissions before service start; verify the
installed bytes and access from the service's execution identity. Render
Compose mounts and native units against that stable path, not the staging
directory. Preserve an existing operator-owned file when no replacement is
necessary; never rewrite it merely to satisfy staging. Do not remove the
staged copy while apply or recovery may need it; after verified completion or
explicitly resolved recovery, delete only the operation's private staging
files. Test in disposable fixtures that a restart works with persistent
copies after staging cleanup; do not introduce an additional live service
restart solely for that test.
Backups of earlier files remain subject to the separate retention policy.
If staged content changed intentionally during migration, explicitly preview
and approve the transformation, validate the resulting bytes and use those
bytes consistently; do not quietly substitute an old snapshot.

Mutable live state is not frozen by the reservation. Do not overwrite
operator-managed or application-written configuration based on a snapshot
unless its writers are coordinated as part of the approved interruption.
Recheck immediately before a required write, preserve the original in the
backup, and stop on detected drift. A comparison alone is not an atomic
compare-and-swap against an uncooperative writer: if writers cannot be
controlled or a safe write cannot be guaranteed, leave that change blocked.
This staging and drift policy must be tested for source changes between
preview, approval, backup and apply, including changed permissions and
unreadable files; no secret contents may enter test output or logs.

##### Apply completion and finalization boundary

The implementation must treat apply and finalization as separate invocations.
Apply completes all required Ansible tasks and handlers and waits for every
deployment-related asynchronous job it launched; it must not use detached
fire-and-forget work. Long-running application services are intended
postconditions, not background deployment jobs. Final required task failures,
unreachable hosts, timed-out jobs, failed handlers or failed checks prevent
finalization even if a playbook is otherwise configured to ignore or rescue
an error. A failed intermediate attempt of an explicitly bounded retry is not
itself a final task failure; the retry and loop rules below still apply. A
deliberately skipped optional check is recorded as incomplete; it must not be
reported as a pass. Independently run the applicable software
and routing checks after apply, and record their outcome before finalization.
For the new deployment entry points, required tasks and handlers must not
use `ignore_errors` or recover into success after a required failure. Flush
handlers before a final required completion task; do not schedule subsequent
deployment tasks, handlers or detached work. The controller collects a
structured per-operation result identifying the target, operation, required
task/handler outcomes, async-job completion and independent checks. Missing,
duplicate terminal, failed, rescued or unreachable required outcomes are
failure or unknown, not success. A zero exit code alone is insufficient. The result is
created only after the play exits, never by a task before handlers finish;
the controller verifies the expected required outcome set and check results
before invoking finalization. Use an enabled Ansible notification callback
with a controller-side runner: capture allowlisted events to a fresh,
operation-specific private stream, and validate it after the process exits.
The callback must catch event-recording errors, retain a sticky failure flag,
and include that flag in a final end-of-play record; an absent or invalid
terminal record, missing events or sticky failure blocks finalization. Do not
rely on Ansible to fail the play for a callback exception: the local rehearsal
below showed it can warn and still exit zero. The runner must treat callback
load/configuration failure, truncated output or an operation/host mismatch
as unknown. Test this mechanism end-to-end in S4 before use; if the result
cannot be obtained reliably, fail closed rather than dispatch finalization.

The expected result set is derived from the approved intent and target host,
including each conditionally required check. Required task and handler
identifiers must be stable and unique within that invocation. A handler that
was not notified is marked `not-needed`, not `passed`; a notified handler
must finish successfully. Each required item records `passed`, `failed`,
`not-needed` or `unknown`, with `not-needed` allowed only when its approved
condition does not apply or an empty loop is explicitly allowed. Include the
operation ID, host, invocation end,
task/handler events and the independent checks; incomplete, duplicate terminal,
unexpected or mismatched identities are rejected. The callback or runner must
capture and signal event-stream or reporting failures. The result remains
non-secret and private; it is not an authorization token and cannot by
itself prove a daemon is idle after a lost connection. Report optional
physical acceptance separately as `passed`, `failed` or `incomplete`.
It is not a required software-completion item when the approved preview
explicitly permits deployment without a controller; it cannot then satisfy
the S5 physical-acceptance gate.

For a required task with bounded `until` retries, record attempts separately
from its single terminal outcome. Intermediate unsuccessful attempts may be
followed by success; a final failed, exhausted, unreachable, timed-out or
missing outcome blocks finalization. A retry is permitted only where repeating
the operation is safe; this does not authorize retries of uncertain apply or
finalization. For a required loop, identify each expected item by a stable,
non-secret operation-local identifier or ordinal, not by logging its value.
Establish the expected item set from the approved intent and validated inputs
before the task runs; if a dynamically derived set cannot be bound to that
intent and independently checked, do not use it as required work.
Record one terminal outcome per expected item, as well as the enclosing task's
terminal outcome; all applicable items and the enclosing task must succeed.
An approved conditional skip is `not-needed`, not success. Empty loops are
`not-needed` only when explicitly allowed by the approved intent. Repeated
events for distinct attempts or items are not duplicates; duplicate terminal
outcomes for the same task or item, missing items, unaccounted retries or
contradictory results are unknown and block finalization.

The implementation must establish from actual Ansible callback behavior how
retry, loop, handler and terminal events map to these identities and outcomes,
including the expected item set, without leaking item values. If an event
stream cannot establish that mapping reliably, prohibit that pattern for
required work or fail closed; do not infer success from event counts. Capture
only allowlisted non-secret fields and keep the result private.

Dispatch finalization at most once per operation, only after those results
are known. Disable implicit controller or transport retries for this
non-idempotent invocation. Never infer successful completion from a dropped
connection or automatically retry uncertain apply or finalization. If an
apply or check outcome is uncertain, keep the reservation and pending record
and follow manual recovery. A successful process return is necessary but does
not prove every Docker daemon operation is complete: the implementation must restrict
itself to operations whose client completion and postconditions it can
verify, and leave unsupported or ambiguous outcomes unresolved. The
finalization invocation verifies matching reservation and operation identity,
records the checks, commits and then removes the reservation as above.

S4 integration tests must use the actual Ansible/Compose entry points to
exercise handlers, awaited asynchronous work, an injected failed or rescued
required task, unreachable targets, failed checks, controller disconnection
during apply and during finalization, and a delayed daemon operation. Assert
that finalization is not dispatched before successful completion and that
uncertain work never becomes a success-shaped result. These are acceptance
requirements, not evidence that the present playbooks meet them.
Include a retry that fails before succeeding, exhausted retries, a loop with
distinct successful items, a failed item, conditional and empty loops, and
missing, duplicated or contradictory terminal events. Confirm each yields the
specified result and that no secret loop values enter the event stream.

##### Manual recovery checklist

This is a design checklist, not a validated executable procedure:

1. Stop submitting deployment commands and identify the affected host,
   operation and last known action. Read the reservation and record without
   changing them; note the previous selection, candidate, coarse phase and
   backup reference. An absent, orphaned, unreadable or inconsistent record
   is an investigation case, not permission to start fresh.
2. Check for a still-active deployment or finalization. Inspect relevant
   Ansible/SSH processes, systemd jobs and units, Docker and Compose project
   containers including stopped and partially created replacements, image
   IDs, logs and target files. Use the actual installed project and ordered
   Compose inputs, not assumed defaults. Avoid exposing environment variables,
   credentials or raw configuration in reports. A single status snapshot,
   absent reservation, lost client or elapsed timeout does not settle
   in-flight work.
3. If relevant work could still be active, observe or investigate; do not
   restore, clear the reservation or run a conflicting deployment. A targeted
   intervention requires separate approval and an assessment of its effect on
   playback and any daemon request. If a safe point cannot be established,
   leave the reservation in place and escalate instead of guessing.
4. Once safe, compare actual state to the last verified selection, intended
   candidate and backup. Confirm the backup is accessible and compatible,
   and identify edits made since it was captured. A missing or unsuitable
   backup rules out presenting restoration as safe. Present the findings and
   obtain explicit approval for a specific recovery action.
5. Keep the existing reservation during the approved recovery and recheck
   relevant state. If none exists, only after establishing a safe point,
   acquire a new recovery reservation exclusively before making changes.
   The operator must ensure no other recovery is running; concurrent manual
   recovery is not supported. Restore compatible artifacts or reconcile the
   installation as approved; verify affected services, routing, data and
   remaining limitations. Atomically record the verified outcome, then remove
   the matching reservation. If a step fails or remains uncertain before
   removal, stop, report it and keep the reservation. If removal may already
   have succeeded, use the exceptional procedure below instead of claiming
   an on-target block exists.

**Exceptional absent-reservation outcome:** if removal succeeds but the
following directory `fsync` fails, or the finalization reply is lost, the
reservation may be absent and another entry point cannot reliably infer the
uncertainty. The single operator must stop all participating controllers and
other deployment commands, notify any other operators and prevent a new
operation while investigating; no automatic host-side block is promised.
Do not blindly recreate or delete a reservation while an earlier operation or
finalization may still run. Once the operator establishes a safe point, acquire
a new recovery reservation exclusively, recheck the record and actual state,
then continue only with explicit recovery approval. If another operation has
already started, stop and investigate both; the reservation does not fence
either one. Restore storage reliability and repeat verification before
resuming. If that cannot be established, leave the situation unresolved and
escalate. This is a deliberate single-operator limitation, not a distributed
exclusion guarantee.

The checklist does not promise that every Docker interruption is recoverable
from available evidence; some cases may require further investigation or a
separately approved maintenance procedure. Never equate operator approval
alone with proof that earlier work has stopped.

##### Implementation validation and rehearsal requirements

The persistent reservation and separate finalization are proposed, not proven
mechanisms. Assess their feasibility and unsupported interruption cases during
implementation; earlier lock probes do not validate them. S4 must test the
actual implementation on disposable Linux:
simultaneous exclusive acquisition across deployment IDs; crashes before and
after reservation creation, pending write, commit and removal; controller loss
during apply and finalization; late finalization response; durability errors;
partial changes; manual recovery; backup/restore; and redaction. Include a
second controller and verify no deployment starts while a reservation remains.
Also test the protected-input and finalization boundaries above, including
runtime restart after staging cleanup and the absent-reservation error case.
S5 retains separately approved Pi and hardware acceptance.

**Readiness assessment, 2026-11-13:** S0 is closed with the limitations stated
above; further exhaustive inventory is not a prerequisite for implementation. The
snapshot-lifetime, absent-reservation and callback-failure gaps have specified
behavior and small local probes. At the operator's direction, S1 and S2 are
closed as a starting point for S3/S4, not as proof that the design works.
The callback's sticky-error and retry/loop protocols, safe staging with actual
consumers, supported filesystem durability and end-to-end backup/restore remain
unverified. Investigate and fix material issues as implementation proceeds;
do not bypass required tests, fail-closed behavior or cutover safeguards.
Actual new entry points, Ansible/Compose fault injection and physical
acceptance remain S4/S5 work and must not be claimed as passed by synthetic
probes.

#### Image and update flow

For managed dev, resolve the requested channel to one revision, resolve each
immutable `dev-<revision>` reference and verify registry index, selected
platform manifest and image-config revision/architecture. Refuse missing or
changed/inconsistent results; no downgrade or cross-channel fallback. Record
both manifest levels and deploy by digest. Matching revision/platform is the
agreed dev pairing criterion, not proof of hardware compatibility. For an
external gateway resolve web only. Stable requires an approved stable release
and fails before mutation until one is available. The explicit local build
records build revision, platform, archive checksum and loaded image identity.

Normal reruns use committed identities; a missing cached image may be fetched
by the same digest, but failure must not change the pin. An explicit update
previews a complete new candidate, then creates the pending marker. An
update check compares the same digest level and does not pull, mutate state or
restart. Moving aliases may differ temporarily; report an incomplete pair
instead of claiming current or mixing revisions. Unknown fetch times remain
null, and deployment time is not a substitute.

#### Redacted preview and implementation test matrix

Example preview shape, with synthetic identities, not executable output:

```text
intent=dev layout=managed platform=linux/arm64
web=itcornernet/mpdrussoundpi-web@sha256:<index-digest>
gateway=itcornernet/mpdrussoundpi-ser2net@sha256:<index-digest>
revision=<same-source-revision> previous=<recorded-or-adoption-required>
services=web,gateway expected_interruption=web,gateway
playback=untouched backup=required-for-migration
checks=web-http,gateway-tcp,controller,agent-if-configured
```

| Case | Required assertion |
|---|---|
| Dev managed, external and dummy; native managed/external | Correct ordered files, images, mounts, endpoint and service scope; unused gateway/device inputs not required. |
| Release unavailable; malformed/blank/unsupported input | Fail before service mutation, explain the field without leaking values; existing data is unchanged. |
| First deploy, same-pin rerun, explicit update, cached/offline | Correct digest and timestamps; no implicit rebuild or update; unavailable pinned digest fails. |
| Ansible inventory, extra vars, shell/Compose env and native CLI | Real precedence and rendered process values match the contract; no-agent, valid-agent and malformed-agent cases reach the runtime parser. |
| Record and deployment failure | Previous commit survives; pending blocks rerun; errors and observed changes are reported. Inject record write, `fsync`, rename, service and verification failures. |
| Two controllers, deployment IDs and manual drift | Exclusive host-wide reservation prevents overlapping participating operations and remains after interruption. Detect and report manual drift, which bypasses it. |
| Config/state migration and restore | Unknown fields, secrets, SQLite consistency, permissions, local-only image and all relevant units/drop-ins survive; no unapproved overwrite or automatic rollback. |
| Logs and dependencies | Exact profile/rotation behavior, redaction and write failures; fresh bounded dependency resolution and no unconditional upgrade on rerun. |
| Routing/health | Separate web HTTP, gateway TCP, actual controller response, agent TLS/auth and audio; missing hardware is explicitly incomplete, not a pass. |
| Agent staged update | Active paths unchanged until activation; interrupted activation preserves evidence and prompts recovery. Cross-host credential rotation is deferred. |

Run render and record/recovery tests on disposable data first, then an isolated
Linux/Ansible integration environment. Pi/network/hardware acceptance and a
six-hour observation are later, separately approved operations; none is
established by these proposed tests or by a documentation commit.

#### Local rehearsal, 2026-11-13

**Reservation and finalization design probes:** a disposable Linux container
using cached `python:3.12-slim`, no network and a temporary filesystem ran
six small model tests: 12 competing processes produced one reservation owner;
owner exit and an incomplete reservation blocked another acquisition; a crash
after commit retained the reservation; a wrong owner was rejected; and a lost
reply after finalization could coexist with a committed record and no
reservation. A later seven-test model exercised protected-content and mode
drift, snapshot-to-installed-copy survival after staging removal, required
result rejection, exclusive acquisition, owner exit, and a simulated directory
`fsync` error after reservation removal. All passed. These models do not
implement or validate production staging, callback/runner integration, real
`fsync` errors or power-loss durability.

A separate disposable local Ansible core 2.21.5 play awaited an asynchronous
task, triggered a handler and deliberately rescued a failed task. The handler
ran, but Ansible returned exit code 0 with `failed=0` and `rescued=1`. This
confirms why exit status alone is insufficient; it does not test the proposed
structured result or real deployment playbooks. A subsequent disposable
notification callback captured the rescued task's failure, handler start and
completion, and end-of-play event despite the zero exit. When its event path
was deliberately inaccessible, Ansible printed callback warnings but still
returned zero. An initial callback run also warned that unrelated files in
the fixture directory were not plugins; the repeat isolated the callback.
A second disposable callback caught an injected write error on the failure
event and emitted a terminal `error` flag; without injection it emitted the
failure event and a terminal `complete` flag. Both plays still exited zero.
This tests the basic sticky-flag idea only, not the complete protocol,
runner validation, dropped terminal records, multi-host identity, actual
deployment or remote disconnections.
All fixture files and containers were removed. None touched the Pi.

A later disposable macOS controller-side probe used a separate Python
`fcntl` lock holder and a real local Ansible play with two simulated tasks.
While the holder was alive, a second process could not acquire the lock.
After the first task started, the probe terminated only the known holder;
another process acquired the lock while the first task was still running.
The unmodified Ansible invocation then completed that task **and dispatched
the second task**. The first attempt failed before starting the play because
Ansible rejected `/dev/null` as its configuration file; a temporary `.cfg`
fixed the fixture and the retry passed. Temporary files were removed.

This confirms that merely observing the loss of an independently held lock
does not stop a running Ansible play. The fixture did not use SSH, `flock`,
sudo, Linux, remote tasks, Docker or a deployment guard, and did not test
whether terminating Ansible stops remote work. It does not validate the
subsequently selected persistent reservation. No Pi service was touched.

A disposable controller-side prototype used Docker Compose v5.6.1 to render
synthetic web-only, managed-gateway and dummy layouts. It checked the selected
services, missing required image/device/endpoint failures, absence of
application builds, gateway port not published, read-only web secret mount,
web port, logging options, blank and valid agent environment handoff, and
shell override of a project `.env` port. No containers were started, no image
was pulled and no actual Ansible play or Pi service was used. This prototype
is not the future Compose implementation; it did not test production release
policy, application startup or network routing.

The same rehearsal ran real separate local processes on macOS and a temporary
filesystem. An advisory lock blocked a second process while held and became
available after its holder was killed. Six simulated process exits around
atomic replacement for prepared/applying/verified states retained the old
committed identity; after replacement, the prototype blocked ordinary reruns
on pending state. Two further simulated exits around final promotion left
either the old identity with `verified` pending or the new committed identity
with no pending state. Injected file-fsync, replace and directory-fsync failures
were reported; an error after replacement left the new pending state visible
but durability uncertain. Invalid and unsupported sample records were
preserved and rejected. These checks cover a small synthetic schema, not the
proposed complete schema, power loss or the Pi's Linux filesystem.

**Recovery design finding:** a separate worker outlived its lock-owning
coordinator, and changed a disposable file while another process held the
released lock. Consequently, reacquiring the lock by itself is insufficient
to authorize recovery. The worker in this probe was a local stand-in, not
Docker or systemd. Recovery must investigate potentially active client and
daemon actions and remain blocked when their state is uncertain. Network loss,
real concurrent controllers, power-loss durability, full record validation,
backup/restore and actual Ansible/Compose application remain untested; S2 is
still open.

#### Disposable Linux and Ansible rehearsal, 2026-11-13

The controller used Ansible core 2.21.5 and `community.docker.docker` to run
a synthetic fixture in a disposable, network-disabled ARM64 Linux container
from an already-cached Python 3.12 image. Its root filesystem was read-only
and `/tmp` was a temporary filesystem; no Pi, host bind mount, production
container, registry or application service was touched. The initial Ansible
attempt failed because its default `/root/.ansible/tmp` was read-only. With
`ANSIBLE_REMOTE_TMP=/tmp/.ansible`, the fixture and repeat run passed.

- A separate Linux worker continued after its lock-owning process exited and
  modified a temporary file while the test process held the reacquired lock.
  Two successive real Ansible tasks also each acquired the same nonblocking
  lock: the first task's process had exited before the second began. These
  observations showed why independent lock acquisitions do not span Ansible
  tasks and why recovery needs careful investigation. The later reservation
  design replaces the proposed long-held lock; this probe did not test it.
- Process exits immediately before and immediately after final record
  replacement produced respectively the old committed value with pending
  state and the new committed value without pending state. Record file mode
  was `0600`. This is a tiny synthetic record, not full schema validation,
  power-failure or crash durability on the Pi's filesystem.
- SQLite's online backup API copied a disposable WAL-mode database. The copy
  passed `integrity_check`, contained a row committed before the backup, and
  excluded a later committed row. This establishes only that single snapshot;
  it does not verify concurrent writers, coordination with other files, a
  complete off-host backup or restoration of a real ledger.
- The synthetic play passed twice; the second run reported no changed tasks.
  That does not establish idempotence of any deployment playbook.

The temporary fixture and container were removed. The Linux kernel and
filesystem belong to Docker Desktop, not the Raspberry Pi. Linux filesystem
durability under power loss, actual reservation integration and daemon-side
actions, multiple real controllers, image resolution, migration and restore,
network routing and hardware acceptance remain unverified. S2 closure does not
turn these limitations into passes; address them in the applicable later gate.

#### Docker client-loss rehearsal, 2026-11-13

In a disposable network-disabled Linux container from the cached Python image,
with a read-only root and temporary `/tmp`, a `docker exec` worker waited for
a test signal. After the worker was ready, the controller killed that specific
Docker CLI process. A separate exec confirmed the worker was still alive; it
then received the signal and completed a change to a disposable file. The
container was stopped and removed. No Pi, host bind mount, production service
or registry was involved.

This directly demonstrates that a Docker client exiting is not a general
cancellation guarantee. It covers a running `docker exec` process only; it
does **not** establish how Compose updates, image pulls, container creation,
systemd jobs or every Docker daemon request behave when their clients die.
The simplified runbook must account for this uncertainty; this experiment
does not validate a recovery procedure.

#### Docker and Compose stop client-loss rehearsal, 2026-11-13

Two separate disposable, network-disabled ARM64 containers used a cached
Python image, read-only root and temporary `/tmp`. Each had a signal handler
that reported receiving SIGTERM, waited five seconds, and exited normally.
After observing the signal, the test killed the specific local client process:
first `docker stop --timeout 20`, then `docker compose stop --timeout 20` in a
uniquely named disposable Compose project. In both cases the container later
exited with code 0 after its client had died. In the direct Docker case, a
status inspection immediately after client death still showed `running`.
The named containers and Compose project were removed afterward; no host
bind mounts, production services, registry or Pi were involved.

These are controlled demonstrations of a **stop** request continuing after
client loss. They do not establish a general completion fence or prove the
outcome of pull, start, create or remove operations. The separate recreate
experiment below covers one specific recreate interruption. Docker Desktop's
Linux daemon is not the Pi daemon. The recovery design must keep pending
state and treat a running snapshot as potentially transient. Recovery after
client loss or daemon restart still requires investigation.

#### Compose recreate interruption rehearsal, 2026-11-13

A uniquely named disposable Compose project used a cached ARM64 Python image,
no network, read-only root and temporary `/tmp`, with no host bind mounts.
The initial container reported an `old` revision and delayed shutdown for
six seconds after SIGTERM. A second `compose up --force-recreate` selected a
synthetic `new` revision. After observing the old container receive SIGTERM,
the test killed both specifically identified Compose client processes.

Eight seconds later, Docker reported **two** project containers: the old
container had exited, while the replacement carrying the new revision existed
in `created` state and was not running. Thus, for this interruption point,
the daemon completed the old container's stop but the overall Compose
recreation did not complete. A recovery check that looks only for the old
container or only for the replacement would miss part of the actual state.
The test then removed this project's containers; no Pi or production service
was touched.

An initial attempt failed before the client-kill step because macOS `ps`
did not support the requested session-ID column; its disposable project was
cleaned up. The successful retry used the process group of the specifically
spawned client to identify and kill its own processes. This was one Docker
Desktop/Compose version and one interruption point, not proof of every
recreate outcome or that no further daemon action can be in flight after a
particular delay. Other handoffs, missing events and daemon restarts remain
unverified. No recovery or deployment implementation has been validated by
this experiment.

### Image identity: proposed resolution and recovery

The [development publisher](../../.github/workflows/images-build-only.yml)
builds both components for `linux/amd64` and `linux/arm64` with OCI revision
labels; the release policy defines `dev-<full-sha>` and moving `dev` tags.
The aliases for separate repositories can move independently, so matching
names or resolving two `dev` tags at different times is not sufficient proof
of a consistent pair.

On 2026-11-12, a read-only public-registry check of the previously published
`dev-83f3e405b9260a80273ba5929da8da17072a85d8` pair verified both index
digests and the digests of their `linux/amd64` and `linux/arm64` manifests and
configuration blobs. Both platform configurations in both images reported
that same revision and the expected OS/architecture. This verifies identity
and platform metadata for that particular pair, not hardware compatibility,
release approval, current moving aliases or Pi deployment readiness. No
images were pulled or deployed by this check.

Proposed implementation for a fresh managed dev deployment:

1. Select one published source revision via the development channel; resolve
   both exact `dev-<revision>` references, not two independently moving
   aliases. If the selection is incomplete or changes during resolution,
   stop with a diagnostic rather than mix revisions or choose an older pair.
2. Verify each repository's registry manifest digest, the requested
   OS/architecture manifest and its configuration revision label. The agreed
   dev pairing criterion is matching verified revision and target platform;
   no durable CI pair record is required. Matching labels do not prove
   hardware compatibility. Reject missing, ambiguous, malformed, mismatched
   or unavailable results. Stable still requires an approved release.
3. Before mutation, show a non-secret candidate record: selected layout,
   source/channel, revision, target platform and per-component
   `repository@sha256` index and selected platform-manifest digests. Record
   the moving tag or channel requested and when it was resolved.
   For an external gateway there is no managed gateway image to resolve or
   pin; specify and verify its endpoint separately. Dummy mode uses an
   explicitly built controller artifact and pinned dev web.
4. Persist a private, versioned **pending** operation record before applying;
   preserve the previous committed pin. After component verification
   succeeds, atomically commit the complete new record and mark the operation
   complete. An interrupted or failed operation must be detected on the next
   invocation and reported as unresolved, not silently retried, overwritten
   or presented as a completed deployment.
5. Normal reruns use the committed digest references and do not resolve moving
   tags. If a pinned image is absent locally, pulling that **same digest**
   may require network access; if it is unavailable, fail rather than switch
   versions. Updates are separately requested. Local builds require equivalent
   source/platform/integrity evidence but must not be mislabeled as registry
   digests.

For each managed image, preserve the requested channel/tag, resolved index
and platform digests, source revision and platform, resolution time and the
time of an actual fetch when known. Record successful deployment time
separately. A cached image does not have a new fetch time merely because it
was deployed again; represent unknown fetch times as unknown, not as the
image creation time or deployment time. For a first installation or adoption
without trustworthy historical data, do not invent timestamps.

A future update check may query the appropriate moving tag and compare its
current digest with the recorded deployed digest at the **same manifest level**
(index to index, or selected platform manifest to platform manifest). A
changed digest is a candidate update, not proof of compatibility or that a
newer version is safe. Validate the full managed component pair and release
policy before offering installation. Report unavailable registry data,
ambiguous or incomplete pairs and unknown prior identity explicitly; do not
infer "up to date" from an error. Checks do not pull, deploy or change the
committed pin. Share the record and check results with
[user-triggered updates](./general_idea_user-triggered-image-updates.md) and
[update status](./general_idea_update-status-and-restarts.md), rather than
independently deriving deployment history. Production stable `latest`
selection remains blocked until approved stable releases are published.

Test initial selection, aliases changing mid-resolution, one missing image,
revision/platform mismatch, corrupt digest, partial pull, offline rerun with
and without cached digests, crash before/after pin promotion, concurrent
invocations and explicit update failure. Also test cached versus fetched
images, unknown historical fetch time, timestamp preservation, matching and
changed digests, index/platform distinction, unavailable registry and checks
that never change deployment state. The operation-record schema and reservation
strategy remain S2 design work, not an implemented guarantee. Stable selection
needs its own approved release; do not infer it from development tag policy.

#### Proposed deployment operation record

Use one durable, private record per target deployment rather than separately
committing a pin and a completion marker. The record contains a schema version,
deployment identity, last verified **committed** selection (or none on a first
install), and an optional **pending** operation with operation ID, candidate
selection, pre-change identity, backup reference and phase. Do not store
tokens, private keys, stream URLs or other secret contents in it.

| State observed | Proposed normal behavior |
|---|---|
| No record, no existing deployment | Acquire the host-wide reservation, resolve or recheck the complete candidate, then persist pending before changes; no committed pin exists until verification succeeds. |
| No record, existing installation | Treat as adoption/migration, not a clean first install. Inventory actual images and ordered overrides; preview and require migration approval. Do not silently adopt a new Hub image or mark the existing deployment reproducible. |
| Committed selection, no pending operation | Normal rerun uses the committed selection. Verify the actual deployment has not drifted; report drift and require an explicit decision rather than silently overwrite it. |
| Pending `prepared`, `applying` or `verified` | Report the unresolved operation, its prior and candidate identities, last recorded phase and known changes. Block normal rerun/update even if the old pin remains; inspect actual services and backup before an operator chooses recovery. `verified` alone is not a committed pin. |
| Verified candidate committed atomically, no pending operation | Candidate is the last verified selection. A rerun still requires an absent reservation; a remaining reservation requires investigation. |
| Unreadable, unsupported, inconsistent or partially written record | Fail closed and preserve artifacts; do not treat it as a new deployment or attempt automatic rollback. |

Before any service mutation, persist the host-wide reservation and then
pending state; test interruption between their writes. Record `applying`
before the first potentially disruptive action; progress does not prove
what actually ran. After verification, the separate finalization operation
writes the committed selection and clears pending in one atomic record
replacement, then removes its reservation as its last target action.
Document filesystem/durability assumptions, file permissions and stale
temporary files. External changes bypass the reservation, so compare
actual state and report drift. Do not delete or restore a stale record
without explicit, compatibility-aware operator recovery.

A throwaway local Python prototype on 2026-11-12 passed four tests for:
blocking a new operation at each uncommitted phase while retaining the old pin;
refusing commit before verification; replacing committed and pending fields
together; and retaining the old record when a simulated incomplete temporary
file exists. The prototype was removed. These are logic examples only: they
do not test real process crashes, power loss, reservation contention, Ansible,
Compose, system services or target filesystems. S4 must test the actual
implementation, including first-install-without-old-pin, crash at every write
boundary, filesystem errors, reservation contention, stale temp files, drift and
recovery; never infer production durability from this prototype.

### Migration rehearsal: proposed checkpoints

1. **Inventory and preview:** identify actual installed images and ordered
   Compose inputs, web/agent/native versions, generated units and mounts.
   Warn that protocol audit will be disabled during migration. A live local
   outage-fix image is not interchangeable with an older Hub tag merely
   because the checkout matches.
2. **Prepare without stopping:** validate inputs and target architecture,
   resolve or build the selected images on the controller, verify integrity
   and check available target space without changing the target.
   Refuse if compatibility, trust or image identity is unknown; do not change
   a pin or infer a safe downgrade.
3. **Approve and back up:** approve the Pi maintenance window and any
   writer interruption first; acquire the host-wide reservation and record pending
   before disruption. Privately save configuration, state, credentials,
   units, drop-ins, prior image/source identities and a restorable local-only
   image. Use an online SQLite backup or stop writers as approved. Verify
   completeness and an off-Pi copy before cutover. Encryption is optional;
   retain backups until explicitly removed.
4. **Apply and verify:** apply only the approved components; update the
   recorded pin only after the selected deployment succeeds. Check web,
   gateway reachability, actual controller response, agent TLS/authentication,
   MPD/audio readiness and preserved intentions as applicable. Report skipped
   hardware checks explicitly.
5. **Failure:** stop, retain the backup and old artifacts, and report completed
   and pending actions. Ask the operator before restoring; verify old
   binary/data compatibility and restore a matching backup if required. Test
   recovery on disposable fixtures before any real maintenance window.

#### Migration and recovery runbook specification

This is the sequence to implement and rehearse, **not an executable Pi
procedure**. Until all checks and approval exist, do not use it to switch
the current installation.

1. **Discover and preview:** snapshot protected inputs before presenting the
   preview as described above; read actual container image IDs, ordered Compose
   files, mounts, runtime environment sources, installed native files, unit
   status, listener ownership and target architecture. Show a redacted
   before/after preview, including the Pi's local outage-fix web image and
   third Compose override. Identify writers, intended components, any audit
   setting change and expected interruptions; reject unexplained drift.
2. **Prepare without stopping:** verify source/platform/digests, dependencies,
   trust, permissions, space and serial/network prerequisites. Check
   configuration against old and new consumers without modifying target
   files or services. Report unavailable readiness checks and backup
   feasibility. Controller-side image preparation may run here; target-side
   image and deployment changes wait until approval, reservation and pending.
3. **Approve the maintenance window:** before stopping any writer or making
   deployment changes, obtain separate Pi approval for the snapshot-bound
   preview, backup
   method, interruption scope and proposed candidate. If the backup result or
   other material facts differ from what was approved, pause and seek renewed
   approval before continuing. Acquire the host-wide reservation, recheck
   drift, protected inputs and prerequisites, and persist pending before the
   first disruptive action.
4. **Capture and verify the backup:** privately save web config/state,
   agent/admin config and installed sources, playback state, credential and
   TLS files, units and drop-ins, audio/MPD fragments, applicable egress
   settings, ordered Compose files, generated inputs, the prior committed
   deployment identity and image/source identities. Preserve the current
   pending record separately; restoring a backup must not silently clear it.
   Archive the local-only outage image in a verified restorable form. Use
   SQLite's online backup API or stop all ledger writers;
   coordinate other mutable files so the snapshot is internally consistent.
   Any writer interruption must be covered by approval and pending state.
   Verify manifest, checksums, permissions, archive readability and transfer
   off-Pi; rehearse restoration using isolated data. Store secrets privately;
   encryption is optional and backups remain until explicitly removed.
   Record snapshot time, included paths and identities, SQLite method,
   verification results and a non-secret backup reference. A SQLite snapshot
   alone does not make other files consistent. Identify all writers and
   report potential loss of post-backup edits before any restore.
5. **Apply and verify:** recheck the backup, approved scope and drift before
   applying. Disable pre-existing protocol audit only after backup, then
   change only authorized components. Independently check HTTP, gateway
   routing, configured agent TLS/authentication, MPD/audio readiness and
   preserved config/state as applicable. Define applicable checks in the
   preview. A failed required check blocks committing the selection; an
   unavailable physical controller check is recorded as **incomplete**, not
   passed. If all applicable software checks pass, the deployment selection
   may be committed with that limitation clearly recorded; S5 physical
   acceptance remains open until the controller is tested. Record follow-up
   observation separately, never as an implied six-hour pass. Apply and
   finalization are separate invocations subject to the completion boundary
   above.
6. **If interrupted or failed:** stop dispatching new work once the failure
   is detected; preserve pending, old artifacts and backup. Report observed
   services, image identities, known changes and uncertainty without secrets.
   Follow the manual recovery procedure above, including investigating active
   or daemon-side work before any approved intervention. Before a restore,
   check post-backup edits, stop relevant writers as approved, and verify
   code/data compatibility. Never automatically restore or clear pending just
   to make a rerun succeed. Verify restored services and routing before
   recording recovery as complete.

The rehearsal must inject failures before and after each disruptive step,
including failed off-host copy, partial archive, unavailable prior image,
SQLite writer contention, mismatched schemas, restart failures and loss of
the controller connection. Test independently that no unrelated service,
checkout or firewall rule changes. Real Pi adoption, restore and a six-hour
observation require their own authorization and evidence.

#### Agent activation and deferred rotation boundary

Stage a candidate agent source tree and bounded-dependency environment in a
separate private path; validate source identity and safe checks without
writing the active installation or token. Explicit activation must report
the agent restart and use the same pending/recovery discipline. Cross-host
credential rotation is deferred from this rework, not implicitly covered by
the host-local reservation. Preserve current credentials; do not rotate them
or claim cross-host coordination as part of deployment. A future design must
cover both hosts, interruption, recovery and authenticated verification before
rotation is offered. The current agent reads one token at startup; do not
promise zero downtime or simultaneous token validity. Do not alter the
currently running agent during design or rehearsal.

### Cleanup sequence

1. Use the S1/S2 baseline and resolve relevant input and compatibility
   questions as they arise; keep the Pi's currently running image, venvs,
   data and existing controller work intact.
2. In S3, fix logging initialization and add focused tests before changing
   logging profiles. Specify and validate clean dependency installation in
   isolation, including the audio venv's currently inherited system packages.
3. In S4, implement the reviewed Compose/Ansible contract and test rendering,
   pins, preflight and failure paths without changing the Pi.
4. In S5, rehearse migration and restore; only then request separate approval
   for any Pi maintenance window and verify playback/routing after cutover.

#### Audio environment: implementation and test boundary

1. Define an audio dependency manifest separately from the agent's HTTP
   dependencies, with reviewed bounded versions and update policy. The
   controller `--help` experiment is a starting point, not proof that
   `python-mpd2` alone covers all runtime paths.
2. Create the replacement in a new path, with system site packages disabled;
   never reuse or modify an existing environment to make it appear isolated.
   Record interpreter, installed dependencies and OS tool prerequisites.
3. Before any service change, run import/CLI checks and focused tests with
   fake MPD, audio and systemd dependencies. Test a fresh target-compatible
   installation, missing tools and packages, and explicit failures. Do not
   exercise installation, cleanup or reconciliation against live playback as
   part of a dry run.
4. Rehearse unit-path changes using disposable configuration and service
   fixtures. During an approved migration, back up the previous unit and
   environment identity, retain the old environment and check agent,
   supervisor, audio and routing independently. Stop on failure and obtain
   operator approval before restoring. Do not assume the previous venv or its
   inherited system packages remain reproducible after unrelated OS updates.

## Configuration and precedence

Use the S0 inventory across web, gateway, agent, audio/MPD, proxy and
Ansible/Compose. For settings affected by a change, establish the relevant
owner, type, default, inputs, current and target precedence, validation,
secret handling, restart requirements and whether they are user-editable.
Investigate material unknowns when encountered rather than reopening S0 for
exhaustive coverage. Distinguish:

1. Application configuration and durable runtime state.
2. Deployment selection and host-specific wiring, including Compose
   interpolation versus environment actually delivered to a process.
3. Explicit CLI options, environment, config file and defaults for the **same**
   setting; the agreed rule is explicit CLI > environment > config file >
   default, only where those inputs actually exist.
4. Ansible variable precedence and generated files as a separate deployment
   layer; do not present Ansible precedence as process runtime precedence.
5. Secrets and certificates, which remain separate protected files and must
   not be copied into public config, logs or API payloads.

Adopt one explicit priority policy for comparable settings or document a
necessary exception with a reason. Validate absent, empty, malformed and
conflicting values consistently, and surface errors rather than silently
claiming a different effective value. Decide compatibility for existing
variables and CLI invocations before changing behavior. Expose provenance to
the configuration and deployment-status ideas without leaking secrets.
Decide the target policy first, then share one effective-value/source contract
with [inline overrides](./web_done_effective-config-overrides.md) and the
[deployment overview](./general_idea_deployment-status-overview.md); do not
let those interfaces independently define precedence.

## Ansible and deployment design

- Separate entry points by operator intent, with a small shared core for
  packages, users, directories, configuration, device permissions, network
  setup and health verification. Avoid copy-pasted deployment logic and
  hidden cross-mode side effects. Keep inventory and host-specific settings
  easy to locate and document each entry point's prerequisites and impact.
- Published image modes use approved Hub references and never build;
  build mode uses an explicit controller checkout/revision, transfers verified
  images to the target and never silently pulls a substitute application image.
  Define when normal base-image pulls occur.
  Explain digest pinning, dev updates, stable promotion and offline deployments.
- Validate inputs, architecture, disk space, serial hardware and required
  secrets **before** stopping a working service where feasible. Preserve dirty
  checkouts and avoid replacing native services unless the selected migration
  explicitly requests it. Make idempotence and failure/rollback boundaries
  visible; distinguish web HTTP readiness, gateway reachability, actual
  controller replies and playback-agent/audio readiness.
- Specify how mode changes preserve or migrate bind mounts, native paths,
  secrets, configuration, state, images and network access. Provide backup,
  verification and rollback procedures, including an explicit no-controller
  acceptance mode that reports incomplete hardware verification honestly.
- Update README, deployment guides, examples and compose commands together.
  Provide a concise decision table and exact commands for each mode; ensure
  existing links and scripts point at the authoritative paths.

## Stages and exit gates

- [x] **S0 — Inventory.** Closed 2026-11-13 at the operator's direction:
  recorded source-level settings, deployment wiring, representative probes,
  known discrepancies and explicit unknowns. Coverage is not exhaustive;
  remaining verification moves to the relevant implementation and acceptance
  gates. No secret contents were recorded.
- [x] **S1 — Agree the contract.** Closed as an implementation baseline
  2026-11-13 at the operator's direction. D1–D9 record agreed choices and
  known remaining details; resolve material compatibility and deprecation
  decisions during implementation. Stable release availability remains a
  separate dependency.
- [x] **S2 — Design and dry-run.** Closed as an implementation baseline
  2026-11-13 at the operator's direction, with synthetic examples and limited
  rehearsals, not end-to-end validation. The proposed entry points, mapping,
  safety controls and migration runbook guide S3/S4; unresolved implementation
  and rehearsal requirements remain in S4/S5. Stable mode remains blocked when
  an approved release is unavailable.
- [x] **S3 — Runtime configuration and logging.** Completed locally
  2026-11-14: strict runtime validation and CLI precedence, shared effective
  endpoint/source metadata, consistent agent/web/deployment credential
  validation, production/development logging and bounded component
  dependencies. Precedence, invalid-value, redaction and regression checks
  pass; API/UI foundations coordinated with the effective-config idea.
  See the [completion record](#s3-runtime-configuration-and-logging-completion-2026-11-14).
  No S3 Pi cutover or audio-environment switch is implied.
- [x] **S4 — Compose and Ansible.** Completed locally: implemented supported
  profiles/entry points and shared tasks, the per-parameter dispositions and
  simplified operator experience above, preflight and non-destructive
  migration controls. Rendering, source/pull behavior, idempotence and failure
  paths pass local/CI tests and Ansible checks for available modes. See
  [S4 controller-side acceptance](#s4-controller-side-acceptance-2026-11-14).
- [ ] **S5 — Migration, documentation and acceptance.** Update authoritative
  guides and examples, rehearse backup/restore and approved layout changes,
  and verify end-to-end routing and health. Live changes require a separate
  maintenance-window approval. Exit: evidence, limitations and rollback
  handover recorded; unavailable stable releases or hardware remain explicit
  blockers/limitations rather than simulated passes.

S0 precedes S1; S1 precedes S2 and remaining implementation. The explicitly
requested Waitress/debug change is an exception. S3 and S4 otherwise depend
on the reviewed S2 design; S5 follows both and the relevant release/hardware
prerequisites. Coordinate UX separately with
[first-time setup](./general_idea_guided-first-time-setup.md),
[diagnostics](./general_idea_guided-diagnostics.md) and
[safe configuration changes](./web_idea_safe-configuration-changes.md);
this plan defines the deployment contract, not those features.

## Local implementation iteration log

1. **Empty agent list and template quoting (2026-11-13).** A regression
   feeding both rendered templates into the runtime parser reproduced the
   default `[]` startup failure. Both templates now render an actual empty
   list as blank; nonempty and malformed values still reach the parser.
   Local Ansible rendering and the repository's Compose configuration
   exposed a separate quote-handling failure for a synthetic path containing
   an apostrophe. Docker env quoting and native systemd quoting were adjusted
   to preserve special characters. The local native check approximates
   systemd parsing; a real Linux systemd check, secret permissions, and
   preflight validation of malformed agents remain outstanding.
2. **External host and image preflight (2026-11-13).** A local play containing
   only the real Docker play's two assertions reproduced acceptance of blank
   external hosts and whitespace-only image references. The assertions now
   reject blank, non-string and surrounding-whitespace values when applicable,
   while external mode does not require a managed gateway image. This is not
   full endpoint or image-reference validation, registry verification, pinning,
   or a safe redeployment workflow.
3. **DNS TCP fallback (2026-11-13).** A ruleset regression reproduced missing
   TCP port 53 exceptions for configured IPv4 and IPv6 resolvers. Generated
   rules now allow UDP and TCP DNS only to those addresses before the existing
   blocked-network rules. Seven egress unit tests passed, including exact
   resolver/protocol rules and the MPD cgroup scope. No nftables rules were
   applied; real DNS fallback and filter activation remain unverified and
   require separate approval.

At the end of iterations 1–3, the focused local deployment suite passed 14 tests, including actual Ansible
template rendering, `docker compose config` and the isolated assertions.
Compose only rendered configuration; no services were started, and no Pi was
contacted.

4. **Composable container layouts (2026-11-13).** Added web-only dev and prod
   layout files and separate managed-gateway/dummy overlays. There are no
   implicit builds, fixed container names or published backend ports; all web
   layouts share explicit persistent mounts and bounded Docker logging.
   Five profile tests pass, including actual Compose rendering and real
   Ansible preview execution. The preview uses role defaults below inventory
   precedence and ignores ambient application settings. It rejects legacy
   mode switches, moving image tags, conflicting unused mode inputs, invalid
   ports and apply requests. It is not a registry or target preflight.
5. **Disposable runtime loop (2026-11-13).** Built a local dummy image and
   used the already cached published ARM64 web digest for revision
   `8f6bf092609bf910eb529684d364d191af5a3ab6`. The first test could not reach
   a host-published port on the internal-only Docker network. The fixture
   first checked web's real HTTP API from inside its container, then added
   a separate web frontend network and verified an ephemeral loopback HTTP
   port as well; the dummy remains on its internal backend network.
   The corrected test passed web-to-dummy controller routing, no-change rerun identity, recreation and
   preservation of config/unknown fields and unrelated state, an unavailable
   pinned image without replacement of running web, and the same dummy acting
   as an external gateway. Its containers and network were removed using the
   unique test project only. This does not prove gateway hardware,
   backup/restore or a complete safe apply workflow.
6. **Documentation consolidation (2026-11-13).** Added one authoritative
   container guide and replaced duplicated Docker launch/build how-tos in
   README with links to it. Architecture, playback and test guides distinguish
   new layouts from legacy stacks. Legacy entry points remain available
   because their replacement and migration are not complete; they are not
   silently redirected. Production is documented as a layout, not an available
   release/deployment workflow.

Validation after these iterations: 611 Python tests ran, 609 passed and two skipped
(including the opt-in container runtime test when not explicitly enabled);
the container runtime test was also run explicitly and passed. All eight
Ansible playbooks passed syntax checks. No disposable test project remains.

S3/S4 and final documentation migration remain open. The controller-only
preview deliberately refuses apply until the reservation, snapshot,
backup/recovery and completion runner are implemented and tested. No Pi was
contacted or changed during local iterations 1–6.

### Early Raspberry Pi target check, 2026-11-13

The operator separately approved replacing the existing web/gateway containers
for a short manual test, leaving the new deployment running only if checks
passed and stopping/reporting on failure. This was not an automated Ansible
apply or acceptance of the unfinished safety tooling.

- Privately archived the prior ordered Compose inputs, container definitions,
  actual local-only web image and web config/state/log/secret assets. Stopped
  the old web/gateway before copying mutable web assets. Verified an off-Pi
  backup copy against its private checksum manifest. No native playback
  configuration or credentials were changed.
- Selected the current published dev pair by immutable digest and verified
  Linux ARM64, source repository and matching revision
  `83f3e405b9260a80273ba5929da8da17072a85d8`. Used stable installed copies of
  the new dev/gateway Compose files and separate private data. Preserved agent
  references and trust files, controllers, zones, sources and unknown config;
  disabled protocol audit only in the backed-up deployment copy.
- **Passed:** both new containers healthy, zero restarts at the first check,
  LAN HTTP 200 for home/config/playback pages, real controller connected and
  four zones loaded. Docker log rotation matched the profile. Native agent,
  supervisor, three MPDs and audio service identities/restart counts matched
  the pre-cutover baseline.
- **Failed:** authenticated agent routing returned HTTP 403. The new project
  received `172.19.0.0/16`, but the existing Caddy source allowlist includes
  the LAN and old `172.18.0.0/16` network only. Health of web and serial TCP
  therefore did not imply playback integration was working. Required fix:
  make network/CIDR selection and proxy authorization an explicit preflight
  and migration step; do not silently broaden proxy trust.
- **Stopped as agreed:** the new containers were stopped after the failed
  required check; old containers also remain stopped. Restart policies on
  both old and test containers are disabled to prevent an unintended restart
  competing for the port/device. Prior policies remain in the private
  backup. Native playback and the proxy remain running, unchanged.
- Shutdown observation: web required forced termination at the 20-second
  stop timeout (exit 137); gateway exited 1. No OOM cause was established.
  Graceful-shutdown behavior remains an additional integration follow-up,
  not a successful shutdown assertion.

**Follow-up explicitly approved by the operator:** add only the new deployment
network to the existing proxy allowlist, validate/reload it, restart the new
stack and repeat the short checks.

- Privately backed up the proxy configuration on and off the Pi. Added only
  `172.19.0.0/16` to the existing source matcher; LAN and old-network rules,
  TLS files and credentials were retained. Caddy validation passed.
- **Second finding:** the installed proxy systemd unit has no `ExecReload`,
  so `systemctl reload` failed without applying the staged configuration.
  Caddy's direct `reload` command against `127.0.0.1:2020` then applied the
  validated configuration successfully with the original PID 909 and zero
  restarts. Added an `ExecReload` command to the repository template and a
  regression that failed before the change. The installed unit was not
  replaced during this test.
- **Repeated acceptance passed:** seven samples across approximately one
  minute reported real controller connectivity, four zones, agent `ok`,
  preserved configuration (apart from the approved audit change), and
  unchanged native process/restart baselines. Both containers remained healthy
  with zero restarts and no OOM flag. LAN home/playback pages returned HTTP
  200. From web, the actual `/v1/health` endpoint returned 401 without a token
  and 200 with the preserved token and verified TLS CA.
- **Final state:** the new `russound-dev` web/gateway project is running with
  `unless-stopped` restart policies on port 8000. Old containers remain
  stopped with restart disabled and private recovery assets retained. Stable
  installed files and private data are under the target-check operation
  directory, not temporary staging. The target's old source checkout was
  not updated.

This short smoke test did not change zones, volume, station selection or play
intentions, and does not establish physical audio quality, fault recovery,
reboot acceptance or a six-hour observation. The network/proxy preflight and
graceful-shutdown findings still require integration into the automated design.

### Returning to local implementation: image selection

7. **Verified development-image candidates (2026-11-13).** Added a
   standard-library, anonymous pull-scope-only Docker Hub metadata resolver.
   Web selects one full source revision; managed ser2net must match it.
   Both moving aliases and exact revision tags must agree, with a final alias
   recheck to detect publication movement. Index, platform manifest and config
   checksums/sizes and source/version/platform labels are validated.
   External and dummy modes resolve web only. No target access, image pull,
   layer download, build, credential lookup or deployment occurs.
   Deterministic fixtures exercise mismatched and missing pairs, moving aliases,
   malformed metadata, ambiguous platforms, corrupt content and invalid scopes.
8. **Public-registry integration correction (2026-11-13).** The first real
   metadata-only resolution failed with HTTP 307 on a config blob; the unit
   fixture had not modeled Docker Hub's CDN. Observed the redirect host without
   logging its signed URL. Config-blob downloads now permit HTTPS redirects to
   the two explicitly allowed Docker CDN hosts, drop authorization headers,
   and reject all further redirects. Token and manifest redirects remain
   forbidden. The second real managed ARM64 resolution succeeded at revision
   `83f3e405b9260a80273ba5929da8da17072a85d8`, matching the manual target trial.
   Regression tests cover credential stripping and rejection of other hosts,
   HTTP, embedded credentials and malformed ports. Unknown future CDN hosts
   fail explicitly, not via a broadened fallback.
9. **Reusable candidates and preview integration (2026-11-13).** Candidate
   JSON is exclusively created with mode `0600`; an existing file is never
   overwritten. Offline reuse preserves references/timestamps and validates
   gateway/platform/schema. Online verify uses exact digests only; check compares
   index digests with a fresh candidate without changing the saved file.
   Inventory can select that file, leave images unset for fresh metadata-only
   resolution, or explicitly request the existing syntax-only digest-rendering
   path. Mixed inputs and incomplete managed pairs fail before network access.
   Real Ansible check-mode previews with an unreachable target pass all three
   gateway modes using saved candidates; conflicting inputs, wrong platform
   and missing files fail without changing the selection. Documentation now
   distinguishes these paths and consolidates image selection in the container
   guide. These files are **not committed deployment records** or approval
   evidence. Transactional apply, persisted target pins, layer transfer,
   protected snapshots and recovery remain open.

Validation of iterations 7–9: the full local Python suite ran 626 tests,
624 passed and two skipped (including the opt-in container runtime test;
container runtime behavior was not changed by this slice). The subsequent
AMD64-specific resolver regression also passed. The new playbook passed an
Ansible syntax check; the new/changed Python modules passed Pylance syntax
checks and whitespace validation passed. A live read-only public-registry
resolution also passed through the actual Ansible-to-Compose managed preview
with an intentionally unreachable target and `changed=0`. No Pi connection,
target mutation or additional running service was involved.

### Safe-apply foundation: persistent state and completion evidence

10. **Persistent target-state primitives (2026-11-13).** Added a standard-library
    state layer for the fixed root-owned `/var/lib/mpdrussoundpi-deploy` directory.
    A host-wide exclusive reservation precedes pending persistence; existing,
    empty, unreadable or old reservations are never automatically released.
    Pending records preserve the previous committed image selection, operation
    UUID, approved non-secret intent identity, expected completion items,
    sequential phase and an operation-local backup reference. Existing committed
    deployments cannot advance past the backup boundary without that reference.
    This records the runner's verified work; it does not create or validate a
    backup itself. Safe path traversal uses directory descriptors/no-follow;
    records reject symlinks, hardlinks, nonregular files, unsafe ownership/modes,
    corrupt schemas and leftover writes. New files are `0600`, directories
    `0700`. Writes and replacements synchronize files and containing directories.
11. **Interrupted operations and separate finalization (2026-11-13).** Local
    subprocess tests prove one winner between competing deployment IDs and
    persistent blocking after process exit, including exit between reservation
    creation and owner persistence. Finalization checks the reservation,
    operation, phase, intent and exact expected passing completion set. It
    persists an exclusive finalization marker before replacing committed/pending
    state, then removes only its own reservation. Mid-finalization failures
    block repeat dispatch; an error syncing the parent after reservation removal
    explicitly reports uncertainty and does not claim the host is still blocked.
    Tests inject sync, pending-write and atomic-replacement failures and retain
    partial evidence instead of cleaning it up. No recovery/unlock mutator is
    exposed. These tests run on the controller's local filesystem, not the Pi;
    real target reboot/power-loss and daemon-quiescence acceptance remain open.
12. **Real Ansible completion evidence (2026-11-13).** Added an opt-in aggregate
    callback and internal controller validator. Allowlisted records contain
    operation/host identity, sequence, stable required-task IDs and outcomes,
    not task names, payloads, arguments or item values. The controller checks
    exact expected starts/terminal outcomes after process exit, all failure/
    unreachable/rescued/ignored statistics and a sticky callback error flag.
    The first real integration run exposed that Ansible rejects `/dev/null` as
    `ANSIBLE_CONFIG` because it has no supported extension. Replaced that
    assumption with a repository-controlled minimal `.cfg`; real plays now pass.
    Successful handlers finish before receipt creation; failed or missing
    required handlers, ignored/rescued failures, unreachable hosts and invalid
    streams block completion. Actual Ansible is used, not a simulated callback
    event ordering. The runner does not dispatch finalization.
13. **Terminal persistence failure correction (2026-11-13).** A callback can
    write complete-looking terminal JSON and then fail its final `fsync`.
    Reading that file and seeing exit zero would not establish successful
    reporting. Added a private inherited pipe: after the last terminal write
    and sync, the callback acknowledges success only when its sticky error flag
    is clear. Missing, failed or duplicate acknowledgements block completion;
    reads are nonblocking after process exit, so an inherited daemon descriptor
    cannot make the runner wait for EOF. A regression reproduces final-sync
    failure with readable success-shaped JSON and requires a failure signal.
    Required retries, loops, conditional skips and async jobs are deliberately
    prohibited by this initial validator, as allowed by the closed design,
    until their richer mapping is implemented. No unsupported pattern is
    inferred successful. This is a restriction on the new internal runner,
    not a behavioral change to existing playbooks.

The primitives also pass a combined fixture in which actual local Ansible
completion is consumed by a separate state finalization. That fixture does
not perform or certify a container deployment or independent routing check.
Operator `apply` stays blocked: target preflight, protected snapshots,
backup/restore, actual deployment/check orchestration, production use of
committed pins and manual recovery still need integration and acceptance.
The Pi was not contacted or changed during iterations 10–13.

14. **Linux/Python compatibility loop (2026-11-13).** The full controller suite
    ran 666 tests: 664 passed, two skipped. Then ran the state tests inside a
    cached ARM64 web image with Python 3.12, network disabled, all capabilities
    dropped, a read-only root/repository and disposable tmpfs test data. That
    exposed an import-time `typing.Generator[int]` failure: Python 3.12 requires
    all three type arguments, unlike the controller's Python 3.13. Corrected
    both annotations to `Generator[int, None, None]`. All 24 Linux state tests
    and all 68 focused controller deployment tests subsequently passed; a final
    combined Linux state/image run also passed all 38 tests.
    Pylance found no errors in the new state, validator or callback modules;
    the callback suppresses only Ansible's missing-third-party-stubs diagnostic.
    The preview playbook syntax and whitespace checks passed. The Linux run
    validates syscall/error behavior on disposable tmpfs, not persistent-disk
    reboot/power-loss durability. No image was pulled, no hardware exposed,
    no network used and no target contacted.

### Protected input snapshots and explicit target preflight

15. **Approved input bytes and stable runtime copies (2026-11-13).** Added an
    internal protected-input API. It exclusively creates one private operation
    directory, copies explicitly selected private single-link regular files,
    and seals a private manifest in memory. Snapshot files are `0400`; directories
    are `0700`. Paths/content fingerprints stay in that private manifest and
    are excluded from diagnostics and public records. Sources are rechecked
    for content, file identity, ownership, permissions and timestamps;
    replacement with identical bytes still requires renewed approval. Copying
    verifies both the open descriptor and current source entry. The shared
    descriptor-based path checker is reused by state and snapshots; existing
    state regressions remain passing.
    Install consumes staged bytes, never rereads a changed source as approval,
    exclusively creates a stable runtime file outside the staging tree, and
    verifies owner/group/mode/content. Existing destinations are preserved,
    not automatically replaced. A separate reader works after staging cleanup.
    Incomplete writes retain staging/partial destination evidence. Cleanup is
    explicit after independently resolved work and removes only known files
    for that operation, not other operations or source/runtime files.
    This first API supports modes `0400`/`0600`, at most 64 inputs of 16 MiB
    each; it has no operator CLI or cross-process reload/transfer protocol.
    Persisting private seal custody and wiring approval/recheck points into
    the runner remain open. It is not a live-state backup or writer fence.
16. **Opt-in first-install target inspection (2026-11-13).** Added
    `russound_action=preflight` to the existing dev entry point. Default
    preview still makes no target connection. Controller input validation and
    Compose rendering run before target inspection. Preflight uses normal
    Ansible target Python discovery, avoiding a duplicate Python-path option,
    then sends the standalone standard-library probe to that interpreter.
    It checks Linux/local Docker/Compose and matching daemon platform,
    rejects namespace-remapped identities, reports free space, checks existing
    projects and overlapping mount data, required existing bind directories,
    mode-based service access, a private secrets directory and host listener
    availability. Managed mode additionally checks device type/group/mode,
    container assignments and open process descriptors; external/dummy modes
    do not need a serial device. Existing/stopped projects or conflicting
    mounts/devices require recorded-rerun/adoption work, not an implicit
    overwrite. No files are provisioned, images pulled, host reserved, service
    stopped or trust broadened. Normal Ansible temporary module files and a
    brief bind/close listener probe are the only preparation side effects.
17. **Preflight integration/fix loop (2026-11-13).** Existing offline Ansible
    regressions caught an accidental malformed task conditional while wiring
    the action; corrected it and verified preview remains target-disconnected.
    Actual local Ansible tests prove opt-in target execution, redacted failure
    reporting and controller validation before any unreachable-target attempt.
    Checked the real Docker CLI output shape: container IDs use JSON-lines,
    and Compose version uses a JSON object; do not assume an array for listing.
    Deterministic fixtures cover incompatible/remote daemons, unavailable
    ports without stopping their listeners, unsafe paths/modes/ACLs and
    existing project/data/device conflicts.
    A Linux check exposed a too-strict generic permission policy for typical
    group-writable serial devices. Only the character-device leaf may now
    have group-write permission; world-writable devices and writable
    directories still fail. A real open pseudo-terminal verifies the process
    holder is detected without hardware. The Linux standalone CLI uses
    fixture Docker metadata with real paths and socket checks; it does not
    mount the Docker socket. Its first fixture invocation failed because the
    container's disposable tmpfs defaults to `noexec`; explicitly enabled
    execution only there for the fixture CLI, retaining a read-only root and
    checkout, no network/capabilities/host hardware devices and automatic
    container cleanup. A final access review added service-mode traversal of
    all data/runtime ancestors: successful reads by the deploying root user
    cannot substitute for service access through a root-private parent.

Preflight reports `safe_to_apply: false`, routing/proxy checks not run, runtime
write access not tested and saved configuration not validated. Space reporting
does not certify adequate backup/image capacity. These point-in-time checks do
not reserve resources, establish hardware readiness or authorize apply.
Recorded-installation preflight, actual runtime-access/config validation,
proxy/container routing, snapshot orchestration, backup/restore and the full
apply/check/finalize/recovery runner remain outstanding. The Pi was not
contacted or changed during iterations 15–17.

Validation: the final full controller suite ran 693 tests, with 689 passing and
four skipped (two existing opt-ins plus two Linux-only preflight checks).
After adding operation-isolation cleanup and ancestor-traversal regressions,
the isolated Linux state/image/input/preflight run passed all 63 tests.
All eight real Compose/Ansible profile tests passed,
including offline-preview compatibility and explicit preflight failures.
Pylance reports no diagnostics in the new production modules or the refactored
state layer; changed Python test syntax, playbook syntax and whitespace checks
passed. No live target acceptance or complete deployment/recovery loop is
claimed.

### Private file backup and isolated container recovery

18. **Verified stopped-writer bundles (2026-11-13).** Added an internal
    streaming file-backup API for explicitly selected roots. Capture requires
    a raising quiescence guard before/during/after work, not a status boolean
    or an assumption that the reservation stops writers. It checks descriptor
    and path identity while copying and repeats the whole source inventory/
    content comparison before the manifest is written. Exclusive private
    operation directories contain read-only blobs and a manifest written last,
    with layout, private source paths, ownership/mode/mtime, sizes and checksums.
    No partial artifact is automatically deleted or retried. A separately
    retained manifest identity is required to reopen/verify a bundle, including
    a copy in another private storage directory and process. Public reports
    omit source paths and content hashes. Limits are 64 roots, 10,000 entries,
    depth 64, 8 GiB of streamed contents and an 8 MiB manifest.
19. **Isolated restore and fidelity failures (2026-11-13).** Verification checks
    strict schema, safe relative membership/parent ordering, bounded numeric
    metadata, exact source coverage and expected payload set before creating
    any restore destination. Restore exclusively creates a separate tree,
    never overlaps original source/backup paths, verifies written bytes by
    rereading them and restores/checks owner/group/mode/mtime. Existing targets
    are not overwritten. Special files, links, special permission bits,
    shared-writable paths and unsupported ownership are blocked. Extended
    attributes, ACLs and filesystem flags also block: silently discarding
    unsupported metadata would not be a valid recovery copy.
    Linux xattr inspection is required; macOS unit fixtures explicitly shim
    that unavailable API, while actual Linux tests use the real API.
    Backups remain until explicitly removed, with no expiry/deletion command.
    Captures accept objects owned by root/capture account under the existing
    safe ancestor policy; mixed-service ownership/adoption needs separately
    verified capture scopes and privileges, not an ownership fallback.
20. **SQLite and interrupted-I/O loop (2026-11-13).** A subprocess writes a
    committed SQLite WAL transaction then exits without clean close. With that
    writer gone, capture includes database/WAL/SHM; isolated restore recovers
    the transaction and passes `PRAGMA integrity_check`, without touching
    original bytes. This implements the stopped-writer option, not online
    SQLite backup or code/schema compatibility. Tests inject short writes,
    silent output corruption, sync/write/metadata failures, source changes
    during and after their own copy, unexpected/missing/tampered files and
    process exit before manifest persistence. Restore errors retain the new
    partial tree and original backup; state/pending/reservation are never
    cleared by file recovery. A malformed/deep-JSON regression initially
    expected one parser error message; some runtimes parse that depth and
    reject the resulting manifest schema instead. Corrected the test to
    require explicit `BackupError` and no destination, not parser wording.
21. **Real Compose/Ansible failed-update recovery rehearsal (2026-11-13).**
    Added an opt-in local test using an already cached ARM64 web digest
    and current dummy source via a test-only image/entrypoint override, no
    image pull/build. Initial fixture runs failed because the reused web
    image's HTTP healthcheck was wrong for the dummy and because direct
    module execution defaults to loopback rather than the dummy entrypoint's
    environment handling. Corrected the fixture with an explicit TCP
    healthcheck, `0.0.0.0`, port and persistent seeded state; no production
    layout was changed for the fixture.
    A backup attempt on Docker Desktop's shared bind mount failed Linux
    descriptor xattr inspection with `EOPNOTSUPP`. Kept production validation
    strict and moved disposable fixture data to a uniquely named local Linux
    Docker volume. Its setup initially changed parent ownership before
    children under a capability-restricted helper; reversed that order.
    Only setup adds `CHOWN`, only for the fixture volume. Subsequent
    backup/restore helpers run as the service user with all capabilities
    dropped, read-only root/checkout, no network or Docker socket.
    The corrected loop passes routing and no-change rerun, persists synthetic
    pending state, stops its own web/dummy writers and verifies them stopped,
    captures a verified backup, then runs a controlled real Ansible/Compose
    apply with a deliberately unavailable candidate and `--pull never`.
    Failure produces no completion receipt; pending remains unchanged and
    another deployment ID stays blocked. Restore uses a new tree and the
    known prior cached image; actual web-to-dummy controller routing passes,
    config/unknown fields/state/secrets remain intact and a rerun retains
    container identity. Recovery deliberately does not clear pending or
    claim live recovery finalization. All fixture containers, networks and
    its named volume are removed by exact project/volume identity.

The rehearsal verifies file recovery with a cached prior image, not image
archive save/load, off-host transfer durability, service-wide writer discovery,
live restore/cutover, power-loss durability or the finished automated runner.
The guard inside the isolated helper relies on the test exclusively controlling
its two stopped writers, checked from the controller before/after helper calls;
this is not offered as a production daemon-quiescence mechanism.
The new backup API has no operator CLI. Automatic apply stays blocked until
service-aware preflight/backup/image handling, protected approval custody,
independent checks and explicit recovery/finalization orchestration are wired
and tested together. No Pi connection or live change occurred in iterations
18–21.

Validation: the final controller suite ran 721 tests, with 715 passing and six
skipped (three opt-in container tests and three Linux-only checks). All 27
backup tests passed on isolated Linux/Python 3.12 with real extended-attribute
inspection. The opt-in real Ansible/Compose failed-update and restored-routing
test also passed with the final backup code. Pylance reports no diagnostics in
the production backup module; changed test syntax and whitespace checks pass.
The full suite still emits socket-resource warnings in existing connector
failure-path tests; no unrelated connector change was made. No recovery test
container or volume remained after cleanup.

### Approved Pi same-pin redeployment smoke test (2026-11-13)

Following explicit approval to test redeployment, verified that the Pi's two
Compose files exactly match the current repository. Used the existing
`russound-dev` project and pinned web/gateway images, without pulling or building
images or enabling the unfinished Ansible apply path.

- An unchanged Compose rerun preserved both container identities.
- Stopped only web and ser2net, then archived deployment inputs and their data
  with numeric ownership, ACLs and extended attributes. Compared the archive
  against the stopped sources and verified an off-Pi copy by SHA-256 before
  recreating containers. The archive is private recovery evidence, not a
  tested full image/system recovery bundle.
- Forced recreation changed both container identities, retained the same
  image IDs and reached healthy state with zero restarts. A subsequent
  unchanged rerun again preserved container identities.
- Three post-redeployment samples confirmed controller connectivity, four
  zones, no setup requirement and playback agent status `ok`.
- Configuration/secret/input hashes and native playback/audio/proxy service
  PIDs, restart counts and active states were unchanged.
- Shutdown remains imperfect: web required forced termination after its
  20-second stop allowance (exit 137, not OOM); ser2net exited 1 on stop.
  Recreation and routing passed, but this does not certify graceful shutdown.

The healthy recreated stack was left running. Private operation evidence is
under the Pi trial directory's `redeploy-check-20261113T2216` subdirectory,
with a verified archive copy in the controller session storage. This was a
manual same-pin Compose redeployment, not an image update, automated Ansible
apply acceptance, physical audio test or six-hour observation.

### Runtime shutdown and import-safe setup logging

22. **Reproduce and fix web shutdown (2026-11-13).** Continued locally after
    the approved same-pin Pi redeployment exposed forced web termination.
    Actual Waitress subprocess regressions first failed: SIGTERM exited -15
    without cleanup, while SIGINT with an open SSE stream produced a
    still-running-worker warning after Waitress's drain timeout. The source
    had no SIGTERM handler and an unconditional watcher loop, and event
    streams could wait 15 seconds on their queue. As container PID 1 the
    unhandled termination signal also explains why Docker needed its kill
    fallback. Added scoped main-thread SIGTERM/SIGINT handlers, restored
    afterward, with repeated termination ignored during cleanup. Shutdown
    rejects new requests, ends SSE streams with half-second stop checks
    while retaining the normal ping cadence, drains/closes Waitress and
    stops/joins the watcher before releasing the gateway connection.
    Missing Waitress or startup errors no longer leave newly started
    background pollers behind. Stopped server objects cannot be reused.
23. **Require explicit background shutdown outcomes (2026-11-13).** Playback
    clients now support a non-blocking stop request; every poller receives
    that request before waiting for individual joins. A stopped poll does not
    send the next health/capability/inventory/favorites request. An
    eight-second poller timeout raises an error instead of returning from
    `close` as if it had stopped. The backend watcher has a ten-second join
    allowance; uncleared HTTP dispatcher workers also fail explicitly.
    Cleanup still attempts remaining clients/connections if one close fails.
    New dev/prod profiles provide a 30-second web stop grace period. Tests
    verify open-stream exit and that an in-flight request completes its
    write before clean process exit; these are not guarantees for
    arbitrarily stuck kernel/network calls. The standalone allowlist route
    fixture exposed reliance on leaked shared-backend state once cleanup
    became real; gave its command a local mock backend instead of requiring
    reachable hardware for a response-shape assertion.
    Final repeated validation exposed a separate readiness-fixture race:
    a bound-but-non-listening local socket can make connect time out after
    five seconds on macOS, racing the HTTP readiness timeout before a
    shutdown signal was sent. Timed child-process output distinguished that
    startup delay from shutdown latency. Replaced that fixture endpoint with
    a real disposable dummy gateway rather than weakening the assertions.
24. **Linux PID-1 recovery loop (2026-11-13).** Extended the disposable
    Compose recovery fixture to bind current web source read-only into the
    cached Python-capable image, as already done for dummy source. No image
    build/pull or Pi change is involved. Opens a real SSE stream, stops web
    with an eight-second Docker timeout and requires exit 0 in less than
    six seconds, no OOM and no unfinished-worker warnings. Then stops
    its dummy writer, runs the existing verified file-backup/failed-Ansible-
    update/isolated-restore loop and rechecks real controller routing and
    retained pending state. This tests current source on Linux/Python 3.12,
    not a newly published application image or the physical gateway.
25. **Remove import-time logging (2026-11-13).** Fresh-process tests reproduced
    global logger changes in all six audio-helper/setup import orders and
    implicit relative debug-file creation. Moved setup logging after CLI
    argument validation and removed audio helper logging initialization.
    Audio and MPD setup explicitly choose INFO or `--debug` DEBUG on stderr,
    replacing pre-existing handlers once to avoid duplicate/missing output.
    Audio setup now accepts `--debug`; MPD setup retains its existing flag.
    Imports and help do not create or alter logging handlers/files. Existing
    `russound_debugging.log` files are not removed. Tests cover exact diagnostic/
    operational/warning output and destinations even after a prior logging
    configuration. Native service behavior otherwise remains unchanged.

These are lifecycle and S3 logging prerequisites, not completion of S3–S5.
Recorded-installation preflight/adoption, runtime config/access validation,
image archive/off-host recovery orchestration, approval custody and the
complete apply/check/finalize/manual-recovery entry point remain open.
Automated Ansible apply is still blocked. Existing Pi pins do not gain
the shutdown fix until a separately built/published image is selected and
deployed; the Pi was not contacted or modified in iterations 22–25.

Validation: the full controller suite ran 738 tests, with 732 passing and six
skipped. The three actual Waitress lifecycle tests and three isolated logging
tests also passed on Linux/Python 3.12 with a read-only checkout/root,
capabilities dropped and no external network. The real Compose/Ansible
PID-1 shutdown and backup/recovery loop passed. Audio/MPD setup regression
tests and dev/prod Compose rendering passed. Pylance found no new production
type errors in the lifecycle/audio logging changes; MPD setup retains its
pre-existing generic-config typing diagnostics. Existing unrelated resource
warnings are not treated as shutdown acceptance. After the readiness-fixture
correction, all three lifecycle cases passed five consecutive runs (15
checks), then the complete 738-test suite and container recovery loop passed
again. Disposable recovery containers and volumes are removed after the test.

### Prioritized local-build preparation (2026-11-13)

The user requested committing the existing implementation before starting this
slice. Commit `161853e0599e289c9e12008186573a625e5272b9` records the prior
deployment foundations, runtime lifecycle and setup-logging work.

26. **Explicit clean-source controller build.** Added a standard-library
    builder using one full commit and Git tree, refusing dirty/untracked source
    without resets. A committed-source archive excludes ignored files and Git
    internals; links/submodules are refused. Builds pin a verified local Docker
    context and matching Docker-driver Buildx worker, not a possibly remote
    selected builder. Managed mode builds a web/gateway pair; external/dummy
    build web only. Target platform and source labels are checked on actual
    outputs. Network for Dockerfile package downloads requires explicit opt-in;
    base-image resolution can still access a registry. Mutable dependencies
    mean source identification is not a reproducible-build claim.
27. **Private portable artifacts and offline preview.** Each UUID operation
    retains private logs, read-only Docker-save archives and a strict manifest,
    whose digest is retained separately. Verification checks archive bytes,
    metadata, source/platform/config and classic/OCI identity, including OCI
    blob hashes and the root-to-config descriptor graph, without extraction,
    Docker or network. A copied bundle is checked again. The new
    `deploy-dev-build.yml` reuses shared dev rendering/preflight tasks and exact
    IDs; it never implicitly builds, contacts a target in preview, transfers,
    loads or applies. Mixed Hub/local inputs fail. Apply remains blocked;
    local candidates are not disguised as registry candidates in state records.
28. **Actual image build/test/fix loop.** Built the repository ARM64 pair from
    a separate clean worktree at the committed baseline. Testing the exact
    packaged web, rather than a source-mounted substitute, exposed HTTP 404
    health failures: private controller umask `077` had turned staged static
    directories into `0700`, inaccessible to a different Compose numeric UID.
    Explicit directory modes inside the private staging tree fixed the cause;
    added a direct umask regression and retained private outer directories.
    Rebuilt, verified and actually reloaded both application archives into the
    controller daemon with exact identities preserved; executed the ARM64
    gateway binary successfully. Then passed actual web/dummy routing,
    unchanged runs, clean PID-1 shutdown with SSE, deliberate Ansible update
    failure, isolated data restore and routing with retained pending state.
    A real scratch pair additionally passes save/copy/offline reverify/tag
    removal/reload/inspect. Full suite: 755 tests, 749 passing, six skipped.

This completes controller build/artifact preparation and local-build preview,
not target deployment support. Approval-bound artifact transfer, target
verification/load, classic/containerd loaded-ID reconciliation, local-candidate
pending/committed records and full apply/recovery orchestration remain.
The Pi was not contacted or changed; no six-hour observation was started.

### Approved local-image Pi trial (2026-11-13)

After controller build validation, the user explicitly approved deploying the
verified local images to the Pi. This was a controlled manual trial, not an
invocation or bypass of the still-blocked automated Ansible apply path.
Current application/Dockerfile source matched clean revision
`161853e0599e289c9e12008186573a625e5272b9`; uncommitted changes concerned
deployment tooling, tests and documentation.

- Transferred managed ARM64 bundle operation
  `c5e0807e-06af-4941-8c84-93999ab18d7d`; independently verified on the target
  against separately retained manifest identity
  `sha256:e9894868b57c8af089c033f1b30c3d0ce2a7c2876996d353a897b89efac3689f`.
  Actually loaded and inspected both images before stopping any service.
- Exported both previous exact image IDs, copied those archives off-Pi and
  independently verified checksums. Stopped only the existing project web/
  gateway writers, captured config/data/secrets and original Compose/env
  with numeric ownership, ACLs and xattrs, compared against stopped sources,
  copied off-Pi and verified checksum
  `8f65068b6d4901daa84c42e4a4fecbe48cf86d1c945fd99db4f0e2b42c594faf`.
- Replaced only the two image selections plus the current Compose files
  (the sole layout difference was web's 30-second stop grace). Used exact
  target-confirmed IDs with `--no-build --pull never`. No target build,
  registry fallback, project-network recreation or proxy trust edit occurred.
- Web now runs
  `sha256:f64f0fa487751a503b1f8a74216c1af1b742e9acde1241d8512e20aa5090454d`;
  gateway runs
  `sha256:4f48b8044c2044632bfffed7ae1850e4a80d01379ad032e03e678fd066be1f5e`.
  Both are healthy at revision `161853e`, with zero restarts and no error/
  traceback/unfinished-worker lines in the final available container logs.
- Five routing samples passed: HTTP 200, physical controller connected, four
  zones, no setup prompt and playback agent status `ok`. LAN HTTP 200 also
  passed. Unchanged Compose rerun preserved container IDs. Opened real SSE,
  stopped web, verified exit 0 in 2.23 seconds with no OOM, restarted the same
  approved image and rechecked routing.
- Config and secret hashes, native playback/audio service PIDs/restart counts/
  active states, and existing Docker network identity were unchanged. Native
  services were not redeployed or restarted. Configuration was not rewritten.

The approved local images remain running at `http://192.168.1.105:8000`.
Private target evidence/backup is under the existing trial directory's
`local-image-trial-c5e0807e` subdirectory, with independently verified backups
and result retained off-Pi. Old images were preserved, not pruned. This short
trial did not test audio output, exercise rollback or start a six-hour
observation. Automated local-build transfer/load/state/apply integration
remains unfinished despite this successful manual deployment.

### Reserved local-image state and loading (2026-11-13)

29. **Keep local candidates distinct from registry records.** Extracted shared
    portable manifest validation so the builder and persistent state use one
    schema without circular dependencies. Local candidates retain the approved
    manifest identity and portable config digests, not controller paths.
    Local pending/committed records additionally retain exact target
    `loaded_images`; registry record structure remains unchanged. Local
    operations cannot reach `apply-complete`/`checks-complete` without those IDs.
30. **Implement the one-shot target-local load boundary.** The internal loader
    verifies already-transferred private bytes against the exact candidate and
    requires the matching reservation/intent at `backup-complete`. A durable
    exclusive load marker precedes Docker mutation. It streams held verified
    descriptors, rejects unrelated operation-tag collisions and incompatible
    platform/namespace/remote-daemon configurations, rechecks bytes and actual
    loaded source/platform/IDs, then persists target IDs. Classic config IDs
    are accepted only when they match the archive's verified config; OCI-root
    IDs remain supported. Partial pair failures, timeout, process death,
    changed archives and uncertain receipt persistence keep reservation and
    pending state, never automatically retrying or rolling back. Loading alone
    does not advance service phases or finalize.
31. **Exercise real and failing paths.** Fifteen focused tests include a real
    local scratch build/export/private copy/tag removal/reserved stream-load/
    inspect loop, and simulated classic-store identity conversion. Fault tests
    cover missing/mismatched authorization metadata, partial pair load,
    malformed inspection, collisions, archive replacement, timeout, child
    process loss after marker sync and uncertain marker/receipt writes. The
    existing full suite passes: 770 tests, 764 passing, six skipped with real
    build/load tests opted in. Changed production modules have no Pylance
    diagnostics. No Pi connection or service mutation occurred.

These are internal APIs only. State metadata is not human approval or backup
proof by itself: the future runner must custody those facts before setting
the phase. SSH transport, approval/input handoff, recorded-installation
adoption, runtime config/access checks, complete image/file recovery and
service apply/check/finalize orchestration still need integration.
The public Ansible apply gate remains closed; the successful manual Pi trial
above is not retroactively an automated-workflow acceptance test.

### Reviewed controller-to-target artifact handoff (2026-11-13)

32. **Bind reviewed artifacts to explicit SSH target/trust.** Added an internal
    in-memory transfer review sealing candidate, deployment/operation/intent,
    host/user/port, remote tooling/destination and existing host-trust bytes.
    A mismatched acknowledgement or changed source/trust fails before SSH.
    The client ignores ambient config, uses strict known-host verification,
    disables forwarding and snapshots reviewed public host-trust bytes into
    exclusive private evidence. Existing optional private authentication keys
    are not copied to the target or fingerprinted in deployment records.
    Remote command paths are shell-quoted; no shell is used on the controller.
33. **Persist receive uncertainty before writing artifacts.** A matching
    reserved local candidate/intent in prepared/inputs-verified state is
    mandatory before receiving into a new private operation directory. Bounded
    length framing names only the expected files; no tar extraction or target
    build/pull/load occurs. The exclusive durable transfer marker survives
    process exit, truncation, corruption, interruption and uncertain writes.
    Complete archive/config/platform/source/checksum verification precedes
    a durable verification flag and receipt. Unverified transfer markers block
    reserved loading. No automatic retry, overwrite, cleanup, phase advancement
    or reservation release is performed.
34. **Validate actual build/SSH/load and failure paths.** Sixteen focused cases
    cover framing, conflicts, drift, timeout, missing receipts, child-process
    loss and marker/verification persistence failure. A real isolated loopback
    sshd, with disposable keys and a forced non-root fixture receiver, proves
    strict correct-key delivery and wrong-key refusal. With build opt-in the
    same test builds a clean scratch pair, removes its unique tags, transfers
    over SSH, verifies target bytes, performs reserved Docker stream loading
    and inspects exact identities. Its specific server and tags are cleaned up.
    Full suite: 786 tests, 780 passing, six skipped. Production module Pylance
    diagnostics are clear. The Pi was not contacted or changed.

This closes the internal artifact transport boundary, not the whole deployment
workflow. A review digest is an acknowledgement mechanism, not independent
human-approval evidence. The future runner must custody that approval, protected
inputs and verified backup results. Trusted target tooling/Python and fixed
root-owned state must already exist; bootstrap and privilege/become integration
are not provided. The controller temporarily spools a complete wire copy and
requires corresponding free disk space. Human approval/input handoff,
recorded-installation adoption, runtime access/config checks, off-host recovery
integration and the final service apply/check/finalize entry point remain open.
Ansible apply stays blocked.

### Target-local transaction integration (2026-11-13)

35. **Connect safety boundaries in one owned attempt.** Added the internal
    development transaction runner: verified local selection, protected
    playbook/inventory/input snapshots, exact non-secret review acknowledgement,
    repeated preflight/custody checks, reservation, stable private installation,
    scoped writer-stop/quiescence hooks, actual file backup, required off-host
    verification hook, reserved image loading, actual controlled Ansible
    execution, exact independent-check reports and finalization. Private extra
    variables carry target-loaded IDs and installed input paths. Backup identity
    stays private. No automatic retry, rollback, release or cleanup is added.
36. **Retain uncertain runtime and persistence outcomes.** Failures after
    reservation retain pending state, installed inputs and recovery evidence.
    Updates retain the previous committed selection until all stages pass.
    Controlled Ansible now has a ten-minute deadline; target descendants may
    continue after timeout, so that result explicitly blocks recovery/retry
    instead of claiming the target is idle.
37. **Exercise the actual local transaction.** Fifteen new tests cover ordering,
    review/input drift, uncertain stage boundaries, failed recorded updates,
    incomplete checks and finalization failure. Actual Ansible consumes private
    snapshots/variables and requires the complete callback. Docker opt-in adds
    a real clean scratch build/export/remove/reserved-load, actual image
    inspection and finalization in that same transaction. Full suite:
    801 tests, 795 passing, six skipped. Production diagnostics are clear and
    completion callers remain compatible. The Pi was not contacted.

This closes the target-local sequencing boundary, not remote operator
orchestration. Concrete platform hooks still need scoped writer control,
recorded-installation/config/access validation, previous-image export,
independent off-host custody and actual health/routing checks. Unit off-host
copies are separate local directories, not proof of off-host storage; scratch
images do not prove application startup or physical routing. Runner-owned
private runtime inputs do not establish another service UID's access.
The runner requires an already-present target bundle and does not attach to an
existing transfer reservation. Controller-to-target approval/input handoff,
trusted tooling/bootstrap, privilege integration and manual recovery remain
open. Public Ansible apply remains blocked.

### Completing the reviewed development deployment path (2026-11-13)

38. **Connect the actual controller and target.** Added the interactive
    `tool.deployment_operator` command and bounded duplex framing. One strict
    SSH connection owns explicit terminal approval, a single operation/reservation,
    protected target snapshots, optional reserved local-image receipt, independently
    verified controller-held backup, controlled Ansible apply and finalization.
    Pinned Hub candidates use a one-shot digest/platform/config-verified pull,
    never a floating-tag or build fallback. Ordinary reruns reuse the target
    committed pin and check health without interruption/recreation; changed
    candidates require explicit `--update`. A separate bootstrap play installs
    only a reviewed committed tooling checkout, isolated dependencies and control
    directories; it refuses existing/partial destinations and never restarts apps.
39. **Replace platform hooks with actual actions.** The concrete Linux/root
    adapter validates prepared self-contained web/external or web/managed
    Compose layouts, numeric identities, current generation hashes and container
    identities before same-layout adoption. It rejects hidden layout changes,
    foreign data/device consumers, unsafe permissions/metadata, missing credential
    inputs, invalid configuration, unsupported daemon modes and insufficient
    backup/restore space. It scopes stop/apply to that exact project, checks
    container/native writers and serial ownership, and independently checks
    runtime IDs, health/restarts, published and in-container HTTP, reported
    backend connectivity and configured agent health. It never rewires proxy
    trust, migrates mounts/networks or touches native playback.
40. **Complete independently held recovery assets and explicit restoration.**
    Actual previous runtime IDs are exported under operation-unique tags and
    verified as Docker archives. The controller checks all backup blobs,
    manifest identity and old archive root/config/platform identities before
    acknowledging custody; apply cannot precede this acknowledgement. Approved
    recovery uploads into a new destination, rejects outstanding Ansible work,
    restores an isolated matching image/data set and retains the complete
    post-backup tree before activation. Healthy restored runtime is required
    before pending resolution; previous committed pins remain unchanged.
    First-install recovery restores prepared data and container absence, with
    absence-specific checks rather than fabricated HTTP/hardware passes.
    A distinct approved `resume-original` path accepts only the matching original
    runtime before any apply attempt. All recovery attempts and finalization
    are one-shot and retain original records/evidence; no generic unlock,
    automatic rollback, expiry or pruning was introduced.
41. **Rehearse the real path and failure boundaries.** A real isolated sshd
    confines target execution to a disposable root Linux fixture using actual
    Docker/Compose and Ansible. Tests exercise wrong-key refusal, adoption,
    application image build/export/transfer/load/apply, unchanged IDs on rerun,
    an actual failing image, off-host archive verification, approved restoration,
    retained post-backup edits/secrets, old committed pins, lost backup
    acknowledgement, original-runtime resolution and first-install rollback.
    The peer dummy gateway remains an untouched external service throughout.
    Full regression: **815 tests, 809 passing, six skipped**, with local-build,
    SSH-transfer and deployment-workflow opt-ins. The bootstrap play passes
    controller-only syntax checking; production Python diagnostics are clear.
    No Pi connection, service change or observation was performed.

Items 1–3 of the current implementation priority list are complete for the
documented prepared development scope. This does not close S3–S5: production,
native/dummy artifact integration, other legacy layout migrations, broader
configuration/logging/dependency cleanup and physical/TLS/audio acceptance
remain. The integration uses a test-only forced SSH command into Linux, so it
does not certify actual Pi sudo/bootstrap, host-wide process visibility, hardware
or reboot/power-loss durability. Its application images derive from a cached
verified web image with fixture revisions; it is not release-source certification.
The next live test requires separately approved tooling installation/adoption
and deployment. A six-hour observation has not started.

### Approved live development acceptance (2026-11-14)

42. Published the implementation through PR #45 and installed the exact trusted
    tooling revision using real Pi SSH/sudo and the bootstrap play. Live
    preflight exposed Docker null device lists and nonseekable terminal approval
    streams; regression-tested fixes passed **818 tests, 812 passing, six
    skipped**, plus required CI including amd64/ARM64 container checks.
    Explicitly approved preparation corrected only two README modes and archived
    and removed one stopped legacy serial-device claimant. The reviewed public
    operator then deployed clean-source local ARM64 images from
    `f1f82ee31ce3575553bfd1dde423f17d6bd3c0df`, operation
    `539bd3a4-e8ae-4a85-8a3b-2733b963a4fc`, retaining independently verified
    off-host old-image/data recovery assets before actual Ansible apply.
    Required checks and durable finalization passed with no pending reservation.
    Five health samples over two minutes showed both services healthy, zero
    restarts, HTTP 200 and reported backend/playback health. Direct Docker
    web-to-gateway TCP passed; ordinary CLI rerun preserved container IDs/start
    times. Configuration/secrets matched backup content, ownership and modes.
    Native services were untouched. This supersedes the earlier lack of live
    bootstrap/deployment evidence, not the remaining S3-S5 work: live recovery,
    physical fault/audio acceptance, reboot durability and six-hour observation
    remain unperformed.

### Isolated Pi recovery and reboot acceptance (2026-11-14)

43. With separate approval, ran the full real deployment/recovery fixture on the
    ARM64 Pi using only isolated projects, a dummy peer and disposable data.
    Failed-update restore, preserved newer edits, lost backup acknowledgement,
    first-install absence and unchanged rerun passed in **505.584 seconds**.
    This did not roll back the live project or exercise disaster recovery
    across independent physical hosts. The actual live off-host backup remains
    retained. OpenSSH strict-mode/privilege prerequisites were accommodated
    without changing host SSH policy. A real Docker inspection-order defect was
    fixed by canonicalizing mount order, retaining exact mount fields; all 13
    operator tests and required CI passed at tooling revision `784e17d`.
44. Read-only web-to-gateway TCP and CA/hostname-verified authenticated TLS
    polling passed, including a separate controller-to-Pi request, HTTP 401 for
    an invalid token and handshake rejection for invalid SNI. Three audio
    outputs reported fresh readiness and correct routes. No audio controls or
    physical-controller fault injection were authorized or performed.
45. Per approval, rebooted the Pi and confirmed a new boot ID, automatic native
    service startup, healthy containers with zero restarts and no failed units.
    Exact container IDs/images/mounts/network IDs, configuration/secrets,
    persisted app state and committed deployment state were preserved.
    An orderly reboot is not a power-loss durability test.
46. Started durable read-only six-hour monitoring at **01:29:07 CET on
    2026-11-14**, expected to finish around **07:29 CET**, as user service
    `russound-acceptance-20261114.service`. Initial samples passed. It records
    health, routing, authenticated TLS, fresh audio readiness, native services,
    protected-file/identity drift and restarts, with no automatic remediation.
    Private records reside under
    `~/.local/state/mpdrussoundpi-acceptance-20261114/observation/` on the Pi.
    **Six-hour acceptance remains pending until a complete successful record
    is verified.** A lost/stale monitor is not a pass.

This advances the scoped S5 gate but does not close the overall plan. Audible
physical acceptance, deliberate controller-fault checks, live-project rollback,
power-loss testing and other layout migrations are not inferred from these
results. The user's approved scope deliberately used isolated recovery and
read-only audio checks; remaining S3/S4 work is unchanged.

## S3 runtime configuration and logging completion (2026-11-14)

Implemented locally at the operator's request to finish S3. This supersedes
earlier *source-level* pending S3 statements, not their historical observations
or the remaining S4/S5 deployment gates.

### Delivered contracts

- **D4 resolution and validation:** explicit CLI values override environment
  defaults without first parsing a lower-priority malformed value. Added
  `--no-debug`; Waitress still serves development and production. Invalid
  nonblank environment booleans/integers, invalid listener/backend ports,
  insufficient workers and excess explicit event streams fail before
  daemonization/server creation. Optional blank values remain unset.
  Integer inputs deliberately use bounded ASCII decimal syntax, not Python's
  permissive integer parser. Missing/invalid saved application configuration
  retains the existing recoverable setup UI, without automatic rewrites.
- **Effective endpoint API/UI foundation:** `/api/config`, successful saves and
  `/api/config/state` expose additive allowlisted `effective_backend` metadata
  from the connection resolver. Host/port remain independently overridable,
  including serial-to-TCP/default-host behavior. Source labels describe actual
  resolution, not merely the presence of a saved key. The browser consumes
  metadata for connectivity and displays a source summary; fields still edit
  saved settings. Offline saves preserve saved serial settings, and removing
  overrides restores their runtime selection. No secret/device path is
  included in the new metadata. Full inline read-only UI remains the separate
  [effective-config plan](./web_done_effective-config-overrides.md).
- **Credentials:** web and agent share a bounded file parser and validator,
  including exact 32/4096-byte boundaries, printable ASCII and the single
  optional LF/CRLF policy. Native CLI rejects invalid identity/token before
  opening configuration/creating a ledger. The agent play's read-only local
  preflight now calls that same parser before installing anything, also in
  check mode, without logging secret contents. TLS trust errors use safe
  messages. This is not staged activation or cross-host credential rotation.
- **D5 logging:** web and native Python services use DEBUG on explicit request,
  otherwise warnings/errors plus named essential health transitions.
  Shared formatters redact HTTP(S) URLs, Bearer values and named secret/session/
  protected-path assignments, including quoted records and exception text.
  Web rotation remains 10 MiB plus five backups; protocol audit remains a
  separate opt-in at 5 MiB plus three. Container `10m`/three-file retention is
  unchanged. Native stderr goes to the existing journal; no global retention
  changes or implicit files. Explicit interactive setup commands retain INFO
  operational output and opt-in DEBUG. Existing third-party daemon levels are
  host-managed. Formatter redaction is defense in depth, not a guarantee that
  arbitrary secret-bearing messages are safe.
- **D7 dependencies:** bounded web/agent/audio/deployment manifests, no
  unconditional upgrades; the audio setup play consumes its audio-only
  manifest. A disposable isolated audio environment passed installation,
  `pip check`, repository backend import and absence of Flask. The working
  audio environment was not modified. The
  [candidate preparation procedure](../documentation/mpd-headless-deployment.md#isolated-audio-dependencies)
  explicitly retains it until a reviewed switch. Bounds are not lock files
  or certification of every satisfiable release; explicit update/retest
  guidance is in the [container guide](../documentation/container-deployment.md#runtime-configuration-logs-and-dependencies).

### Verification and compatibility

- Full Python suite: **831 tests, OK, 11 skipped** on the local Python 3.13
  environment; skips remain explicit, not simulated hardware passes.
- Node suite: **112 tests, 102 passed, 10 skipped, zero failures**.
- Both changed Ansible plays passed syntax checks. The localhost-only
  inventory intentionally matches no `musicpi` target; no target tasks ran.
- Built and ran the real web image locally with Python 3.12.15, Flask 3.1.3
  and Waitress 3.0.2. Nonroot, read-only root filesystem, tmpfs data/log paths,
  **no external network or published ports**: startup, real config API,
  default-source metadata, shared-helper imports and graceful SIGTERM exit 0
  passed. The image was a disposable smoke artifact, not a deployment pin.
- Pylance reports no diagnostics in the new shared helpers, web playback client
  or supervisor; agent/server have only existing unused/static-platform hints.
  Existing validator unknown-type and unnecessary-check diagnostics in
  `config_types.py` were not broadened into unrelated cleanup; no diagnostic
  points into the new resolver metadata.

**Intentional compatibility changes:** malformed nonblank runtime values no
longer silently fall back; non-ASCII/oversized integer spellings are rejected.
Credentials shorter than 32 characters, non-ASCII/control characters, padded
values and extra newlines no longer receive different treatment by web,
agent and preflight. Existing nonconforming inputs require reviewed correction
before deployment, not automatic rewriting or rotation.

**Deployment boundary at local completion:** no S3 source was installed on the Pi, no working
credential/environment was changed and no service was restarted. S4 still
owns remaining layout/shared-Ansible work, typed variable fixes and staged
agent activation; S5 owns approved migration/live activation and remaining
layout/physical acceptance. The prior successful freshness-fix six-hour
observation does not certify these undeployed S3 changes.

### S4 local implementation in progress (2026-11-14)

Implemented locally, without applying changes to the Pi:

- Shared strict Ansible boolean, port, literal-IP/CIDR, hostname and playback
  registry filters; component defaults now use role defaults so inventory has
  higher precedence. Native web account/group resolution and bind-derived
  readiness URL are shared. Audio no longer selects native ser2net by default.
- MPD output operations now use exact configured IDs and managed unit markers,
  reject legacy/overridden unit paths and unreviewed drop-ins before mutation,
  preserve unrelated/stale matching units and never use broad `pkill -f mpd`.
  Marker checks read only the bounded first-line signature; existing unit,
  configuration and drop-in directories must be user-owned and not
  group/world-writable. Conflicting MPD/egress CLI action options fail before
  subprocess work.
- Local image bundles can include a verified `web` + `dummy` pair. Dummy
  registry selection remains unavailable; Compose validation requires explicit
  backend-connectivity acceptance, the healthy dummy dependency, isolated
  writable dummy state, web-to-dummy route and no hardware permissions.
- `setup-musicpi.yml --tags stage` now creates a revision-specific candidate
  checkout and bounded audio virtual environment, validating existing
  operational config while leaving the active checkout, config, units,
  gateway and services untouched. Default mode is stage. Its legacy in-place
  activation path is explicitly blocked until native reservation, independently
  verified off-host backup, interruption handling, recovery and health checks
  are implemented. The playback-agent play now stages a revision-pinned source
  and isolated dependency environment without touching credentials or active
  services; its activation remains blocked on the same transaction workflow.
- Added `stage-russound-web.yml` as a separate, candidate-only native web path.
  It clones a full selected commit and creates an isolated dependency
  environment, compiles candidate modules without writing into the checkout,
  and validates existing operational config without contacting the gateway.
  It does not update the legacy live web play, install system packages, write
  units or start/restart services.
- Development image-selection files are now read through descriptor-based
  no-follow checks requiring a private, owner-owned, single-link `0600`
  regular file; output validates the candidate and is exclusively created via
  a checked directory descriptor. This closes symlink/hard-link/permission
  races in selection reuse and creation.

Typed-variable follow-up: the shared input role now converts validated
inventory/default port values to integers after strict validation. Proxy
additional-CIDR container type is checked before list concatenation, and
hostname/address validation is centralized in that role; the proxy play keeps
only its mode-specific unprivileged-port constraint. Extra vars retain
Ansible's higher precedence, so templates still explicitly convert supported
string boolean gates rather than relying on truthiness. Real Ansible tests
verify boolean/port types across all seven component defaults, reject invalid
proxy list shapes before mutation, and render string boolean extra vars through
the actual agent unit template.

Validation: full Python suite **851 tests, OK, 11 skipped**; Node suite **112
tests, 102 passed, 10 skipped**. All seven changed or new Ansible plays pass
syntax checks; the native web candidate play's `--tags stage` list contains
only candidate tasks. The audio `--tags stage` list is candidate-only, and a
local negative run of the legacy `init,new` tags fails in the pre-task guard
before mutation. Tests verify dummy bundle identity, Compose route/state/
hardware boundaries, typed inventory values and MPD ownership/CLI failure
behavior. The agent and native web staging plays are syntax-checked and
statically tested for their candidate-only behavior. Candidate staging itself
was not run on the Pi. The safe-apply transaction and callback validation are
implemented locally. The isolated SSH/Docker end-to-end failure/recovery
rehearsal passed on the local Linux/ARM64 daemon: **34 tests, 33 passed, one
skipped**, including update failure retention, independently held backup,
approved recovery, lost backup acknowledgement, and first-install absence
restoration. A cached web image whose Docker-save archive did not match its
inspected identity was rejected before fixture resources were created; the
rehearsal passed with a cached image whose archive identity verified. No Pi
deployment or native activation occurred. Broader S4 exit criteria below are
not yet claimed.

Follow-up typed-input/shared-role validation after the full-suite run:
**32 focused tests passed** across the input-role, render-integration,
deployment-workflow and agent/proxy suites. All seven plays using the shared
input role passed Ansible syntax checks. No target connection or mutation was
performed.

**Candidate-directory permission correction:** the approved Pi staging run
showed that the audio revision directory inherited mode `0775` from the target
umask, unlike the web and agent candidates. Audio staging now validates the
existing candidate root's owner/mode, refuses symlinks, and atomically reserves
each new revision directory as `0700` before cloning. A focused regression
checks the ordering and exact mode. Existing staged revisions are deliberately
not rewritten or reused; this correction applies only to fresh candidate paths.

The shared proxy-input role now requires additional trusted CIDRs to be a
sequence before combining them with the primary CIDR. Real Ansible regressions
reject strings, numbers, booleans, null and mappings at the shared preflight,
before address parsing or any mutation.

**Verified corrected audio staging on Pi:** at published revision
`dcbf35ed30f710e8c451b13a6256fad4e99730dd`, the fresh audio candidate staged
successfully under
`~/.local/share/mpdrussoundpi/audio-candidates/dcbf35ed30f710e8c451b13a6256fad4e99730dd/`.
The candidate parent and revision directory are both owned by `musicpi` with
mode `0700`; the source checkout is clean at the exact revision and its
isolated environment passes `pip check`. The existing audio configuration
validated for three outputs. The play reported activation not performed and
services untouched; a subsequent read-only check found the web/ser2net
containers and playback/audio user services active. This validates fresh
candidate directory creation only, not native activation or physical audio
acceptance.

### S4 controller-side acceptance (2026-11-14)

The remaining controller-side S4 acceptance checks passed on the published
`6e78940d52ef5a682c350c6b0739d24d8ddc35d1` head:

- The documented opt-in local build/load, SSH transfer and deployment runner
  set passed: **64 tests, OK**.
- All **11** repository Ansible playbooks passed syntax checks.
- The JavaScript suite passed: **112 tests, 102 passed, 10 skipped**.
- PR #45 CI passed Python/JavaScript tests and amd64/arm64 container checks.
- `git diff --check` passed. The only other worktree change is the unrelated
  guided-first-time-setup plan, which is not part of this acceptance.

Together with the previously recorded full Python suite (**853 passed, 11
skipped**), focused Compose/input/staging tests, and isolated deployment and
recovery rehearsal (**34 tests, 33 passed, one skipped**), this closes S4's
local/CI implementation gate. It does not claim S5 live migration, native
activation, physical audio/routing acceptance, or a six-hour observation.

### Isolated S3 Pi deployment and background observation (2026-11-14)

Following explicit operator approval, committed S3 separately as
`b973cbd74e6054fd77b5f8ace531901b15001246`; the unrelated guided-setup plan
remained outside that commit. Built the ARM64 web and explicit dummy images
on the controller from a clean detached worktree at that revision. Verified
the web bundle manifest/archive, dummy archive transfer hashes, loaded image
IDs/platforms and revision labels on the Pi before starting anything.

The test is a **manual isolated development rehearsal**, not certification of
the unfinished S4 dummy/operator deployment path:

- Project `russound-s3-b973cbd`, web bound only to `127.0.0.1:18080`, separate
  private config/state/log/credential copies and an internal dummy network.
  No serial device is assigned; the dummy does not control physical hardware.
- Web alone also joins the initially empty, already-allowlisted
  `mpdrussoundpi_default` bridge for existing authenticated agent HTTPS access.
  This is an external egress bridge, not a fully network-isolated test.
  No live proxy allowlist, production network/service configuration or
  credential was changed. The live gateway is not used by the test web.
- Production-minimum logging is selected deliberately for the test web;
  explicit DEBUG was also exercised in isolated logging checks.
- Actual Pi web-to-dummy TCP/controller responses and CA/hostname-verified
  authenticated TLS polling of the existing read-only agent passed. All
  three live audio outputs reported fresh readiness and correct routes.
- The new API metadata agreed across endpoints, reported environment-forced
  TCP over a saved synthetic serial endpoint, and retained saved configuration.
  A subprocess partial-port override correctly reported the default host.
  Invalid runtime input failed startup without echoing the supplied value.
- Native source was extracted separately under the test directory. Using the
  existing agent interpreter without installing packages, token-boundary,
  redaction, production/DEBUG and supervisor help/import checks passed.
  A temporary loopback-only read-only agent on port 18081 passed authenticated
  liveness, HTTP 401 without auth and graceful SIGTERM exit 0, then was stopped.
  Its intentionally missing config prevented MPD/audio access; this is **not**
  live native-agent/supervisor activation or operational hardware acceptance.
  Invalid-token CLI execution created no operation ledger.
- An unchanged Compose rerun preserved exact candidate container IDs, start
  times, images, mounts and networks. Both test containers are healthy with
  zero restarts. Original live container identities, native service PIDs/
  restart counts, protected config/credentials and installed native source
  hashes matched the pre-test baseline after all checks.

Only after those checks passed, started durable user service
`russound-s3-b973cbd-observation.service`. Timed observation began
**18:21:28 CET**, duration **1800 seconds**, expected completion about
**18:51:28 CET**. It samples candidate API/metadata/audio freshness every
10 seconds and performs full identity, protected-file, direct routing and
original-live-deployment checks about once a minute. It stops on the first
failed sample, records named failures and performs no automatic remediation.
Three synthetic observer tests verify named failure reporting, identity-drift
detection and persisted unsuccessful early completion.

**Superseded by operator direction at 18:25 CET:** stopped this observer and
its dummy/web Compose project to test the real deployment instead. The last
record had **22 samples, zero failures**; this interrupted run is **not**
a completed 30-minute pass. Private baselines, check reports, append-only
observation records and atomic status reside on the Pi under
`~/.local/state/mpdrussoundpi-s3-test-b973cbd-20261114/`.
Test containers/network were removed with the isolated project's Compose down;
private evidence/data/credential copies were retained. No six-hour run was
started. The real S3 update below supersedes the earlier no-live-change boundary.

### Approved real S3 deployment and observation (2026-11-14)

The operator confirmed the Pi is not in production use and explicitly requested
stopping the dummy test, updating the real deployment and testing that instead.
This authorizes this development rehearsal, not general unattended future
updates or implicit credential/environment migration.

1. **Real managed container pair:** built clean ARM64 web/ser2net from S3
   revision `b973cbd74e6054fd77b5f8ace531901b15001246` on the controller.
   The reviewed public operator transaction
   `60fc23f4-f82e-448b-a825-1eb16cce359e` received the operator's exact approval
   phrase, preserved the existing project/mounts/network/config/credentials,
   stopped its writers, retained and independently verified an off-Pi recovery
   backup including old images, then applied successfully. All required
   `apply`, `containers`, `http`, `backend` and `playback` checks passed.
   Final web image is
   `sha256:981a345cdfe33ecebc6026f3275ff6521b2793368c1191b7f3fdea632cf9251b`;
   ser2net is
   `sha256:fc4a43a45b416870d7b0f6fe828de4862116b4f811ab90707a3fe2b829145dd4`.
   The existing development logging profile was preserved, not silently changed
   to production.
2. **Scoped native S3 activation:** stopped only the agent and playback
   supervisor, leaving PipeWire, WirePlumber and the three MPD processes
   running. Captured original source, units, admin/runtime configuration,
   credentials and consistent operation-ledger files; SQLite integrity passed.
   Copied the 266240-byte/32-member native archive off the Pi and independently
   verified its SHA256 and every member's content/size/ownership/mode before
   acknowledging it and applying source changes. The native backup's SHA256 is
   `c432a23fd788cd741c2d4db47e7658c78abeef0d6d644a00a7d377c2b80964f3`.
   Retained this backup privately alongside the container recovery material.
3. Installed exactly eight source/manifest files from the tested S3 staging:
   agent entry point, observer helper, shared credential/logging helpers and
   bounded agent manifest; supervisor entry point, shared logging helper and
   audio manifest in its existing checkout. Verified every installed source
   checksum, retained existing units/configuration/credentials and started
   supervisor then agent. This intentionally modifies the existing native
   checkout without resetting its unrelated untracked files. No Git pull,
   dependency installation, venv switch, audio regeneration, credential
   replacement, proxy change or global journal-retention change occurred.
   Both native environments already contained compatible python-mpd2 3.1.1;
   imports were checked before activation. Agent PID became 764242 and
   supervisor PID 764240; automatic restart counters remained zero.
4. **Real acceptance before timing:** actual controller responses, direct
   web-to-ser2net TCP, CA/hostname-verified authenticated agent TLS, consistent
   effective endpoint API metadata and fresh readiness/correct routes on all
   three outputs passed. The first cold native-agent sample included pending
   routes; this was observed during startup, not counted as a timed pass.
   Timing began only after complete warmup. An unchanged real Compose rerun
   preserved exact container IDs/start times/images/mounts/networks. Protected
   web config/credentials and native units/admin config/token matched their
   pre-update hashes. MPD/PipeWire/WirePlumber PIDs were unchanged.

**Real background observation:** user service
`russound-s3-live-b973cbd-observation.service`, timed start **18:32:10 CET**,
duration **1800 seconds**, expected completion about **19:02:10 CET**.
Every 10 seconds it checks real web/controller/playback health, exact output
set, audio readiness/route status, stale flags/ages and effective endpoint
metadata. About once a minute it additionally checks container identity/health/
restarts, protected files, installed native sources, native service PIDs/
restarts, direct gateway TCP and authenticated TLS. It stops on the first
failure, records the named failure and performs no automatic remediation.
The installed helper still refreshes audio every 20 seconds; the independent
stale threshold remains 30 seconds.

**Verified completed at 19:05 CET:** the observation finished at
**19:02:10 CET**, after **1800.247 seconds**, with **174 samples and zero
failures**, `complete/passed:true`, no early stop and systemd result success/
exit 0. Maximum readiness age was **20115 ms** and route age **20026 ms**,
both below the unchanged 30000 ms stale threshold. A fresh full baseline
comparison at 19:05 CET passed real routing, protected files, installed native
sources and service identities; both containers were healthy with zero
restarts. Completed JSONL/status and post-completion evidence were copied to
the controller. This passes the scoped 30-minute real S3 check, **not**
six-hour acceptance. No new long observation was started.
Durable private records reside under
`~/.local/state/mpdrussoundpi-s3-live-b973cbd-20261114/` on the Pi; native and
container backups/check evidence also have independently verified controller
copies. The real UI remains at `http://192.168.1.105:8000/`. Only the real
web/ser2net containers remain running; no dummy deployment or dummy observer
is active. Remaining S4/S5 layout/refactoring/migration work is not closed by
this scoped manual native activation and real managed-layout rehearsal.

### Guarded tooling upgrade retry (2026-11-14)

The first tooling-only upgrade attempt to `5d60b23d4f645fa93393e22f246cd49027b066ec`
atomically switched the checkout and installed dependencies, then failed its
operator `--help` check because Ansible invoked the module outside the checkout
working directory. The playbook rollback restored the original
`784e17d0896be54bba3f6c5f7b128ae1bfcf0ec6` checkout, released the reservation,
and left the failed candidate at
`/opt/mpdrussoundpi-deploy.upgrade-5d60b23d4f645fa93393e22f246cd49027b066ec`
for inspection. The incident did not change application services.

PR #48 corrected the operator-check working directory in both upgrade and
restore playbooks and added a parsed-YAML test. Focused tests, both playbook
syntax checks, and all CI jobs passed. The merged tooling commit
`ff2f4281392efacb1cf015534bda588d0dd31e57` was then applied through the guarded
upgrade play. The active checkout reports that exact revision, its isolated
environment passes `pip check`, the operator help check passes, and deployment
state is unblocked with no active reservation. The original checkout remains
retained at the revision-specific sibling path. Web and ser2net container IDs
remain `a5c40f5cf5f3` and `745f860c81fa`, both healthy; the playback supervisor,
three MPD services, playback agent, PipeWire, and WirePlumber remain running.
No application image, service, configuration, or credential was changed.
The six-hour observation remains deferred and has not started.

### Isolated backup and recovery rehearsal (2026-11-14)

Reviewed the operator deployment/recovery runbook in
[`container-deployment.md`](../documentation/container-deployment.md#failure-explicit-recovery-and-retained-evidence)
against the backup and recovery implementation. It documents stopped-writer
backup capture, independent off-host verification, separate approval,
matching code/data restoration, preservation of post-backup trees, one-shot
failure behavior, and the prohibition on forced unlock or automatic cleanup.

The focused file-backup suite passed **27 tests with one Linux extended-
attribute test skipped on this host**. The opt-in container recovery rehearsal
initially found a stale test fixture that tried to resolve a registry dummy
image, which is intentionally unsupported. The fixture now uses a synthetic
valid external web-image candidate solely to inject an unavailable image for
the failed-apply path; it does not claim registry verification or adoption.
After that correction, the isolated ARM64 Docker rehearsal passed. It used a
unique temporary Compose project and labeled named volume, verified web-to-
dummy routing, retained pending state after failed apply, restored the
synthetic config/data/secret tree into a new isolated path, and verified
routing and repeat-run stability after restoration. The test cleaned up its
project and volume.

This validates the bounded local recovery rehearsal only. It is not a Pi
migration, off-host disaster-recovery exercise, schema-compatibility check,
physical-controller/audio acceptance, or six-hour observation. S5 live
acceptance remains open and requires its own approved scope; the six-hour
observation remains deferred.

### Live Pi routing and playback software check (2026-11-14)

With the operator's approval for S5 verification, performed read-only checks
against the existing Pi deployment. The deployment state remained unblocked
with no active reservation. The web and ser2net containers remained healthy
with their existing IDs. `GET /api/rooms` reported four zones and
`backend_status.connected: true`; `GET /api/playback/health` reported the
configured playback integration `ok` with its agent `ok`. Its fresh snapshot
was not stale (846 ms at observation) and reported all three outputs playing,
audio-ready and correctly routed. Native `mpdctrl.py --status` and
`--check-route` agreed for `stereo_front`, `stereo_centerlfe` and `stereo_rear`.
The non-enforcing audio readiness check found the configured hardware/profile
and sinks ready. A TCP connection from the actual web container to
`russound-ser2net:6666` succeeded.

No application service, image, configuration, credential, saved gain or
playback intention was changed. Because all outputs were already playing, no
start/stop/gain command was issued that could replace or interrupt the
operator's current content. These checks verify software/controller routing
and playback telemetry, not that sound was audibly confirmed in the room.
Six-hour observation remains deferred and unstarted.

### Six-hour endurance and audible acceptance (2026-11-15)

The subsequent read-only S5 observer completed successfully from **01:53:41
to 07:53:43 CET**, with **350 samples and zero failed samples** over
21602 seconds. It checked container identity/health/restarts, native service
stability, protected configuration, gateway TCP and authenticated agent TLS,
and playback readiness/freshness/routes. Its persisted completion reported
`passed: true`; both containers remained healthy with zero restarts at the
10:14 CET follow-up, and native playback/audio services were active.
Private evidence remains on the Pi under
`~/.local/state/mpdrussoundpi-s5-6h-20261115T0154/`.
No automatic remediation or playback changes were performed by the observer.

The operator then performed guided listening checks in **Living Room**
(controller 1, zone 1), operating playback, source selection and comfortable
listening volume themselves. They confirmed the physical connections and
reported the correct station, clear and uninterrupted audio on all three:

| Pi output | Russound input | Operator result |
|---|---|---|
| `stereo_front` | 1 (Radio) | Passed; channels reported normal |
| `stereo_centerlfe` | 2 (TV) | Passed; all good |
| `stereo_rear` | 3 (Bluetooth) | Passed; all good |

These are operator-reported audible results, not an automated acoustic
measurement. Other rooms were not audibly tested. No saved input mapping was
edited; these connections were confirmed for the test only. The assistant
issued no playback, power, source or volume mutations.

This closes the deferred six-hour and representative audible acceptance
items, not the remaining S5 migration/recovery, support-boundary,
documentation and legacy-retirement gates.

### Prepared split-host development scope (2026-11-15)

The operator explicitly chose **split-host deployment only**, deferring
same-host ↔ split-host migration. They further chose a **prepared native
backend**: manage the standalone ser2net container while preserving the existing
native agent, TLS proxy, supervisor, MPD and audio services. Fresh native
provisioning/activation/update/recovery is not part of this iteration.

`compose.backend.yml` reuses the existing gateway definition without bringing
up web. Its published TCP listener requires an explicit backend address and
port. The operator validates a specific non-loopback LAN listener, exact
serial device/group and gateway-only service scope. Registry selection and
local bundles contain only ser2net; Ansible preview/preflight supports the same
mode without a hidden target build or web dependency.

Both hosts reuse the existing independent reviewed deployment transactions:
explicit approval, off-host verified backup, pinned images, unchanged reruns,
explicit updates and failed-operation recovery. Backend acceptance checks the
gateway container and published TCP route; controller and playback acceptance
must be required by the external-gateway web deployment. No cross-host atomic
commit or automatic peer rollback is implied. Credentials, TLS trust and
Docker-aware firewall/proxy source allowlists must already be prepared.
See the authoritative
[split-host procedure](../documentation/container-deployment.md#split-host-deployment-on-prepared-hosts).

This replaces the need for a legacy in-place Docker play for a prepared
split-host deployment, not the native installation/activation paths. Legacy
retirement still requires an explicit inventory and retained-entry-point tests.
The current combined live Pi project is not migrated by this implementation.
Physical separate-host deployment acceptance remains distinct from synthetic
controller/TLS routing tests and the prior same-host Pi endurance/audio results.

Local validation:

- Deployment regression discovery: **230 tests run, 220 passed and 10
  opt-in/platform-dependent skips**. Coverage includes gateway-only registry
  and local bundles, strict device/listener/service scope, controller-only
  Ansible previews and layout-specific recovery receipts. The recovery-state
  boundary now rejects web-only or incomplete receipts for a standalone
  gateway, and gateway-only receipts for a web deployment.
- Five retained playbooks passed Ansible syntax checks: development preview,
  local-build preview, tooling bootstrap, tooling upgrade and tooling restore.
  Tooling plays now select arbitrary inventory names, with a tested
  single-host guard before connection instead of a hard-coded `musicpi` target.
- The explicitly enabled isolated Docker routing test passed on local ARM64.
  Web and synthetic backend/agent peers used separate Docker networks and
  published host ports, not shared Docker DNS. It verified controller replies,
  authenticated CA-verified HTTPS, rejection of untrusted CA/mismatched
  certificate hostname/missing authentication, unchanged rerun identity,
  recovery of routing after web recreation, unchanged backend identities and
  preserved configuration/token bytes. The cached web image ran current
  mounted source; the controller and audio status were synthetic fixtures,
  not serial hardware, real native MPD/audio, or an image-release certification.
- That routing test is wired into the amd64/arm64 container CI matrix; those
  new GitHub jobs have not yet run for these unpushed changes.
- No live Pi service, configuration, credential, firewall, proxy or layout was
  changed during those local tests. The subsequent real-host trial below
  supplies separate runtime evidence; legacy retirement and the existing native
  activation/cross-layout migration gates are not closed by either test.

### Real Mac-web / Pi-backend acceptance (2026-11-15)

The operator approved using the existing Mac Docker installation as the
separate web host and explicitly allowed Pi service interruptions. No additional
system-wide tools or packages were installed on the Mac or Pi.

The successful bounded trial ran **13:55:53–13:57:37 CET**:

- Stopped the original Pi web and gateway containers, preserving their IDs,
  images, configuration and project. Native playback services remained active.
  A temporary gateway-only project used the actual `compose.backend.yml`,
  existing accepted ARM64 gateway image, USB serial adapter and device group.
- Published the standalone gateway on the Pi LAN address, TCP 16666, with a
  temporary Docker-aware rule admitting only the Mac's measured source address
  in the original connection direction. A Pi-container source was rejected.
  No SSH tunnel or cross-host Docker DNS was used.
- Started web on the Mac with `compose.dev.yml`, an isolated copy of the
  stopped-writer configuration/state and protected credential/CA files, and
  environment overrides selecting the real Pi gateway and HTTPS agent.
  The test UI was bound only to `127.0.0.1:18080`. It used the cached accepted
  ARM64 web image; selected runtime/UI file hashes matched current source.
- **Ten successful readiness samples** verified real controller communication
  and authenticated playback health. All three native outputs had fresh,
  successful audio readiness and correct routes through the Mac web API.
  This was telemetry verification, not another operator listening test.
- Unchanged web and gateway reruns preserved their respective container IDs.
  Explicitly recreating each test container changed its ID and recovered the
  real controller/playback routes. Native service PIDs/restart counts remained
  unchanged. No playback, source, volume or power mutation was requested.
- CA-verified authenticated HTTPS succeeded. Missing authentication was rejected
  with HTTP 401; missing CA trust failed certificate validation. A wrong TLS
  server name was rejected at the handshake by the existing Caddy endpoint.

Earlier attempts did not pass: the first temporary firewall rule also filtered
reply-direction traffic and prevented gateway communication; after correction,
the negative TLS probe initially expected a certificate-validation exception
where Caddy instead rejected the wrong SNI during the handshake. Both attempts
restored the original healthy installation before retrying. Neither failure was
hidden or counted as a pass; no application workaround was required.

After the successful test, removed the Mac test containers/network and Pi
standalone project, removed the temporary firewall rule, cancelled the
Pi-side timed restoration guard and restarted the **original container IDs**.
Both original containers were healthy; controller and playback checks passed.
Pi protected web configuration/secret contents and metadata, plus native service
identities, matched baseline. Temporary copied configuration/state/credentials
and transfer archives were removed from the Mac; private non-secret result and
restoration receipts remain in the session evidence.

This closes the representative **real separate-host runtime/routing acceptance**
for the prepared hybrid scope. It does **not** certify the Linux-root
deployment operator on macOS, a fresh image build/release, automated cross-layout
migration, or the full reviewed two-Linux-host apply/failure-recovery lifecycle.
The trial used explicit manual Compose operations and restored the original
same-host Pi arrangement. The previously completed six-hour/audio acceptance
remains separate evidence, not a new split-host endurance claim.

### Prepared native web/gateway scope and implementation (2026-11-15)

The user selected a bounded prepared-Pi native increment: native web and native
ser2net, preserving the already working native agent, supervisor, MPD, audio,
proxy and credentials. Fresh-machine/full-native-stack provisioning is not
included. The scope selection itself did **not** authorize a live cutover.
Split-host implementation/acceptance was committed separately as `0733d78`.

The [native workflow](../documentation/container-deployment.md#prepared-native-web-and-gateway)
now has source/venv candidate sealing, root-owned service-readable releases,
explicit same-layout update and recorded Docker-to-native cutover scope,
shared host reservation/state, stopped-writer backup, independent off-host
verification before activation, exact unit/controller/playback checks,
unchanged-candidate verification, pre-apply original-runtime restart and
explicit recovery of failed or latest committed native operations. Recovery
preserves post-backup trees and restores the prior committed selection only
after checking the prior runtime. It does not change OS packages, the
distribution ser2net unit/config or the native playback stack.

The old in-place native installer is a non-mutating refusal, not an unsafe
alternative activation path. Legacy-native adoption, external-native gateways,
schema/settings/network migrations, OS upgrades and fresh-machine disaster
recovery remain excluded. Native candidates require an unchanged prepared OS;
the backup preserves application/environment bytes, not a bootable system.

Local tests exercise approval refusal, shared reservations, candidate drift,
canonical unit round-trips, off-host failure, explicit recovery, post-backup
edit preservation, unchanged reruns, previous native environment restoration,
source/environment byte/mode checks and host HTTP acceptance. Linux-only
staging was also exercised on the Pi in disposable directories: a separate
web venv imported successfully as the nonroot service account; all **1454**
source/environment backup entries restored with the exact original seal;
the final staging check also used private umask **077**. Generated units passed
`systemd-analyze verify`; an isolated nonroot process using the actual Pi
ser2net binary parsed the generated connection/options and passed bidirectional
TCP-to-PTY byte checks without opening the real USB serial adapter.
Original live Docker IDs,
restart/health state and native playback service PIDs/restart counts remained
unchanged. This used the existing trusted application's available revision,
not an unpushed new application release. **No native unit was activated.**
Disposable Pi staging/source/venv/backup/restore trees were removed after
retaining the verification receipts off Pi.

The full local Python regression passed **901 tests, 13 skipped, no failures**.
Focused cutover coverage independently verifies archived Docker image identities
under the native operation ID and restoration of the prior Docker deployment
record. Failure injection covers partial native unit installation before
systemd loads it, as well as lost off-host verification and post-start failure.
Service/Docker control in these local lifecycle tests is simulated; these are
not substitutes for a real Pi cutover/rollback rehearsal.

### Prepared native live cutover and Docker recovery acceptance (2026-11-15)

The user subsequently authorized committing and testing on the Docker Pi.
Native implementation was committed as `3e922d8`; exact committed source was
transferred by verified Git bundle and installed through the guarded tooling
exchange, retaining prior checkouts. The actual Ansible native staging play
created sealed release `42d2e3a0-5c9c-4805-b04f-d35601924945` from that commit.
No global packages or playback/audio/proxy services were changed.

The first reviewed activation safely refused before reservation or interruption:
ordinary log growth changed the backup-size estimate during approval. Two real
preflights reproduced growth from 273256848 to 273257506 bytes with otherwise
identical review scope. Commit `1c574bd` fixes this by retaining a reviewed
capacity with the existing 64 MiB margin, rejecting excess growth and rechecking
free space. Focused native tests passed **28 tests**; the full regression passed
**902 tests, 13 skipped, no failures**. The fix was separately installed with the
same guarded exchange; no in-place tooling edits or safety-check bypasses were
used.

Reviewed native operation `76cf7990-eb40-4982-9e01-84b144e6ecbd` completed after
the controller independently verified and retained the stopped-writer backup.
Its manifest SHA256 is
`0a5fab0b44b48b8407adc9d21e008244f7353366ab07ccc32e8284559f5c086a`.
Both Docker writers stopped; the native units were enabled/active with zero
restarts and UID/GID 1000. The dedicated gateway listened on `127.0.0.1:6666`,
web remained on port 8000, and the real controller and authenticated TLS agent
route passed. All three stereo outputs had fresh, successful readiness/route
probes reporting ready/correct. This is not a new human audible confirmation.

Same-candidate invocation `3d033f91-94da-41d2-b790-a0872e3c6774` returned
`unchanged`: identical native unit PIDs/restart counts, no new backup and the
original committed native operation retained. Deliberate approved recovery
`fc023589-209d-4d4f-a969-0d1c9285c847` then restored the original Docker runtime
from the independently held off-Pi backup. Both original container IDs/image
IDs were healthy with zero restarts; the exact prior committed deployment
record was restored, with no pending operation/reservation. Native managed
units were removed/inactive and the native loopback listener was gone.
Post-backup data was retained under the recovery's `.failed-UUID` tree.

All seven existing audio/agent/supervisor service identities and twelve
protected file hashes/owners/modes matched baseline before cutover, while
native ran and after Docker restoration. Final controller/playback and all
three output probes passed again. The operator explicitly selected **restore
and retain Docker** as the final layout. Private backup, identity, approval and
verification receipts remain off Pi; recovery assets and staged native release
are retained. Named temporary source-bundle transfer files were removed.

This closes the representative prepared-Pi native cutover/rerun/Docker-return
trial. It does not certify fresh-machine provisioning, live native-to-native
update/recovery, reboot, a native six-hour observation or new audible output.
The earlier six-hour/audio acceptance remains Docker evidence only. Broader
S5 gates and final legacy retirement are not marked complete by this trial.

## Acceptance and verification

- Render Compose configurations for dev Hub, dev local build and production;
  assert expected images, logging, mounts, secrets, networking and no
  unintended builds. Missing production release must fail, not use dev.
- Test each Ansible entry point and repeated runs, shared task behavior,
  missing prerequisites and failure rollback; preserve unrelated services and
  dirty checkouts. Exercise native, Docker, external gateway and dummy modes
  according to the final support decision.
- For changed settings, use focused representative tests for applicable
  CLI/environment/config/default conflicts, blank and invalid values, source
  reporting, save/reload and existing configuration migration. Verify affected
  Ansible-generated values actually reach the intended container or service;
  investigate newly discovered material gaps without requiring every
  permutation of every setting.
- Check development diagnostics and production minimum logging, rotation,
  redaction, server choice, exposed ports and resource limits. Ensure the dev
  debugger is not accidentally network-exposed.
- On the Pi and a representative non-Pi dev setup, verify web-to-gateway and
  web-to-agent routing, TLS, physical controller responses when available,
  MPD/audio readiness, preservation of state and a deliberate rollback.
  Report absent hardware or unavailable stable images as unverified, not passed.

Related: [deployment status](./general_idea_deployment-status-overview.md),
[update status and restarts](./general_idea_update-status-and-restarts.md),
[image release pipeline](./general_open_cicd-docker-release.md), and
[agent containerization](./mpd_new_agent-containerization.md).

## Final step: retire legacy deployment paths and artifacts

**Superseded web/gateway repository paths retired on 2026-11-15**, at the
operator's request. The checklist below is retained as the cleanup contract;
the implementation and retained-surface inventory follow it. Remaining native
acceptance was explicitly deferred, not passed or replaced by this cleanup.

After the preceding implementation and acceptance work is complete, remove
superseded deployment entry points so operators have one clean, documented
path for each supported deployment intent. This is a planned final cleanup,
not authorization to remove or alter anything on a live host before migration
and recovery have been separately approved.

- Inventory legacy Compose files, Ansible plays/roles/templates, scripts,
  variables, defaults, CI jobs, examples and documentation references. For
  each, identify its supported replacement or explicitly mark it unsupported;
  do not leave two commands that appear to deploy the same intent differently.
- Check repository references, packaging, automation and operator
  documentation. Update active guidance to the reviewed entry points, retain
  historical design/decision records where useful, and remove obsolete
  instructions rather than silently redirecting legacy commands.
- Remove obsolete scripts and generated/template artifacts only after
  replacement behavior and supported migration/recovery paths have passed the
  acceptance checks above. Preserve shared helpers and test fixtures still used
  by supported workflows; verify no active playbook or CI job depends on the
  removed files.
- Keep target data, credentials, local-only images, deployment state, backups
  and recovery evidence out of repository cleanup. Their retention or removal
  requires a separate reviewed migration/recovery decision; repository cleanup
  must never imply host cleanup.
- Run the focused tests and syntax checks for every retained deployment
  entry point, then the relevant full suites. Search for stale references and
  confirm the published docs expose only supported paths, while documenting
  unsupported layouts and the exact boundary of any remaining compatibility
  support.

### Repository retirement and support-boundary reconciliation (2026-11-15)

Removed the old build-oriented hardware/dummy Compose files, mutable Docker
installer, already-refusing native web installer, source-only web staging play,
their three component-default files, three web/Docker templates and unused
deployment-account role. Their replacements and non-equivalences are listed in
the authoritative [retirement table](../documentation/container-deployment.md#existing-installations-and-legacy-entry-points).
In particular, prepared-host deployment does not replace the old installer's
OS/Docker provisioning side effects, and sealed native staging cannot activate
old private source-only candidates.

Retained all five current Compose profiles/overlays, image Dockerfiles and
entrypoints, reviewed operator/recovery/state/backup modules, and all eleven
Ansible plays: two development previews, three tooling lifecycle plays, sealed
native staging, audio staging, agent staging/removal, proxy and egress.
The shared playback-registry task/filter is still called by development
preview and is retained; only its unused legacy component dispatch was removed.
Separate audio/agent activation guards and component tooling are not changed
by web/gateway retirement. Automatic full native-stack provisioning and
same-host/split-host migration remain excluded, not silently redirected.

Retired-template/source-only-staging tests were removed with their targets.
Replacement tests exercise playback registry escaping through the actual
Ansible preview and retained Compose profile, native unit quoting/host-secret
mapping, absence of retired paths, no retired entry-point references in active
automation, and syntax of every retained play without target contact. Existing
profile, transaction, split-host and recovery tests remain applicable.

README, architecture/protocol guidance, runtime-directory notices and the
authoritative guide now distinguish accepted prepared-host trials, unverified
two-Linux-host operator acceptance, deferred native update/recovery/reboot/
endurance/audible checks and blocked stable production. Historical inventory
remains evidence, not an alternative runbook; links to removed artifacts in
the deployment/release plans were converted to historical names. The unrelated
in-progress first-time-setup draft was deliberately preserved unchanged.

No Pi/host command, service, package, configuration, credential, saved override,
image, deployment record, staged release or backup was changed by this work.
Legacy runtime-directory notices and ignore rules remain to protect data from
older checkouts. Cleanup is repository-only; it does not prune recovery assets.
Further native acceptance is deferred at the operator's direction. S5 still
requires final evidence/rollback handover and CI for the unpushed changes;
unavailable stable production and unverified layouts remain explicit limitations.

Validation passed: **54 targeted workflow/input/profile/native tests**, then
**252 wider deployment tests, 10 skipped, no failures** (overlapping suites).
All **11 retained Ansible plays** passed syntax checks against a deliberately
unreachable inventory without target execution. Active automation has no
references to the retired entry points; defensive Docker ignore patterns and
historical/retirement records are intentional, not active consumers.

### Further native acceptance: approved one-hour scope (2026-11-15)

The operator subsequently authorized the prepared-Pi native acceptance work,
explicitly limiting observation to **3600 seconds**, not a six-hour run.
Structured confirmation additionally approved **one Pi reboot** for native
startup checks and selected **restore the original Docker deployment** after
the trial. Listening remains operator-controlled; no automatic playback,
source, power or volume changes are authorized by the observer.

A fresh private baseline captured the healthy Docker deployment, all seven
existing native audio/agent/supervisor identities and twelve protected file
hashes/owners/modes. Trusted Pi tooling remains committed revision `1c574bd`;
uncommitted repository retirement changes are not installed on the Pi.
The existing sealed native release from `3e922d8` was reverified and the actual
Ansible native staging play produced a second sealed source/venv release from
`1c574bd` for a native-to-native update/recovery rehearsal. Neither staging
step changes the playback stack.

The planned sequence is reviewed Docker-to-native cutover with a new off-Pi
backup, native-to-native update and explicit native recovery, unchanged rerun,
the approved reboot, complete routing/readiness warmup, then the bounded
read-only observation and reviewed restoration of the captured Docker baseline.
Every lifecycle operation uses the public operator and independent backup
verification. Observation failure is recorded, not silently remediated.
At this point the further acceptance is **in progress, not completed**.

**Subsequently paused at 19:53 CET:** the operator aborted the controller
session and requested restarting native deployment only, with no tests.
The retry `fc48434c-1a55-4308-9686-428140fef770` remained pre-apply at
`inputs-verified`; both native writers were stopped and an orphaned target
operator was still running. After confirming its exact PID and pre-apply
state, terminated only that orphan and used the public
`native-resume-original` action with fresh reviewed approval. Recovery
preserved current data and returned the committed native operation
`ae8e2331-772e-4860-aefa-e2048848fffa` to an unblocked state.
Web and dedicated serial services were active/running with zero restarts;
Docker writers remained stopped. No observer or deployment operator remained.
No reboot, acceptance test or timed observation was started after this request.
The native update candidate was not applied; partial backups/evidence remain
for inspection. Further tests and final Docker restoration are paused pending
renewed operator direction.

**Separately approved reboot-only test at 19:56 CET:** performed exactly one
Pi reboot. Boot ID changed from `73e33fa4-edb2-435d-9b94-3fce13ff02ad` to
`2614beeb-7a5a-4593-b3c9-3423fdda7a26`. Native web, dedicated ser2net,
TLS proxy and all seven existing audio/agent/supervisor services started
automatically, active/running with zero restart counts; Docker writers stayed
stopped. Protected file hashes/owners/modes matched the pre-reboot snapshot
and existing user-service command identities matched after excluding dynamic
systemd timestamps/PIDs.

**Result: failed controller-recovery acceptance, not a passed reboot test.**
The required runtime check timed out after 60 seconds; subsequent API checks
still reported `backend_status.connected=false`. Authenticated playback health
was `ok` and all three output readiness/route probes were fresh, ready and
correct. Gateway startup journal reported an initial invalid network-address
error for `tcp,127.0.0.1,6666`, followed by accepter startup ten seconds later.
Web initially got connection-refused and then logged gateway reconnection,
but that TCP recovery did not establish controller readiness. The gateway held
the USB serial device and listened on loopback; web listened on port 8000.
Root cause is not established by these observations. No additional reboot,
service restart, native update, playback mutation or observation was performed.
Private reboot receipts remain with this trial's off-Pi evidence.

**Root-cause diagnosis after 20:02 CET:** no services were restarted or
reconfigured. USB enumerated normally, ser2net alone held `/dev/ttyUSB0`,
serial framing was 19200 8N1 with software/hardware flow control off, and the
web-to-gateway TCP connection was established. The actual sealed web source
contained the watcher gate
`should_poll = self._has_active_event_clients() or self.controller.backend_health_snapshot() is None`.
An initial failed startup poll records disconnected health; subsequent headless
iterations then skip hardware polling because health is no longer `None`.
Gateway reconnect reattaches the listener but does not invalidate that failed
health or request a fresh full poll. Normal health/rooms GETs serve that cached
failure instead of polling hardware.

Verified the causal trigger on the live Pi: immediately before a read-only
`/api/events` subscription the controller API still reported disconnected.
Registering the event client requests a full poll; within the first two-second
check the API reported connected, with no error message, and remained connected
after the diagnostic subscription closed. Web/gateway PIDs remained 1194/908,
with zero restarts. This demonstrates that hardware/serial communication works
and identifies the persistent failure as application health/poll scheduling
after delayed gateway startup, not proof of a broken controller or adapter.
The early ser2net listener delay triggered the startup failure but does not
explain its indefinite cached status. A source fix and regression validation
remain required; the read-only subscription is diagnostic evidence, not the
permanent fix or a passed unattended reboot test. No observation was started.

### Blocking bugfix: unattended controller-health recovery

**Status: CLOSED for controller-health recovery.** Native deployment acceptance
remains open; the latest reboot exposed a separate audio-readiness blocker.
Scoped bugfix commit: `060084d936ae31578512756d3907b7e11dab008f`.
This is shared application behavior,
not a native-only deployment defect: Docker, native and split-host layouts
using this watcher can retain a failed startup health result without an event
client. A read-only native reproduction established this failure; the other
layouts have not independently reproduced this failure.

This fix and its acceptance evidence are prerequisites for closing the
deployment plan. They do not replace the remaining native update/recovery,
reboot, approved one-hour observation and audible acceptance requirements.

#### 1. Reproduce in focused regression tests

- [x] Reproduce failed initial connection followed by gateway availability,
  with no SSE/browser client throughout. Assert that a real successful
  hardware-read result, not TCP connection alone, publishes connected health.
- [x] Cover an established gateway socket with initially silent hardware
  that later answers, without a socket reconnect or event subscription.
- [x] Cover loss and reconnection after a previously successful poll, including
  reconnection when the cached health still says connected.
- [x] Cover persistent unavailability, healthy idle behavior, active SSE
  behavior, shutdown and stale-generation/configuration result rejection.
  Use deterministic synchronization/clock control rather than long sleeps.

#### 2. Fix the shared polling/reconnect producer

- [x] Retry unknown or failed controller health without requiring event
  clients, using the existing bounded polling cadence and transport backoff.
  Do not add a busy loop, parallel polling worker or per-request hardware I/O.
- [x] Ensure a successful gateway reconnect schedules a full hardware poll
  even when the previous cached health was successful. Preserve the existing
  healthy-idle optimization outside recovery; continuous headless liveness
  monitoring is not added by this fix.
- [x] Keep TCP reachability separate from verified controller responsiveness.
  An unavailable or silent controller must remain explicitly unhealthy until
  a successful hardware response; do not clear failure into optimistic health.
- [x] Preserve state-lock boundaries, generation guards, listener lifecycle,
  shutdown responsiveness and normal event-driven refresh behavior.

#### 3. Validate locally and package the fix

- [x] Run the failing regressions first, then the related server, controller
  and connector suites and affected deployment/runtime checks. Verify retry
  counts/cadence and exact health payloads, not just worker invocation.
- [x] Update directly related runtime/deployment documentation with the
  verified behavior and recovery bound. No deployment-specific workaround or
  service ordering change substitutes for fixing shared application recovery.
- [x] Once approved for release, produce the sealed native candidate and
  corresponding container artifacts from the same corrected application
  revision through the existing reviewed workflows.

#### 4. Live acceptance, separately authorized

- [x] On the Pi, deploy through the reviewed transaction workflow and repeat
  unattended reboot acceptance with no browser/SSE client open. Require
  confirmed controller health within the existing 60-second runtime-check
  window; retain timestamps, service restart counts and controller responses.
- [x] Exercise delayed gateway availability and recovery from an established
  but initially nonresponsive controller using controlled test doubles first.
  Any live service interruption, hardware disruption or additional reboot
  requires explicit operator approval.
- [x] Check the corrected container deployment path and routing as well as
  native behavior; reuse shared application regressions across layouts, but
  record live results only for the layouts actually exercised.
- [x] Return the other native acceptance items to the parent plan, preserving
  their approval boundaries. The one-hour observation remains paused, not
  replaced by a six-hour test or implied by this bugfix closure.

**Bugfix exit gate:** regression coverage proves autonomous recovery without
event clients and the corrected deployment passes unattended controller-recovery
acceptance. The parent deployment plan separately requires its remaining gates
to have their own evidence.
A browser-triggered refresh, healthy TCP socket or running service alone is
not sufficient. Planning this fix authorizes no live mutation, reboot,
observation, commit or push.

**Implementation and local evidence:** six of the first eight deterministic
watcher tests failed on the original code; all passed after the scheduling fix.
The expanded targeted suite ran 217 tests with one opt-in container test skipped
and no failures. The separately enabled real-container acceptance then passed
using an immutable, verified Linux ARM64 web/dummy bundle built from `060084d`:
web started before the dummy gateway with failed controller health, recovered
without event clients or web replacement after the gateway appeared, and
passed existing rerun, forced recreation and external-gateway routing checks.
Production Pylance diagnostics contained no errors or warnings; changed tests
passed syntax validation.

The operator approved the scoped commit (no push), native update, one further
unattended reboot and local container acceptance, but not a timed observation.
Trusted tooling was upgraded by guarded atomic exchange to `060084d`, retaining
the old checkout. The actual native staging play produced sealed release
`5b2507b4-f5b1-4d00-807d-6a7c50f76959` with seal
`b43fc6ce57b33dc33dc46c9ab9ffaa70f5e720f3b98ae7b5439dbc600171699c`.
Reviewed same-layout update `0eb36b79-8003-4acc-9507-30a9ee37bc53` was approved
with protected audio/agent/proxy scope unchanged and the off-Pi backup guard
retained. It completed with all four native runtime checks passed, protected
configuration/service identities unchanged, Docker writers stopped and no SSE
clients. All three authenticated playback output/route probes passed after
the update.

**Measured live controller-recovery acceptance:** the first newly approved
reboot recovered controller communication without an event client, but the
session helper then incorrectly queried system rather than user services.
Read-only correction verified the actual user services; the original elapsed
samples were not retained, so that run was not used to certify the 60-second
threshold. The operator separately approved one additional reboot and a
temporary Pi-local test-only systemd probe.

That probe measured hardware health **10.958475815 seconds after native web
service activation**, within the required 60 seconds, with zero event clients.
It used Pi monotonic timestamps and persisted samples independently of laptop
or SSH delays; its own tests reject success arriving after the deadline and
reject an active event client. Boot changed from
`4163d139-21cc-49fd-acb6-606fa6e09c81` to
`50e8c04a-26ed-4299-9934-f4917e813761`. Web and gateway were active with zero
restarts. The temporary probe unit was disabled and removed afterward; no
additional reboot or observation was performed. The corrected application and
trusted tooling remain at `060084d`; no registry publication or Git push occurred.

**Separate native acceptance blocker, not a controller bugfix failure:** after
the final reboot, playback API connectivity was healthy but all three outputs
reported `hardware_profile_unavailable` readiness errors and pending route
observations. The three MPD output services were retrying startup with restart
counts of one; PipeWire, WirePlumber, playback agent and supervisor were active
with zero restarts. Protected configuration, secrets and unit file hashes were
unchanged, and Docker writers remained stopped. The overall native reboot/audio
acceptance is therefore **not passed**. Do not start the one-hour observation
until audio readiness is diagnosed and corrected with appropriate approval.
No audio configuration, profile, playback setting or protected service was
manually changed during this bugfix acceptance.

The session-only observer's user/system manager selection was corrected
locally and its five synthetic tests passed. Its previously installed,
never-started Pi copy must be refreshed and verified before any future
observation. Four additional Pi-local probe tests passed. Off-Pi backup,
container build/runtime, transaction, boot and correction receipts remain
private session evidence; the first inferred reboot receipt is explicitly
superseded by its timing correction and the measured Pi-local receipt.

### Audio profile investigation and approved runtime recovery

The subsequent investigation reproduced the readiness failure directly with
the deployed audio code, using `enforce_profile=False`. USB selection was
unique and stable (`0d8c:0102`), ALSA enumerated the card, and the complete
configured downstream graph passed validation. There were no full-speed USB
reset messages in this boot's kernel journal. Nevertheless, both `pactl` and
native PipeWire `Profile` parameters reported profile index zero, `off`, while
the same card still owned an existing six-channel `analog-surround-51` sink.
WirePlumber's saved default profile already requested
`output:analog-surround-51`; a wrong saved operator profile was not observed.

This inconsistent live profile state explains why all three MPD services
remained in their audio-readiness `ExecStartPre` guards: those guards correctly
require the reported active profile and full downstream topology, not merely
an existing sink. Repeated ordinary requests to select 5.1 returned success
without changing the reported profile. The playback API accurately reflected
this failure; the web controller-health fix was not its source.

The operator approved progressively scoped runtime experiments:

1. Stop only the three failing MPD outputs, issue one serialized selection of
   the configured profile, and request their startup again. The profile stayed
   `off`. This experiment **did not prove isolation**: the playback supervisor
   independently starts those services, and a later stop conflicted with it.
   The planned second profile experiment aborted before profile mutation.
2. With separate approval, pause the playback supervisor as well as all three
   failing outputs, then perform one explicit `off` -> configured 5.1 reset.
   Explicit `off` removed the stale hardware sink. Selecting 5.1 then recreated
   the sink and both native PipeWire and PulseAudio consistently reported
   `output:analog-surround-51` (profile index 15). The state remained correct at
   the one- and three-second checks. All four services were requested to start
   again afterward, allowing the supervisor to reconcile saved intentions.

**Verified result:** all seven audio/agent/supervisor services are active and
running. All three outputs subsequently published fresh readiness `ready=true`
and route `correct=true` with the exact expected virtual sink. Routes initially
remained pending and then became fresh/healthy during bounded warmup.
Controller connectivity remained healthy. The boot ID did not change,
PipeWire/WirePlumber were not restarted, and protected configuration, secrets
and unit file hashes were unchanged. No new playback intention, test audio,
reboot, observation or application fix was introduced. Restart counters now
reflect the approved manual service lifecycle and do not retroactively pass
the failed zero-intervention reboot.

**Remaining uncertainty and acceptance consequence:** the failure mechanism
is a stale/inconsistent audio-server profile state that the current
same-profile retry cannot clear; the reason this boot entered that state
(including any startup race or upstream PipeWire/WirePlumber defect) is not
established. Concurrent readiness guards are a possible startup factor, not a
proven root cause. The explicit reset is verified runtime recovery, **not an
implemented permanent fix or unattended reboot pass**. Do not bypass profile
verification or blindly add repeated `off` resets: they can disrupt working
outputs. A follow-up should reproduce the inconsistent state and evaluate a
single coordinated, bounded, identity-checked recovery path before further
native reboot/endurance acceptance. Private isolation/reset and final fresh
probe receipts remain with this trial's evidence.

### Extended audio startup isolation: profile restore failure without writers

The operator then authorized further diagnostic reboots and playback changes.
One additional reboot was performed, from boot
`50e8c04a-26ed-4299-9934-f4917e813761` to
`92c983d6-6c58-4461-93b1-9dadfc29e236`. A temporary Pi-local, read-only
transition recorder captured normal USB initialization on this run:
no device, then `off` without a sink, then profile 15
`output:analog-surround-51` with the six-channel sink. All seven services
started with zero restarts. The recorder unit was disabled and removed.
This successful run does not erase the earlier intermittent failure.

Controlled trials then paused the supervisor and output services and restarted
the audio session to isolate profile restoration without further Pi reboots:

- Eight warm-session profile-selection trials, with either one or three
  simultaneous `pactl` requests, all succeeded. Concurrent profile requests
  on an already initialized session alone did not reproduce the fault.
- Cold-session trials reproduced the contradictory `off`/existing-5.1-sink
  state repeatedly with the deployed readiness guards active. Single-guard,
  restore-only and additional triple-guard runs also often passed; the
  failure is intermittent, and verbose logging changes its timing.
- Tracing the actual deployed guard showed ordinary 5.1 requests returning
  successfully while both inventories remained `off`. In a failing run,
  WirePlumber logged selecting the stored 5.1 profile at boot-monotonic
  707.947 seconds, then route restoration for **`off`** at 707.972 seconds.
  The first traced application profile request was not until 708.649 seconds.
  Thus the inconsistent restoration was already visible before that request.
- Most importantly, a control trial reproduced failed restoration with the
  supervisor and all MPD outputs stopped and **only read-only readiness
  clients**. Their mutation function was guarded to raise if called, and
  recorded profile-write count was zero. In that run WirePlumber selected
  stored 5.1 at 1098.247 seconds but restored routes for `off` at
  1098.281 seconds; native and PulseAudio reads continued reporting `off`
  through the end of the five-second trace.

**Revised causal conclusion:** concurrent MPD startup guards are **not a
necessary cause**. The fault can originate within the prepared host's
PipeWire/WirePlumber profile-restoration/notification path without application
profile mutations. Current versions on this host are PipeWire/Pulse/SPA
`1.4.2-1+rpt3` and WirePlumber `0.5.8-2`. The correct saved profile is selected,
but the exposed active-profile state can remain inconsistent with the created
sink. Same-profile retries do not repair that state; an explicit off/on
transition does. This narrows the responsible layer, but does **not** establish
the exact upstream C/Lua defect, a fixed package version, or that a lock around
the existing MPD guards alone would prevent it. The failure is below the web
application and is not intrinsically specific to the native web layout.

One extended logging harness exceeded its controller timeout while reading
journal history. Its temporary debug override was removed and the diagnostic
process exited; output/supervisor startup was explicitly restored afterward.
Subsequent journal reads were bounded, results persisted directly, and cleanup
was verified. This harness issue is not audio acceptance evidence.

**End state verified:** supervisor, all three MPD outputs, PipeWire,
WirePlumber and playback agent are active/running; each output has fresh
readiness `ready=true` and exact-route `correct=true`. Controller communication
is healthy. Protected configuration, secrets and unit files match the initial
baseline. Temporary boot units and runtime debug drop-ins are absent. Saved
intentions were reconciled by the existing supervisor; no new application
playback command was issued. No packages or production code were changed, no
commit/push was made for this investigation, and no timed observation started.

**Permanent-fix direction:** retain strict profile/topology validation. A
follow-up must either verify an upstream audio-stack correction against this
reproducer or add a single, bounded, identity-checked recovery coordinator for
the demonstrably inconsistent state, avoiding resets of functioning outputs.
Serialization can protect recovery, but is not by itself a proven fix for the
restore defect. Add regressions for successful-but-ineffective profile requests,
transient restoration, missing/ambiguous/replaced hardware and recovery limits,
then repeat cold-start/reboot acceptance before native endurance validation.

### Guarded audio recovery implementation trial: promotion held

The operator approved the targeted upstream check, bounded shared-host recovery
if needed, and subsequent acceptance. The available Pi PipeWire candidate is
`1.4.2-1+rpt5`; its packaging changelog contains libcamera changes only.
WirePlumber's candidate remains `0.5.8-2`. Public upstream searches/release notes
did not establish a matching correction. No audio packages were upgraded.

An uncommitted candidate in the shared `lib/audioready.py` now:

- Serializes selector-based profile enforcement using a nonblocking, per-user
  kernel lock in a validated private `XDG_RUNTIME_DIR`. Read-only observers
  neither acquire the recovery lock nor mutate profiles/state.
- Requires agreement between native and Pulse profile observations before
  declaring selector hardware ready; downstream/sink checks remain mandatory.
- Recognizes the narrow reproduced `off`/existing valid 5.1 graph inconsistency.
  Normal profile selection is retained during a five-second observation grace.
  Healthy readiness cancels the observation.
- Refuses resets while a sink/playback stream is running or its activity state
  is unknown, or when hardware identity, topology or native profile inventory
  cannot be established.
- Persists one reset attempt per USB device/PipeWire-server generation before
  issuing off/on. Other guards, service retries and guard crashes cannot
  replenish that budget. Server cookie, object serial/ID and Pulse index
  identify the generation. Identity is rechecked before profile changes and
  after off; server/device replacement aborts re-enable.
- Requires a subsequent complete readiness check after the reset. Command
  success is not readiness. Errors and exhausted budgets remain explicit.

**Repository validation:** 205 focused audio and coupled playback tests pass;
production Pylance diagnostics are clear. Two new regressions fail against
unchanged HEAD: missing off/on recovery and accepting native/Pulse disagreement.
Tests also cover grace cancellation, healthy/read-only no-ops, active streams,
kernel lock exclusion, identity/index reuse, replacement/server restart during
recovery, per-generation budgets, failed resets and unsafe/corrupt state files.

**Live candidate only, not installation:** the candidate was staged separately
from the installed audio checkout. Sixty of 61 bounded cold-session trials
passed with three concurrent candidate guards. Five genuine restoration
failures, established by a read-only startup phase before candidate execution,
recovered automatically with exactly one reset each. Fifty-five normal trials
needed no reset.

**Remaining blocker:** one trial had native Profile inventory cardinality other
than one. The candidate refused mutation and all three guards timed out.
Its error identified the cardinality check, but this first harness did not
retain the raw Profile array; whether it was empty or had multiple entries
is therefore unknown. Two subsequent bounded capture runs (16 and 20 trials)
did not reproduce that shape, although they each reproduced and recovered two
ordinary stale-off failures. These later successes do not supersede the failed
trial or prove the additional inventory condition resolved.

**Promotion is held**, rather than loosening native-profile validation based on
an assumption. Capture that inventory variant and establish safe handling (or a
verified upstream correction), then rerun acceptance. No permanent audio module
was installed, no Pi reboot was performed in this implementation trial, and no
one-hour observation was started. All seven original audio services and fresh
three-output readiness/exact-route probes were verified healthy afterward;
controller communication and protected configuration are unchanged. The
installed audio module still matches its pre-trial backup. Diagnostic candidate
files are retained for follow-up, with no background harness left running.

### Approved session recovery and installed reboot acceptance

The operator authorized the follow-up investigation and explicitly selected
**one guarded WirePlumber restart while audio is idle** when asked whether to
expand recovery beyond the previously approved USB off/on reset.

**New real-fault evidence:** the first follow-up capture failed on trial 11
because the selected USB device's entire profile catalogue disappeared:
Pulse reported `profiles={}` and `active_profile=null`; `pw-dump` reported an
empty native `EnumProfile`. A separate direct `pw-cli enum-params` returned no
EnumProfile entries, although native Profile still had one `off` entry. Pulse
logged inconsistent port/profile counts, and both desired-profile and cleanup
off requests returned `No such entity`. Thus this case is not merely our
Profile-array parser rejecting a healthy card. Restarting WirePlumber restored
the catalogue and original output services. No package/configuration change
was needed.

The original earlier non-singleton Profile array was **not** recaptured; its
exact shape remains unknown. Another 30 bounded read-only-first trials passed,
including six stale-off recoveries. This does not prove that earlier
cardinality failure had the same cause as the empty-catalogue failure.

**Approved extension implemented:** under the same kernel lock, missing native
Profile/EnumProfile or missing Pulse profile inventory must persist across a
five-second grace and fresh device-identity checks. All inspected sink/playback
nodes must have a known idle/suspended state. The guard persists a global
once-per-boot WirePlumber-restart budget before requesting the nonblocking user
service restart. Changing device/server generations, guard retries and failed
commands cannot replenish it. A healthy inventory cancels an unspent
observation. Conflicting Profile entries and missing/ambiguous/replaced
hardware still fail closed; normal read-only probes never recover audio.
All configured profile, sink and downstream checks remain required after
either recovery path.

**Verification before installation:**

- 235 targeted audio, stream and coupled playback tests pass, one skipped.
  Added regressions cover empty versus conflicting native profiles, transient
  publication, active/unknown audio, cross-generation boot budgets, failed or
  timed-out restart requests and read-only isolation. Production Pylance
  diagnostics and test-file syntax checks are clear.
- The extended candidate passed 30 further real cold-session trials with three
  concurrent guards. Seven real stale-off faults recovered with exactly one
  card reset each; other trials required no reset. Missing inventories did
  not recur in this set.
- A separately labelled **reader-only fault injection** substituted empty
  Profile/EnumProfile arrays in the candidate's observations until the real
  WirePlumber device generation changed. Three concurrent guards requested
  **exactly one real WirePlumber restart**, then all passed against fresh,
  uninjected native/Pulse inventories. This validates the live recovery path,
  not spontaneous reproduction of the missing-catalogue fault.

**Installed prepared-host fix:** after those gates passed, the exact tested
module, SHA-256
`5ff2d9dbb3f111d19f75d960e53fc6341647146ee08e58f6732943481bd6812f`,
was atomically installed into the existing prepared audio checkout with its
owner/mode preserved. The previous module was verified unchanged before the
swap and retained in the private recovery directory with an activation receipt.
Supervisor/MPD writers were paused, the agent restarted to load the shared
change, then outputs/supervision restored. No unit, credentials, application
configuration, audio package, web release or deployment transaction was changed.
This is a scoped, uncommitted prepared-host audio-module update, not a new
packaged/native-web release or a claim that the entire audio checkout was
updated. Preserve the backed-up pre-existing checkout changes.

**Two unattended installed reboots passed:**

1. `92c983d6-6c58-4461-93b1-9dadfc29e236` to
   `3bd42689-32d4-4fc1-94bf-106ee51ae6a4`.
2. `3bd42689-32d4-4fc1-94bf-106ee51ae6a4` to
   `b78f85f7-9a0b-449e-93b3-73b8db16f929`.

Both returned with all seven audio services active/running and zero automatic
service restarts, fresh three-output readiness/exact routes, healthy controller,
zero SSE clients and unchanged protected baseline files. The installed module
hash survived both reboots. Empty recovery records and correctly filtered
audio-unit journals show no reset/restart was needed on these healthy boots.
Final native runtime checks passed units, HTTP, backend and playback, with
committed operation `0eb36b79-8003-4acc-9507-30a9ee37bc53` unchanged and unblocked.

The first receipt collector failed to parse a normally empty recovery-state
file after the first reboot; it was corrected and resumed receipt collection
without another reboot. No first-reboot timing is claimed. A self-matching
journal query was also corrected using actual user-unit fields and normalized
boot IDs; its correction receipt supersedes the earlier log fields. These
collector issues are not application failures or timed-endurance evidence.

**Disposition:** the implemented bounded recovery and installed reboot gate
are validated for the evidence above; native validation can continue. Exact
upstream internals and the first unknown Profile-array shape remain unproven,
and conflicting inventories deliberately remain explicit failures rather than
being accepted. This is a shared-host resilience fix, not an upstream defect
closure or proof that arbitrary audio failures self-heal. Remaining native
lifecycle/rollback and audible/endurance acceptance are still open. No timed
observation or six-hour test was started, and no commit or push was made.

### Resumed native lifecycle and one-hour observation (2026-11-16)

The operator authorized continuing native acceptance and then explicitly
approved the reviewed update and recovery scopes. To avoid intentionally
reintroducing the known controller bug, this rehearsal used **two separately
sealed releases of the same fixed application revision** `060084d`.
It validates native source/environment release switching and matched-data
recovery mechanics, not cross-version compatibility. The earlier real
native-to-native code update to `060084d` remains separate evidence.

- Actual Ansible staging produced release
  `eb346206-d97f-45be-b8ba-710a114d44f6`, seal
  `3f54376088d1fd00df35a6640bcc843245bb7a22c9f2e05acf90478716400504`.
  The original fixed release remained
  `5b2507b4-f5b1-4d00-807d-6a7c50f76959`.
- Public-operator update `32258c33-edc1-4e2e-adf0-36268490ae0c` completed
  after a reviewed, independently verified off-Pi backup. All required
  units/HTTP/backend/playback checks passed. The backup retained its existing
  expensive quiescence checks; no checks were bypassed to shorten the outage.
- Separately reviewed native recovery
  `0284b449-2041-47eb-9053-1ae15f2aec67` passed matching-data/original-runtime
  checks, restored the original fixed release and committed operation
  `0eb36b79-8003-4acc-9507-30a9ee37bc53`, and preserved post-backup trees.
- Identical rerun `7bf82026-f706-4b61-9a11-03472e540bee` returned
  **unchanged**, with all required checks passing. Exact recovered runtime
  identity, service PIDs/restarts, protected files and committed selection
  remained unchanged. Existing audio/agent services were untouched throughout;
  the installed guarded audio fix remained in place.

The first local operator invocation refused before target reservation because
its required evidence directory did not yet exist. Target state was checked
unblocked before creating that directory and requesting a fresh review. This
preflight refusal is not an applied update or recovery failure.

**Observation started, not completed:** the refreshed read-only observer was
staged to a new private Pi directory and its seven deterministic tests passed
both locally and on the Pi. A fresh complete baseline passed with 20 protected
files, adding the installed audio-recovery sources/configuration, relevant unit
drop-ins and the Pulse service. API snapshot/readiness/route observations must
be strictly younger than 30 seconds; exact three-output and downstream-route
checks remain required.

Pi systemd unit `mpdrussoundpi-native-1h-20261116.service` started the observer
at **2026-11-16 00:07:52 UTC / 01:07:52 CET**, with a monotonic **3600-second**
observation deadline and ten-second sample interval. Expected completion is
**01:07:52 UTC / 02:07:52 CET**, not evidence that it has completed.
The first persisted sample and complete runtime/routing checks passed, and the
unit was verified active/running. Durable Pi-local status/sample files survive
SSH disconnect or laptop sleep; the temporary Mac sleep inhibitor was stopped
after the lifecycle transactions completed.

The observer performs no recovery, restart, playback change or rollback;
failures are persisted and end the run. There is no automatic Docker restore
scheduled. Native remains active and Docker writers remain stopped pending
observation results and final-layout review. Audible native confirmation,
completed observation evidence and final handover remain open. No six-hour
observation, commit or push was started by this continuation.

**Operator-approved extension:** at approximately 01:10 CET on 2026-11-16,
the operator extended the observation to **four hours total**, measured from
the original 01:07:52 CET start. The new expected finish is **05:07:52 CET /
04:07:52 UTC**. The original one-hour process was left uninterrupted because
its deadline was fixed at startup. An overlapping Pi-local read-only observer,
`mpdrussoundpi-native-4h-20261116.service`, continues through the new deadline,
using an exact copy of the original protected baseline, selection and operation,
not a newly captured baseline. Both evidence streams must be reviewed together;
the original one-hour completion alone is no longer sufficient for acceptance.
The extended observer's nine deterministic tests and initial API/full-runtime
checks passed on the Pi, and its service was verified active/running. Neither
observer remediates services or restores Docker. Four-hour completion remains
pending; native audible confirmation and final handover remain open.

### Completed four-hour observation and current read-only check (2026-11-17)

Both Pi-local observer streams were reviewed after completion. The original
stream ran from **2026-11-16 01:07:52 to 02:07:52 CET**, completing 341 API
samples and 57 full checks with zero failed samples. The overlapping extension
started at **01:09:59 CET** and completed at **05:07:52 CET**, with 1,359 API
samples and 227 full checks, also with zero failed samples. Both systemd
services exited successfully. Their protected baselines, native specifications,
candidate selections and committed operation files matched byte-for-byte; the
maximum consecutive sample gaps were 13.35 and 13.40 seconds respectively.
Together the streams cover the approved four-hour window; neither performed
automatic remediation or Docker restoration.

At **2026-11-17 01:13 CET**, a new read-only API and full baseline check passed:
controller connectivity, authenticated playback, the three fresh audio
readiness/routes, deployment selection, runtime identities, protected files and
stopped Docker writers matched the observation baseline. This is a point-in-time
check, not continuous monitoring after the observation ended.

The observation establishes monitored health, not audible output. On
**2026-11-17**, the operator independently operated the web controls and
confirmed in Living Room that `stereo_front`/source 1,
`stereo_centerlfe`/source 2 and `stereo_rear`/source 3 each played the correct
station clearly and uninterrupted on both speakers. The operator was asked to
listen for about 30 seconds per path at a comfortable volume and reported each
passed with “all good”; the listening duration was not independently measured.
This verifies Living Room only, not every room. No assistant-issued playback,
zone power, source or volume changes were made.

At **01:20 CET**, a fresh read-only API and full baseline check passed after
listening, with the controller connected and all three outputs ready with
correct routes. The operator selected restoring the previous Docker layout as
the desired final state and requested a separate review; no restoration has
been performed. The committed native selection is newer than the original
Docker-to-native cutover, and the recovery operator only accepts the latest
committed operation. Therefore the original cutover cannot simply be recovered
directly: establish and review a supported return path and preservation of
current state before any service change. S5 and its evidence/recovery handover
remain open until the layout and remaining documentation/CI gates are
addressed. The release-switch rehearsal remains same-revision evidence, not
proof of cross-version or schema rollback compatibility.

**Recovery-chain inspection (2026-11-17):** the host is unblocked with native
operation `0eb36b79-8003-4acc-9507-30a9ee37bc53` committed. Both relevant
off-host backups passed portable-manifest and blob verification against their
separately retained manifest identities. The current operation's backup
identifies an earlier native deployment
`ae8e2331-772e-4860-aefa-e2048848fffa`; that operation's backup identifies the
previous Compose deployment. Sequential recovery would therefore restore two
historical data snapshots, preserve post-backup trees separately, and briefly
run the earlier native release before restoring Docker. It does not merge
current state into the restored data. Its suitability, the historical Docker
image's controller-recovery behavior and any current-data migration require
separate review; no recovery was started and no current data was replaced.

### Proposed current-data-preserving native-to-Docker return (2026-11-17)

The operator selected designing and validating a safe migration instead of
rolling back historical data. This is a new, separately reviewed cross-layout
transaction, not a bypass of the existing recovery checks. Ordinary Compose
apply explicitly refuses a committed native deployment. No currently supported
operation implements this return while retaining current data; the proposal
below is not an implemented or approved live migration.

**Image preparation (2026-11-17):** a local `linux/arm64` managed web/ser2net
bundle was built from commit `e93783bbbb1c0c2c60add80563f03c9766a14093`,
which includes the controller-health fix. Its separately retained manifest
identity is `sha256:c6bf9b878327de45f9bb5aef792d3b162638cc5e006e80be3c736aba09a01dd1`.
Bundle verification, both isolated image smoke tests, and an isolated
web-to-gateway Docker-network TCP check passed. The gateway used a `stdio`
connector in the route test, not the physical serial adapter. This is local
image evidence only, not a registry publication, a schema compatibility
assessment, a tested migration transaction or live hardware acceptance.

**One-time Pi migration choice (2026-11-17):** the operator selected a
Pi-specific migration rather than a general cross-layout feature, with a
tested state handover rather than leaving deployment tooling blocked or its
committed native record misleading. A second managed ARM64 bundle was built
from the exact currently committed native revision
`060084d936ae31578512756d3907b7e11dab008f`; its independently retained
manifest identity is
`sha256:eb3312d820745381034ba1e7f6b08fd552abd44dfe15b720e10f6551182453ab`.
Verification, separate web and gateway smoke tests, and an isolated
web-to-gateway TCP route check passed. The gateway route test used `stdio`,
not the serial device. The native and container Python environments are
different, so matching source revision is not proof of runtime or schema
compatibility. The one-time handover, fresh quiescent off-host backup,
failure recovery, live activation and hardware acceptance remain outstanding.

The preparation-only `tool.deployment_pi_handover` derives a managed Compose
layout from the native scope and requires the same source revision, target
platform, serial device/framing, listener, data mounts and playback registry.
It rejects loopback playback endpoints, which would resolve inside a bridge
container, and Compose interpolation expressions. This helper does not verify
live provenance, reserve state, transfer or load images, capture a backup,
activate services or provide recovery. Its derived layout is not authorization
to perform the one-time handover.

State-layer tests establish that a reserved local image candidate keeps the
old native selection committed until the required backup, image-load and
completion boundaries have been recorded. A new internal handover recovery
state boundary requires the exact native committed record, a matching
same-revision managed image candidate, the backup phase and both native
recovery checks. A separate portable-backup verifier checks fresh data roots,
native units, gateway configuration and sealed release against independently
retained identities. Neither component stops writers or restores services,
and state receipts are not proof that those actions occurred. Ordinary image
recovery now also refuses to clear a pending operation when the committed
baseline is native; only the explicitly selected handover boundary can do so.
A backup-capture
boundary now requires the reserved operation, a quiescence guard and a
caller-supplied independent off-host verification before recording
`backup-complete`; a failed transfer retains the pending reservation. Internal
framed-transfer helpers now verify the received backup against controller-held
review inputs, persist its manifest identity before acknowledgement, and
reject an incomplete or mismatched receipt. A duplex test exercises these
helpers together with the capture boundary. They are not wired into the
operator or SSH transport and do not stop services. The one-time transaction,
operator integration, recovery orchestration and live acceptance remain
unimplemented; do not start a cutover based on these pieces.

An internal native-unit fencing primitive now requires the matching reserved
operation and committed native assets, verifies the existing native runtime,
records a one-shot attempt, disables and stops the two native units, and checks
quiescence and both listeners. It is not wired into an operator, and callers
must separately review and fence conflicting containers. Its mocked tests do
not establish Pi or reboot behavior. A separately reviewed
`handover-resume-original` action handles the reserved pre-backup phase without
replacing data and clears state only after native runtime checks. Its service
boundaries have only been mocked, not rehearsed on Linux or the Pi. Post-backup
and post-activation recovery remain unimplemented.

1. **Freeze the scope and candidate.** Bind the review to the current native
   operation, exact native units/release, current data root and ownership,
   host/ports/device, gateway and proxy settings, plus a verified compatible
   `linux/arm64` managed-gateway image pair. Require the web image to include
   the controller-health fix. A retained locally verified `060084d` web image
   bundle exists, but it is a **dummy-gateway bundle**, not an accepted
   managed-gateway pair; do not activate it or silently substitute images.
   The managed pair described above is locally verified; independently
   establish data/schema compatibility before considering it for activation.
2. **Implement a dedicated reviewed transaction.** Reuse the durable host-wide
   reservation and exact identity checks; revalidate scope immediately before
   stopping only the selected native web/gateway writers. Confirm all writers
   are quiescent, take a fresh consistent backup of the current data and native
   source/environment/runtime, and require independent off-host verification
   before activation. Preserve the active data and credentials in place, never
   restore either historical snapshot as migration input. Retain the prior
   native units and recovery evidence; do not change the agent, audio, proxy,
   OS or unrelated containers.
3. **Activate and validate the reviewed Docker pair.** Reject incompatible
   mounts, networks, port/device ownership, target identities and unexpected
   services instead of guessing. Verify exact loaded image identities, Compose
   units, web health, physical controller communication and authenticated
   playback readiness/routes before committing the new selection. Keep the
   controller fix and do not claim readiness proves audible playback.
4. **Recover explicitly on failure.** No automatic replay, rollback, data
   overlay or reservation clearing after uncertain dispatch. Preserve current
   and failed-state trees, logs and off-host backup; a separately reviewed
   recovery restores the matching native runtime and corresponding fresh
   backup. Specify and test each crash/backup/activation boundary before use.
5. **Validate before target approval.** Add deterministic transaction tests for
   native-to-Compose identity guards, fresh backup and independent custody,
   data/config/secret preservation, image/port mismatches, failures before and
   after mutation, interrupted execution, idempotency, exclusion races and
   reviewed native recovery. Exercise an isolated representative host first.
   Only then present an exact live review with interruption, rollback and
   current-data implications. After any separately authorized cutover, verify
   actual routing and obtain operator-controlled audible confirmation as
   appropriate; update the handover and S5 status only from real evidence.

The earlier GitHub CI runs predate deployment commit `e55d9de`; no CI run for
that commit was found on 2026-11-17. Do not report those older runs as CI for
the current changes. The implementation and live migration remain outstanding.

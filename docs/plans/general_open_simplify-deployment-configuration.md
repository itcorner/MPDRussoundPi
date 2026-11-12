# Plan: simplify deployment and configuration

Status: **open; inventory and contract review underway; preliminary design
recorded** (2026-11-12). The Waitress/debug change below is authorized
separately; this plan does not authorize deployment, service restarts, hardware
tests, or changes to a live installation.

## Goal

Reduce the number of ways to configure and deploy the solution, make the
remaining choices explicit, and provide one consistent, documented precedence
policy across components. Simplify the Ansible entry points so an operator can
understand which parts run where and which images or source revisions are used.
Preserve existing configuration and state through any migration.

## Proposed end state: operator overview

This is the **target**, not the current implementation. The choices recorded
in S1 are agreed; other defaults remain proposals. Paths and entry-point names
are a proposed contract; the S2 blueprint specifies candidate paths and
compatibility mappings, but none is an implemented command.

### Choose how to run it

| Operator choice | Default source and behavior | When to change it |
|---|---|---|
| **Docker development** | Published Hub development images for web and ser2net; detailed, bounded logs. No build. | Select an explicit local build only for source changes. Build on the Ansible controller from identified source and transfer verified images to the target. |
| **Docker production** | An approved matching pair of stable Hub images; minimal operational logs. | Select a different approved stable pair deliberately. Stable publication is not yet available: report the blocker instead of using dev or building. |
| **Docker local build** | Explicit dev opt-in, using the same development deployment profile. | Supply a clean, identified source revision; refuse ambiguous or dirty source without deleting or resetting changes. |
| **Bare metal** | Native web and gateway as selected, with native Pi playback services. | Use when containers are unsuitable. The exact component scope and migration from Docker need design. |

Two Compose files express **development** and **production**; four clear
Ansible intents cover **dev**, **release**, **dev local build**, and **bare
metal**, reusing shared tasks. The default Pi layout remains hybrid: web and
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
3. Back up configuration, state, credentials and deployed image/source
   identities privately; stop writers or use an online SQLite backup.
4. With explicit approval, apply only the selected components, retain existing
   state and verify web, gateway/controller, agent and audio separately.
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

## Current complexity and important constraints

- [`docker-compose.yml`](../../docker-compose.yml) currently declares both
  local `build` and local `image` names. The earlier baseline Docker playbook
  builds on the target; subsequent local work adds an optional registry
  override and flags for published images. Native web deployment is a
  separate playbook. These changes are not evidence of a released feature.
- Configuration is split among persisted application config/state, environment,
  CLI flags, Compose interpolation, Ansible variables, systemd units and
  agent/audio configuration. Some are deployment inputs or secret references,
  not interchangeable application settings. The
  [effective-configuration idea](./web_idea_effective-config-overrides.md)
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
2026-11-12. The detailed matrix below is in progress, not yet a complete
per-setting audit; unresolved items remain at the end of S0. Reconcile
earlier deployment work with the design before reuse. Never record
token contents, private keys or full stream URLs.

### Existing paths and component placement

| Path | Current behavior and important boundary |
|---|---|
| [Hardware Compose](../../docker-compose.yml) | Local web and ser2net images with build definitions; serial device/group passthrough; web config, data, logs and read-only secrets bind mounts; web port 8000 by default. A Compose environment value may be interpolation only or may be passed to a container. |
| [Dummy Compose](../../docker-compose.dummy.yml) | Locally built web plus dummy backend for hardware-free development; separate dummy state and port. Its mounts and passed settings differ from hardware Compose. It is not a physical-controller test. |
| [Docker Ansible play](../../ansible/playbooks/deploy-russound-docker.yml) | Earlier baseline clones/updates the target checkout, generates `.env`, builds there and can use an external gateway. It stops native web and, when managed, native ser2net. Subsequent local changes add explicit published-image references, a registry override, pull policy and optional controller check; they also change missing-device behavior. Review them separately, not as already shipped. |
| [Native web play](../../ansible/playbooks/deploy-russound-web.yml) | Target checkout and Python venv; systemd web service; optionally installs and restarts native ser2net. Its default Git version is `main`; CLI settings are generated in the unit. |
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
| Web backend and persisted UI config/state | [Endpoint resolver](../../web/config_types.py) and saved web config; nonempty host and valid port overrides can select TCP, including from serial. Invalid ports are currently logged and ignored; the [effective-config idea](./web_idea_effective-config-overrides.md) records a reporting mismatch. | Map every editor/API field, fallback, path, save behavior, consumer and compatibility requirement. Do not apply a blanket precedence claim. |
| Agent connections and security | `RUSSOUND_PLAYBACK_AGENTS` supplies a JSON list; Compose mounts token/certificate files separately. Web host/origin and trusted-proxy settings are process environment. | Record validation, defaults, file ownership, non-secret provenance, restart/recreate requirements and which deployments pass each variable. |
| Serial gateway | [Gateway entrypoint](../../docker/entrypoint-ser2net.sh) reads device, options, bind, port and optional connector from environment; native ser2net uses its configuration file. Compose supplies a subset plus host device/GID. | Map direct-entrypoint and Compose inputs separately, port exposure, ownership, validation and external-gateway behavior. |
| Playback and audio | [Admin config and runtime state](../documentation/mpd-headless-deployment.md): output hardware, station seeds and watchdog settings in config; favorites, intentions, selections and gain in state. Agent CLI and generated systemd units add identity, token path, listener and operations switches. | Inventory every schema field and CLI flag, defaults, validators, XDG paths, systemd environment, generated MPD/PipeWire files and their readers/writers. |
| Proxy and egress | Ansible variables render Caddy listener, certificate, CIDRs and operations policy; egress requires explicit resolver addresses and activation. | Map generated files, private assets, trust, migration and restart effects, including Docker/NAT source addresses. |
| Deployment and logging | Ansible inventory/play vars and extra vars generate files; Compose interpolates shell/`.env` into service definitions. Runtime logs, protocol audit, container stdout and systemd journals are distinct. | Inventory all variables and exact Ansible/Compose precedence, paths, logging destinations/rotation/redaction and whether settings reach the process. |

Complete S0 with a machine-readable or reviewable per-setting matrix: owner,
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
| [MPD setup](../../mpdrussoundpi/russound/mpdsetup.py) | Install generates config and units; start/stop/restart and enable/disable act on services; cleanup and kill are disruptive. Flags are not globally mutually exclusive: install may be followed by start and enable, while kill/cleanup take precedence and exit. It requires a positional config path even for cleanup/kill. |
| [MPD control](../../mpdrussoundpi/russound/mpdctrl.py) | Exactly one action is required. Status, route check and config revision are observations; start/pause/stop, set/apply gain and route recovery alter runtime or saved intent. Gain setting requires an explicit output. |
| [Supervisor](../../mpdrussoundpi/russound/mpdsupervisor.py) | `--initialize` writes missing intentions and exits; `--once` initializes then starts/reconciles MPD services once; normal mode continues reconciliation and may run the watchdog. Both flags together select initialization, not reconciliation. |
| [Egress](../../mpdrussoundpi/russound/playbackegress.py) | `--wait` checks existing enforcement. Normal mode requires user and UID, starts the protected slice, validates/applies nftables rules and writes enforcement state; it is not a read-only check. |
| [Audit](../../mpdrussoundpi/russound/playbackaudit.py) and [stream levels](../../mpdrussoundpi/russound/streamlevels.py) | Audit records observations to an output directory, or `--install-service` generates a user unit without starting it. Stream-level measurement reads sources and may write a new explicitly named config; it refuses an existing output or the input path. Neither is a deployment dry run. |

### S0 follow-up: earlier local deployment work

The changes reviewed here were uncommitted at the time of this inventory.
They may be included in the documented baseline, but are useful prior work,
not the approved final design or evidence of a safe live redeployment:

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
[hardware Compose](../../docker-compose.yml), and
[native unit](../../ansible/templates/russound-web.service.j2).
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
[Docker](../../ansible/playbooks/deploy-russound-docker.yml),
[native web](../../ansible/playbooks/deploy-russound-web.yml),
[agent](../../ansible/playbooks/deploy-playback-agent.yml),
[proxy](../../ansible/playbooks/deploy-playback-agent-proxy.yml),
[egress](../../ansible/playbooks/deploy-playback-egress.yml),
[audio](../../ansible/playbooks/setup-musicpi.yml), and
[agent removal](../../ansible/playbooks/remove-playback-agent.yml).

| Inputs / owner | Defaults and downstream effect | S2 migration and verification |
|---|---|---|
| Inventory `musicpi`, `ansible_host`, `ansible_user`, `ansible_python_interpreter` | Inventory selects target and SSH account; plays target the `musicpi` host pattern and commonly set `/usr/bin/python3`. Deploy user defaults to Ansible user. | Support non-default inventory, separate controller/target paths and missing host errors; never equate a localhost syntax check with a matched target deployment. |
| Web `repo_url`, `repo_version`, `app_dir`, `deploy_user` | Both web plays default to repository URL, `main`, and a target checkout under the user's home; native adds `venv_dir`, `russound_config_dir`, `russound_state_dir`. Git updates are not forced. | Pin and report exact revision; test dirty checkout refusal, missing revision, permissions and native/Docker state preservation. |
| Docker `russound_publish_port`, `russound_serial_device`, `russound_serial_options`, `russound_ser2net_bind`, `russound_manage_serial_gateway` | Defaults 8000, `/dev/ttyUSB0`, `19200n81`, `0.0.0.0`, true; device and group feed generated `.env`. Gateway management false requires an external backend hostname. | Test device/group validation before service changes, external gateway and native service ownership; earlier baseline and subsequent missing-device behavior differ. |
| Docker `russound_backend_host`, `russound_backend_port`, `russound_debug`, `russound_playback_agents` | Defaults `russound-ser2net`, 6666, false, `[]`; generated `.env` supplies Compose. `russound_uid`, `russound_gid`, `russound_serial_gid` are resolved facts. | Test actual generated/parsed environment including F1, override precedence and external gateway. Do not persist credentials in generated env. |
| Docker `russound_use_published_images`, `russound_web_image`, `russound_ser2net_image`, `russound_published_pull_policy`, `russound_registry_override_dir`, `russound_verify_backend` | Defaults false, empty image references, pull `always`, private override directory, false. When enabled, explicit image refs and `--no-build` are used; optional rooms API check tests controller response. | Added after the earlier baseline. Review before reuse; test missing ref, `always`/`missing`/`never`, failed pull, compatible image pair, target architecture, zero unintended builds and staged verification. |
| Native web `russound_host`, `russound_port`, `russound_playback_agents`, `russound_manage_serial_gateway` | Defaults `0.0.0.0`, 8000, `[]`, true; systemd unit passes listener via CLI and agent JSON via environment. Native ser2net is optionally installed and restarted. | Test exact unit-to-runtime behavior including F1 and nondefault host; avoid unexpectedly modifying another gateway. |
| Agent `playback_agent_id`, `playback_agent_port`, `playback_agent_start`, `playback_agent_enable_operations`, `playback_agent_enable_tunein_selection`, `playback_agent_token_source` | Defaults `musicpi`, 8081, false, false, true; token source is required controller-side private file. Installation can occur without activation; unit listens on loopback. | Test absent/invalid token, read-only vs operations, recreation and preservation of token/ledger. Coordinate proxy permissions separately. |
| Agent `playback_agent_home`, `playback_agent_dir`, `playback_agent_venv`, `playback_agent_secret_dir`, `playback_agent_config`, `playback_agent_state_dir`, `playback_agent_unit_dir`, `playback_agent_source`, `playback_agent_environment` | Derive from audio user's home and controller checkout; runtime includes XDG, user D-Bus and Pulse socket. Paths are asserted absolute/systemd-safe. | Test non-default account/path, controller source identity, existing user bus and preservation; do not capture token contents in logs. |
| Proxy `playback_proxy_hostname`, `playback_proxy_allowed_cidr`, `playback_proxy_additional_allowed_cidrs`, `playback_proxy_port`, `playback_agent_port`, `playback_proxy_start`, `playback_proxy_enable_operations` | Hostname and primary CIDR required; port 8443, agent 8081, additional CIDRs `[]`, start false and writes false. Additional CIDRs were added in subsequent local work. | Test SAN/CIDR validation, Docker NAT source and both agent/proxy write gates; keep old access until explicit cutover. |
| Proxy `playback_proxy_caddy_binary`, `playback_proxy_caddyfile`, `playback_proxy_unit`, `playback_proxy_certificate`, `playback_proxy_private_key` | Point to system Caddy binary, unit/config and certificate pair; play requires Caddy and checks the pair. | Preserve and privately back up key/trust; test existing and absent pair, renewal boundary, permissions and isolated rollback. |
| Egress `playback_egress_activate`, `playback_egress_resolvers`, `playback_egress_user`, `playback_egress_install`, `playback_egress_source` | Activation defaults false; resolver IPs are required; default user `musicpi`; source copied from controller. | Test empty/invalid resolvers, missing nftables, existing unrelated firewall rules and MPD interruption; explicit approval for activation. |
| Audio `mpd_russoundpi_repo`, `mpd_russoundpi_dest`, `mpd_russoundpi_scripts`, `mpd_russoundpi_config`, `venv_dir`, `mpd_russoundpi_update_repo`, `mpd_russoundpi_start_playback` | Target checkout/config and venv derive from audio user's home; update and start default true. `init,new` staging guide explicitly sets start false; `update` tag requests OS upgrades. | Test source matching, persistent calibrated config, tagged scope and stop/start effects; staging still affects audio services and must not be presented as no-impact. |
| Agent removal `playback_agent_unit_dir`, `playback_agent_environment` | Removes the agent unit after stopping/disabling it; retains files and credentials. | Test absence/idempotence, private assets and unrelated services. Removing a unit is not complete data deletion or rollback. |

### S0 findings and remaining work

1. **F1 — empty-agent default is rejected.** Both web deployment plays default
   `russound_playback_agents` to `[]`; the
   [Docker environment template](../../ansible/templates/russound-docker.env.j2)
   and [native unit](../../ansible/templates/russound-web.service.j2) render
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
   them; see the [effective-config idea](./web_idea_effective-config-overrides.md).
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

### Remaining work and disposition

| Item | Next gate | Required evidence or result |
|---|---|---|
| Current configuration matrix and precedence | S0/S1 | Review remaining fields and malformed/empty cases against the agreed runtime precedence; decide necessary exceptions and validate intended Ansible variables, not just the four sampled. |
| Audio logging and Python environments | S2/S3 | Remove import-order-dependent logging, specify reproducible dependencies and rehearse a separately created isolated audio environment; preserve the active venv until a verified, separately approved transition. |
| Earlier registry and proxy work | S2/S4 | Reconcile each feature with pinned images, the outage override, CIDR validation and safe cutover. Add executable regression coverage before reuse. |
| Running Pi image, state, secrets and generated files | S2, before any cutover | Capture an approved private, consistent backup and exact deployed identities; inspect ownership and compatibility, including venv/system dependencies, MPD drop-ins and audio fragments. Do not equate this inventory with a backup. |
| Stable production image pair | S2/S4 | Design production to fail clearly without an approved stable pair; absence is a release blocker, not a reason to block dev-only design. |
| Separate-host authenticated routing, hardware and audio | S5 | Test representative deployments with separately provisioned credentials, TLS and actual peer/CIDR, then controller and playback readiness. Current unauthenticated LAN probe is not a pass for this gate. |
| Migration and rollback | S2/S5 | Rehearse on isolated data, verify version/schema compatibility, and obtain separate Pi maintenance approval. On failure stop and ask, rather than automatically restoring. |

Still open before S0 exit: finish verifying individual Ansible/inventory
override cases, CLI actions, logging and generated-file ownership/restart
effects beyond the examples above; the field tables do not establish that all
deployment paths have equivalent behavior. Verify an authenticated
separate-host web deployment and other supported network paths during S5;
compare installed dependencies and generated audio assets before migration,
and finalize how earlier local work is incorporated. Record
malformed/empty behavior and exceptions for unresolved entries. Compare
remaining guide claims with supported commands and record discrepancies. S0
remains open until those items and the matrix receive review.

</details>

## S1: decisions and approval gate

The operator has agreed the following direction. It is a design contract,
not authorization to deploy or restart services. S1 remains open until the
remaining details, compatibility impacts and outstanding S0 evidence are
reviewed; S2 specifies and rehearses the mechanics.

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
| D9 | Agent updates and credentials | **Agreed:** stage agent changes separately; do not modify active agent files or credentials until explicit activation. Credential rotation is a separate coordinated agent/web operation with visible interruption and recovery. **Remaining:** design atomic activation, token transition, failure handling and tests; current play does not provide this guarantee. |

## Preliminary S2 design: cleanup and deployment

This draft translates agreed directions into implementation work; it does not
close S0/S1 or approve a live cutover. Resolve outstanding compatibility and
configuration cases before finalizing it.

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
| Protocol-audit setting | Saved config and example seed can enable audit without an explicit new-install choice. | New installs default off. On an approved migration, announce and disable existing audit only after backup; later explicit opt-in survives reruns. Test file preservation and migration recovery. |

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

### Implementation blueprint for review

The names and examples below are a proposed implementation contract, **not
commands currently available**. They are intended to give S3/S4 a concrete
starting point, not to close the rehearsal gate or authorize a Pi change.
Existing commands continue to behave as inventoried above until replaced.

#### Entry points and composition

Use the following future repository paths. Keep current playbooks available
until the replacement and migration documentation are tested; a legacy
entry point must not silently forward an incompatible invocation.

| Intent | Future playbook | Ordered Compose inputs | Application source |
|---|---|---|---|
| Development | `ansible/playbooks/deploy-dev.yml` | `compose.dev.yml`, then optional gateway **or** dummy overlay | Hub dev web and managed gateway pins; dummy explicitly built on controller |
| Production | `ansible/playbooks/deploy-release.yml` | `compose.prod.yml`, then optional gateway overlay | Approved stable web and managed gateway pins; currently blocked |
| Development local build | `ansible/playbooks/deploy-dev-build.yml` | `compose.dev.yml`, then optional gateway overlay | Explicit controller-built web and managed gateway artifacts |
| Native web | `ansible/playbooks/deploy-native-web.yml` | None | Identified native web source and isolated web environment; optional native ser2net |

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
4. **Separate finalization:** the controller starts a final target-side
   finalization operation only after the apply invocation has returned
   successfully and all required checks have completed successfully. It
   checks the reservation and pending operation identity, atomically commits
   the verified selection and clears pending, then removes only its own
   reservation as its last target action. No deployment task, handler or
   background work may be queued to run after finalization. Each record and
   reservation update must use appropriate file/directory `fsync` and surface
   errors; test on the supported filesystem.
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
   the matching reservation. If a step fails or remains uncertain, stop,
   report it and keep normal deployment blocked.

The checklist does not promise that every Docker interruption is recoverable
from available evidence; some cases may require further investigation or a
separately approved maintenance procedure. Never equate operator approval
alone with proof that earlier work has stopped.

##### S2 review and rehearsal exit criteria

Review the entry points, input mapping, image selection, reservation, record,
backup and manual recovery sequence. The persistent reservation and separate
finalization are proposed, not proven mechanisms. Before S2 closes, review
their feasibility and unsupported interruption cases; earlier lock probes do
not validate them. S4 must test the actual implementation on disposable Linux:
simultaneous exclusive acquisition across deployment IDs; crashes before and
after reservation creation, pending write, commit and removal; controller loss
during apply and finalization; late finalization response; durability errors;
partial changes; manual recovery; backup/restore; and redaction. Include a
second controller and verify no deployment starts while a reservation remains.
S5 retains separately approved Pi and hardware acceptance.

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
| Agent staged update and token rotation | Active paths unchanged until activation; interrupted activation preserves evidence and prompts recovery, including old/new web and agent token state. |

Run render and record/recovery tests on disposable data first, then an isolated
Linux/Ansible integration environment. Pi/network/hardware acceptance and a
six-hour observation are later, separately approved operations; none is
established by these proposed tests or by a documentation commit.

#### Local rehearsal, 2026-11-13

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
network routing and hardware acceptance remain unverified. Keep S2 open.

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

1. **Discover and preview:** read actual container image IDs, ordered Compose
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
   deployment changes, obtain separate Pi approval for the preview, backup
   method, interruption scope and proposed candidate. If the backup result or
   other material facts differ from what was approved, pause and seek renewed
   approval before continuing. Acquire the host-wide reservation, recheck
   drift and prerequisites, and persist pending before the first disruptive
   action.
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
   observation separately, never as an implied six-hour pass.
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

#### Agent activation and rotation boundary

Stage a candidate agent source tree and bounded-dependency environment in a
separate private path; validate source identity and safe checks without
writing the active installation or token. Explicit activation must report
the agent restart and use the same pending/recovery discipline. A credential
rotation is a distinct coordinated agent/web action: stage protected new
credentials and trust, preview the expected interruption, activate with a
defined order, verify authenticated requests from the actual web host, and
retain the prior protected credentials until an operator-approved recovery or
retirement. Do not promise zero downtime or simultaneous token validity; the
current agent reads one token at startup. Test failures at every transition
and report which side has which **credential generation identifier**, never
the token. Do not alter the currently running agent during design or rehearsal.

### Cleanup sequence

1. Complete the remaining S0/S1 input and compatibility review; keep the Pi's
   currently running image, venvs, data and existing controller work intact.
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

Inventory each actual setting across web, gateway, agent, audio/MPD, proxy and
Ansible/Compose. For each, record owner, type, default, inputs, current and
target precedence, validation, secret handling, restart requirements and
whether it is user-editable. Distinguish:

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
with [inline overrides](./web_idea_effective-config-overrides.md) and the
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

- [ ] **S0 — Complete inventory.** Finish the per-setting matrix and inspect
  the deployed versions and wiring read-only when authorized. Identify
  consumers, documentation discrepancies and the disposition of earlier
  deployment changes. Exit: evidence and unknowns reviewed; no secrets recorded.
- [ ] **S1 — Agree the contract.** Resolve D1–D9 and document approved choices,
  exceptions, compatibility/deprecation timeline and stable-release dependency.
  Exit: the operator agrees to the supported layouts, input precedence, logging,
  image/source selection and migration/rollback scope.
- [ ] **S2 — Design and dry-run.** Specify the two Compose files, four Ansible
  intents and shared tasks; define old-to-new input and path mapping, safety
  preflights, version compatibility and recoverable cutover steps. Produce
  redacted rendered examples and a test matrix. Exit: reviewed design and
  migration/rollback runbook; stable mode explicitly blocked if unavailable.
- [ ] **S3 — Runtime configuration and logging.** Implement the approved
  setting resolution/validation and logging contract with compatibility
  handling and focused tests. Waitress/debug decoupling was implemented early
  at the user's request; per-component logging details still need design.
  Exit: precedence, invalid values, redaction and regression tests
  pass; API changes coordinated with the effective-config idea.
- [ ] **S4 — Compose and Ansible.** Implement supported profiles/entry points
  and shared tasks, preflight and non-destructive migration controls. Test
  rendering, source/pull behavior, idempotence and failure paths without a live
  cutover. Exit: local/CI tests and Ansible checks pass for available modes.
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

## Acceptance and verification

- Render Compose configurations for dev Hub, dev local build and production;
  assert expected images, logging, mounts, secrets, networking and no
  unintended builds. Missing production release must fail, not use dev.
- Test each Ansible entry point and repeated runs, shared task behavior,
  missing prerequisites and failure rollback; preserve unrelated services and
  dirty checkouts. Exercise native, Docker, external gateway and dummy modes
  according to the final support decision.
- For every comparable setting, test CLI/environment/config/default conflicts,
  blank and invalid values, source reporting, save/reload and existing
  configuration migration. Verify Ansible-generated values actually reach the
  intended container or service.
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

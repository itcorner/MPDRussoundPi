# Container deployment

This is the authoritative guide for the new container layouts. Use them for
**new, isolated development deployments** only. The existing Pi must not be
migrated by running these commands: automated backup, adoption, apply and
recovery are not integrated yet. Persistent reservation/record primitives,
protected-input snapshots and completion-evidence validation have local tests.
Private stopped-writer file backup and isolated restore are implemented and
have a disposable container recovery rehearsal.
An explicit read-only first-install target preflight is available; none of
these enables apply.
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
| Development, no hardware | `compose.dev.yml`, `compose.dummy.yml` | Published web digest and explicitly built local dummy |
| Production | `compose.prod.yml`, optional `compose.gateway.yml` | Layout only; blocked until approved stable images and deployment tooling exist |

Include **one gateway overlay at most**. External mode has no gateway service,
device requirement or hidden dependency. The backend containers are internal
to the project network: port 6666 is not published. Web defaults to host port
8000. Different projects have separate networks and generated container names;
they must also have separate data directories and non-conflicting host ports.

None of these files contains `build`. Starting containers never implicitly
builds an application image. The development profile enables debug logging;
the production layout disables it. Every container rotates Docker logs at
10 MiB, three files. This is not yet the complete cross-component logging
contract.

### Runtime shutdown and native setup logging

Current web source handles SIGTERM/SIGINT, including as container PID 1.
It rejects new work, drains SSE/request workers and closes background polling
and gateway connections. The dev/prod layouts allow a 30-second web stop grace
period. Shutdown timeouts fail explicitly; always verify the actual writer
state before capturing data. This source fix is not present in older published
images merely because the Compose layout was updated. The approved Pi test
used older pinned images and still observed forced web termination and a
nonzero ser2net stop; those live images have not been replaced by this change.

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
New files are private and never overwrite an existing selection.
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

Image selection has three mutually exclusive paths:

| Inputs | Behavior |
|---|---|
| `russound_image_selection_file` | Offline candidate validation and reuse; no registry calls |
| No selection file or image references | Fresh verified registry metadata resolution for this preview; candidate is **not saved** |
| Explicit `russound_web_image` and, for managed mode, `russound_ser2net_image` | Advanced offline, digest-syntax-only rendering; **no identity verification** |

Do not combine a selection file with explicit images or provide only half a
managed pair. The preview reports which verification path it used.
The controller Python running Ansible invokes the standard-library resolver;
Docker Compose must also be installed on that controller.
The initial preview does not support playback agents or legacy source/mode
switches. Preview does not pull images or verify target permissions, installed
images, hardware or deployment safety.
`russound_action=apply` fails explicitly. Do not interpret a successful preview
as authorization or evidence of safe deployment.

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
initialize, apply or finalize command. The only CLI action is read-only status;
the fixed root-owned state directory is not automatically provisioned.

The internal [completion validator](../../tool/deployment_completion.py) runs
Ansible with a controlled notification callback and verifies private, allowlisted
events after process exit. It requires the exact expected task set, a complete
terminal record, zero failures/rescues/ignored errors and a separate successful
callback acknowledgement after syncing the evidence. It does not expose task
names, result payloads, command arguments or loop values. It neither applies a
deployment by itself nor dispatches finalization.

This first validator accepts only unconditional, synchronous required tasks;
required handlers must actually run. Loops, retries, asynchronous work,
conditional required tasks, skips and missing handlers block completion rather
than receiving guessed outcomes. These restrictions apply to the unfinished
runner, not to legacy playbooks. Support for richer patterns remains gated on
their event mapping and tests.

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
approval-seal custody across runner invocations still needs integration.

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
Any future live restore still requires separate approval, writer/daemon
quiescence, code/data compatibility checks and accounting for post-backup edits.

Current boundaries:

- Linux extended-attribute inspection is required. ACLs, extended attributes,
  filesystem flags, symlinks, hardlinks, special files, special permission bits,
  shared-writable paths and unsupported ownership fail explicitly rather than
  being silently lost. Source objects must belong to root or the capture
  account; ancestor ownership follows the same safe-path policy as state.
  Mixed-service ownership may require separate capture scopes and privileged
  restoration; it is not automatically adopted.
- At most 64 selected roots, 10,000 entries, depth 64 and 8 GiB total contents;
  the private manifest is bounded to 8 MiB. Sparse files are stored as bytes,
  without preserving sparse allocation.
- SQLite is copied with **all writers stopped**, including any WAL/SHM files.
  Tests recover committed WAL content after unclean process exit. This is not
  an online SQLite backup or application/schema compatibility certification.
- An image archive can be captured as a regular file, but automatic
  `docker image save/load`, image-identity verification and off-host transfer
  orchestration are not implemented. A copy/reload fixture proves file-bundle
  verification only, not a restorable container image.
- No backup/restore operator CLI or live restoration action is exposed.

Local tests cover crashes/concurrency, persistence failures, successful and
failed handlers, ignored/rescued failures and unreachable targets. They do
not establish live recovery, daemon quiescence, target filesystem/reboot
durability or real target readiness. The opt-in container recovery test uses a
unique local Compose project and Linux data volume: it runs current web/dummy
source in an already cached Python-capable image and stops its own writers,
backs them up, observes a real failed Ansible image update, restores a new tree,
checks restored web-to-dummy routing and unchanged rerun, and confirms pending
remains blocked. Its synthetic unavailable candidate is deliberate fault
injection, not successful registry approval or adoption.
Remaining work includes recorded-installation
preflight and full runtime access/config checks, protected-snapshot orchestration,
service-aware backup/image/off-host orchestration, actual apply and independent checks, then integration with these
state/completion primitives and manual recovery.
Do not use the internal Python APIs as a deployment shortcut.

## Existing installations and legacy entry points

The old `docker-compose.yml`, `docker-compose.dummy.yml`,
`deploy-russound-docker.yml` and `deploy-russound-web.yml` remain for compatibility;
they are **not aliases** for the new files. Do not mix their variables,
state paths or ordered file lists with the new workflow. Legacy Docker
provisioning can build on the target and stop native services; play-level
defaults also override inventory. Native web still uses a separate venv and
persistent native config/state and optionally manages native ser2net.

The Pi's extra Compose override and local-only web image need explicit
preservation. Continue to treat adoption as a separately approved migration.
The [implementation plan](../plans/general_open_simplify-deployment-configuration.md)
records remaining safety, native/local-build intents and integration gates.

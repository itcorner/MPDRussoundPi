# MPD integration with the Russound web interface

Date: 2026-10-14; deployment status updated 2026-10-17. Solution D is selected;
the read-only agent, production identities, Pi TLS proxy and Docker web status
integration are deployed. S07 manual playback and calibration-gain controls
are done, deployed and production-verified by the user on 2026-10-17.
Hardware-driven policy remains planned.

S08 managed station selection and S09 TuneIn search/resolution are implemented
and active in the approved trusted-LAN Pi test deployment. D09-D12 are accepted:
no user-entered URLs, explicit one-source queue management, retain saved
stations for supervisor retry, and selecting a station always starts playback.
TuneIn station selection is enabled by default on agents with manual
operations; `--disable-tunein-selection` turns it off. Pi-side public-only
egress enforcement now covers resolved destinations and redirects through
connection-pinned Python fetching and an MPD-only cgroup packet filter; see the
[egress plan and evidence](./playback-egress-hardening-plan.md). Live deployment
is not full production/lifecycle sign-off.

Implementation progress: S01-S07 are complete, including the
[production Pi-to-Docker rollout](./playback-deployment-evidence.md#production-read-only-rollout-2026-10-17).
S08/S09 and the [configuration split](./config-split-plan.md) are deployed;
remaining live checks and hardening are recorded in the
[rollout record](./playback-deployment-evidence.md#configuration-split-rollout-2026-10-17).
See the [implementation plan](./mpd-web-integration-implementation-plan.md)
for remaining decisions and [collector usage](./mpd-backend-api.md).

## 1. Selected architecture

The user selected solution D. It uses a small **Pi-local playback agent**
exposing a versioned HTTP/JSON API, with the existing Flask web server as the
browser's same-origin gateway.
The agent wraps and extends the existing Python MPD backend; it does not
replace the existing tested route/gain helpers or add a competing recovery loop.

Keep MPD, PipeWire, WirePlumber and the playback supervisor on the audio host.
The web application may run in Docker on another host, natively on the Pi, or
in Docker on the Pi. Use the same agent API in every deployment.

The read-only status, manual playback and volume steps are complete. S08/S09
deliver individual station replacement using TuneIn and saved favorites. Introduce
hardware-driven automation last, as an explicit opt-in.
Do not deploy or mutate the production audio configuration while the current
endurance test is running.

See the [stepwise implementation plan and decision register](./mpd-web-integration-implementation-plan.md)
for dependencies, acceptance gates and open decisions to resolve one at a time.
Architecture selection is not authorization to implement all phases. All new
endpoints, schemas, modules and timing values below remain proposed until agreed.

Accepted availability decision (D01): the web host owns hardware-demand policy.
During a web-host outage the Pi preserves/reconciles its last playback intention,
but does not make new hardware-driven start/stop decisions.

Accepted S08 source behavior: the browser submits TuneIn station IDs or saved
favorite keys, never arbitrary URLs. Admin configuration may seed URL favorites.
Resolved feeds must be direct HTTP(S) URLs without embedded user-info or
fragments; retain query values for tokenized feeds but
never echo full URLs in status or errors. Each output may adopt an empty queue
or a single song matching its prior configured source. Thereafter, remove only
the tracked MPD song ID and refuse cleanup if the queue contains unowned items.
Save the new desired URL before applying it so the existing supervisor can
retry after failure. A source selection changes that output's intention to
`play` and starts it; there is no silent-selection action. Show desired and
observed current source separately using redacted display values.

## 2. Requirements and terminology

| Requirement | Architectural consequence |
| --- | --- |
| Web and MPD may run on different hosts | A network boundary independent of Python imports and shared files |
| Live running state and now-playing information | Observe MPD state and dynamic metadata, not just configured station labels |
| Independent stream replacement | Stable output identity independent of selected station, URL and display name |
| Admin-configured URL stations and TuneIn | Saved favorite keys or validated TuneIn IDs; Pi-side stream resolution |
| Web-adjustable stream volume | Directly edit the persisted output calibration gain; keep Russound zone volume separate |
| Start/stop from hardware state | A headless policy controller consuming fresh power/source observations |
| Recovery and maintainability | Reuse local backend semantics, explicit ownership, bounded failures and contract tests |

An **output** is a stable playback channel backed by one MPD instance and one
exclusive virtual sink. A **source selection** is the station/feed assigned to
that output. A **Russound source** is the physical input numbered 1 through 6.
A **zone** is addressed by `(controller, zone)`, not by display name.

Changing the station on Stereo CenterLFE must not change the output's MPD port, sink,
systemd unit or physical input mapping. No transcoding or audio relay through
the web container is required: the Pi fetches and decodes the stream itself.

## 3. Existing interfaces and integration gaps

The following facts were checked in repository source. The
[Pi insights](./musicpi-insights-2026-10-11.md) supply deployment context, not
proof that the proposed integration already exists.

### 3.1 Interface inventory

| Existing interface | Current behavior | Reuse / required extension |
| --- | --- | --- |
| [Web server](../web/russound_server.py): `GET /api/state` | Returns a published/cached Russound view; does not wait for a busy hardware lock | Preserve fast reads; add a separately cached playback view |
| `GET /api/events` | SSE `state-change` with revision **and full view payload**, keepalives and reconnect support | Keep one browser stream; add a distinct playback event without breaking existing clients |
| `POST /api/system/power`, `/api/source`, zone power/source/volume, shortcuts | Hardware commands publish state; keypad updates also refresh state | Feed policy from resulting hardware observations, not only HTTP command handlers |
| [Controller](../web/russound_controller.py): `backend_health_snapshot()` | Cached `connected`/`message`; at least one responding zone can mark the backend connected | Add per-zone successful-read timestamps and explicit unknown/partial health |
| [State](../web/russound_state.py): `sync_system_power()` | `system_power = any(zone.power)` across represented zones | This is aggregate zone demand, not a measurement of controller mains/standby power |
| [MPDBackend](../mpdrussoundpi/russound/lib/mpdbackend.py): `list_outputs()` | Immutable settings: output ID, label, port, sink, runtime gain, selected station, intention | Inventory keyed by output ID (after the [configuration split](./config-split-plan.md)) |
| `status(name=None)` | State, configured gain and expected sink only; sequential, first error aborts | Add per-output detailed observations and failure isolation; metadata is missing today |
| `start`, `pause`, `stop` | Persist intention before MPD commands; use the common mutation lock | Reuse for manual control; report requested versus actually applied state separately |
| `set_gain`, `check_route`, `recover_route` | Pi-local `pactl`, route verification and atomic gain persistence | Keep on the audio host; expose measured versus configured values honestly |
| [Supervisor](../mpdrussoundpi/russound/mpdsupervisor.py) | Starts local daemon units and reconciles saved intentions/URL/gain, with backoff | Remains the only continuous playback recovery loop |
| [CLI](../mpdrussoundpi/russound/mpdctrl.py) | Delegates to `MPDBackend`, exact stream names | Keep a first-class cooperating control path, including future policy override semantics |
| [Audio readiness](../mpdrussoundpi/russound/lib/audioready.py) | Read-only inspection is available; waiting for readiness can enforce the card profile | Status uses inspection only; never run enforcing readiness from a GET request |
| [Recorder](../mpdrussoundpi/russound/playbackaudit.py) | Records status/queue/route/gain/signal without controlling playback | Reuse observation concepts, not its audit process or FFmpeg probes as a UI server |
| Operational config and playback intentions | Pi-local JSON; config reload per operation; atomic cooperating writes | Agent/CLI/supervisor share these authoritative files; web must not maintain a second copy |

The current Pi layout is output `stereo_front` (Stereo Front) on port 6601,
`stereo_centerlfe` (Stereo CenterLFE) on 6602 and `stereo_rear` (Stereo Rear)
on 6603; each plays a selectable station. Their physical Russound input numbers
are **not established by those MPD ports or sink names** and must be
configured/verified explicitly.

See the existing [Python backend contract](./mpd-backend-api.md),
[web API](../web/API.md) and [headless deployment](./mpd-headless-deployment.md).
Source is authoritative where old web API prose conflicts: API-token wording
is obsolete; the current code uses origin/content-type/host protections, not
user authentication. The current SSE implementation includes the view payload.

### 3.2 Constraints that affect the architecture

- `MPDBackend` connects to **localhost**, calls local `pactl` helpers and uses a
  per-user kernel lock. Pointing it at a remote host would not make its audio
  operations remote. Sharing its files with the Docker container is not enough.
- Its existing `status()` result does not include `currentsong`, queue length, elapsed time,
  MPD errors, intention or daemon status. The older
  [MPDInstance](../mpdrussoundpi/russound/lib/mpdinstance.py) logs a title but
  returns only a state string; it is not the integration boundary. S02 adds a
  separate read-only collector supplying these fields without changing `status()`.
- There is no supported runtime source-change method today. Changing a raw MPD
  queue outside the backend can be undone by the supervisor restoring the
  configured URL. Existing starts preserve unrelated queue entries; arbitrary
  source switching therefore needs a deliberate queue-ownership policy.
- Names currently identify configuration entries, saved intentions and derived
  systemd units. Renaming an output is not just editing a label.
- The supervisor restores software mixer volume to 100. A web slider issuing
  raw MPD `setvol` would be overwritten by reconciliation and bypass startup
  gating semantics.
- Full hardware polling currently runs when there are SSE clients or no
  recorded health result. Unsolicited/keypad updates still run, but an unattended
  automation controller cannot assume complete fresh state without changing
  this polling condition.
- Zone reads can fail individually while persisted values remain in the view.
  A healthy gateway/banner and an SSE revision do not prove every relevant
  zone was just measured. Freshness must come from successful observations.
- The web image currently copies only `web/`; its dependencies are Flask and
  Waitress. It intentionally excludes the MPD/audio tree. Avoid importing
  Pi-local audio code into that image.
- Generated MPD configs always bind `127.0.0.1` and have no MPD password;
  only local processes (agent, supervisor, audit, CLI) connect. Remote MPD
  clients are intentionally unsupported. Existing installs must rerun
  `mpdsetup.py --install` and restart MPD to drop the earlier all-interface
  binding.

## 4. Architecture choices

### 4.1 Comparison

| Choice | Split deployment | Reuses local safety/recovery | Live metadata | Operational burden | Maintainability |
| --- | --- | --- | --- | --- | --- |
| A. Co-located web + Python backend | Poor without adding another transport | Yes, if same user/runtime | Local polling or MPD `idle` | Low natively; high with audio-container mounts | Simple for one host, tightly coupled to audio environment |
| B. Web directly talks MPD TCP | Yes | Not for mutation via raw protocol | Good | Low initial setup, more exposed ports | Cheap read-only prototype; substantial duplicated control logic later |
| C. Web controls CLI over SSH | Yes | Yes for supported commands | Polling; current CLI has no structured metadata | Keys, host identity, remote env, process management | Useful maintenance bridge, brittle product API |
| D. Pi-local HTTP agent + web gateway | Yes | Yes | Polling first; local `idle` later | One additional small service per audio host | Best fit: explicit contract, reusable domain logic, isolated deployment |
| E. Pi agent + MQTT/message broker | Yes, including outbound-only Pi networking | Yes | Publish/subscribe telemetry | Agent, broker, ACLs, delivery/expiry rules | Good for larger integrations; excess moving parts for this installation |

### A. Co-located application

The Flask server can call the Python backend when it runs as the audio-owning
user on the Pi with the correct runtime environment. This minimizes networking
and serialization code and is suitable for a strictly single-host system.

However, mounting Pulse/PipeWire sockets, D-Bus, persistent configuration and
matching user IDs into a web container couples browser service deployment to
host audio privileges. Moving the UI off the Pi later requires a new boundary.
Do not implement a second in-process control path merely to optimize
co-location: the recommended agent remains useful over loopback.

### B. Direct MPD protocol from the web server

`status`, `currentsong`, and `idle` provide state and changing stream tags.
This is a reasonable isolated read-only prototype with restricted connectivity.
The browser cannot speak MPD's raw TCP protocol; a server adapter is still needed.

For mutations, direct `play`, `stop`, `addid` or `setvol` bypass intentions,
route/gain verification and persistent configuration. Recovery can undo them.
Adding remote shell/pactl operations to repair that gap combines two control
planes and duplicates the backend. Raw MPD also has no knowledge of calibration,
hardware automation modes or catalog selection provenance. Not recommended as
the final write architecture.

### C. SSH command bridge

An allowlisted SSH command can invoke the existing CLI in the Pi's environment,
preserving its supported safety semantics. This avoids opening an agent port.

It requires safe argument handling, strict host-key checks, restricted keys,
explicit user runtime variables, timeouts and structured output. The existing
CLI status text is not a versioned metadata API; spawning commands for every
poll is expensive and disconnects leave command completion ambiguous.
Source switching still needs a backend extension; stream volume can wrap the
existing `set_gain` operation. Once a
structured remote worker and event transport are added, this effectively becomes
a more awkward agent. Retain SSH for administration, not browser requests.

### D. Local HTTP/JSON playback agent (recommended)

The audio-owning user runs a small service wrapping `MPDBackend`, observations
and an operation worker. Flask/Waitress can be reused rather than introducing
an unrelated web framework. HTTP is easy to inspect, proxy, secure and test;
the frontend does not depend on Pi paths, systemd or MPD versions.

Costs are API versioning, credentials/TLS, caching, a new unit and distributed
failure semantics. These are bounded and necessary for a durable split deployment.
Keep transport thin: no copies of playback algorithms in HTTP handlers.

### E. Broker-based control and telemetry

A Pi agent publishes observations to MQTT and consumes explicit desired-state
commands; the web subscribes through a broker adapter. Useful when several Pis,
home automation consumers or outbound-only network paths are real requirements.

A broker does not remove the need for local backend extensions. Retained
commands can replay obsolete play requests; QoS duplicates require idempotency;
retained telemetry can look fresh after a disconnect. Correlation IDs, command
expiry, revisions, presence and ACLs become mandatory. Start with HTTP; add a
broker transport only when its deployment benefits justify those costs.

## 5. Recommended deployment and ownership

```text
Browser
  | same-origin HTTP + one SSE connection
  v
Russound web application (Docker host or Pi)
  |-- existing Russound adapter --> ser2net/RNET --> hardware
  |-- hardware-demand policy (optional, headless)
  |-- playback client/cache
  |
  | authenticated HTTP/JSON, bounded requests
  v
Playback agent (audio-owning user on Pi)
  |-- read-only MPD/audio observer --> local MPD instances
  |-- bounded operation worker ----> MPDBackend
  |                                   |-- config/intentions
  |                                   |-- route/gain helpers
  |-- accepted policy target -------->|-- shared mutation lock
                                      ^
                                      |
                       existing playback supervisor
                                      |
                             MPD --> PipeWire --> USB audio
```

The web policy decides **whether an output is demanded by hardware**. The Pi
backend owns the persisted effective intention and local application of it.
The supervisor restores that intention; it never independently evaluates
Russound power. The policy evaluator must not become another playback repair loop.

| Data / responsibility | Authoritative owner |
| --- | --- |
| Russound observations, zones and source IDs | Existing web hardware controller |
| Agent endpoint registry, physical-input/output mapping, automation rules | Web configuration, persistent Docker config storage |
| Output identity, selected playable source, output calibration gain | Pi operational config, written by cooperating backend operations |
| Manual/automatic mode and effective playback intention | Pi control state shared by agent, CLI and supervisor |
| Actual state and metadata | Local MPD; observed and cached by the agent |
| Routes, audio readiness and measured mixer gain | Pi-local audio helpers |
| Catalog/provider credentials | Server-side provider configuration/secret storage |
| Web/agent snapshots | Disposable caches, never sources of desired state |

No NFS/shared-volume edits of the Pi config from Docker, Docker socket access,
remote `pactl`, or systemd restarts per browser command.

### Deployment variants

- **Current split:** Docker web calls the Pi agent over a restricted LAN API;
  it continues using the existing network Russound gateway.
- **Native all-on-Pi:** web calls the same API over loopback; agent runs under
  the audio user. Other components retain their existing lifecycle.
- **Docker web on Pi:** only the web-to-agent endpoint changes. Use an explicit
  reachable host address/gateway and bind/firewall appropriately; container
  loopback is not host loopback.
- **Several audio hosts:** endpoint registry maps each output to a host ID and
  output ID. Status failures remain isolated per host/output. Do not build
  discovery, clustering or brokers before they are needed.

Initially support one active web/policy controller per Russound installation
and one agent per audio host. The existing web hardware connection/state/event
model is not a multi-worker distributed architecture. Scaling requires a single
hardware/policy owner and shared events, not just more web processes.

## 6. Proposed network contracts

### 6.1 Agent API

Use an `/v1` boundary and advertise schema/capabilities. S01 freezes initial
read routes, fields and error/freshness semantics in the
[Playback read API v1 contract](./mpd-playback-read-api.md). That contract and its
fixtures take precedence over the longer-term illustrative snapshot below.
S07 manual write endpoints are defined in the
[operations contract](./mpd-playback-operations-api.md); later source,
automation and provider fields below remain proposed.

| Endpoint | Purpose / semantics |
| --- | --- |
| `GET /v1/health` | Agent process liveness; audio unavailability is separate |
| `GET /v1/capabilities` | Agent identity, schema version, supported operations and volume/source limits |
| `GET /v1/outputs` | Cached inventory and per-output snapshots, including partial failures |
| `GET /v1/outputs/{id}` | One cached snapshot, freshness and last known good observation |
| `POST /v1/outputs/{id}/operations` | Explicit play/pause/stop, output calibration gain, source selection or control-mode change |
| `GET /v1/operations/{operation_id}` | Accepted/running/applied/failed/expired operation, persistence and actual outcome |
| `PUT /v1/outputs/{id}/automatic-target` | Later: sequenced hardware-demand target and validity lease, not a manual override |
| `GET /v1/events` | Optional later optimization: invalidation notifications; snapshots remain authoritative |

The web exposes same-origin `/api/playback/*` routes forwarding typed,
allowlisted operations, not a generic proxy to arbitrary agent URLs/paths.
Provider search/resolution can later use `/api/stream-catalogs/*`; catalog
routes do not exist today.

Example cached output:

```json
{
  "agent_id": "musicpi",
  "epoch": "agent-start-identifier",
  "revision": 42,
  "output": {
    "id": "stereo_front",
    "label": "Stereo Front",
    "control": {
      "mode": "manual",
      "desired_state": "play",
      "reason": "manual",
      "pending_operation_id": null
    },
    "selection": {
      "kind": "catalog",
      "provider_id": "radio-directory",
      "item_id": "station-886",
      "label": "Radio 88.6"
    },
    "observation": {
      "observed_at": "2026-10-14T17:30:00Z",
      "stale": false,
      "mpd_reachable": true,
      "state": "play",
      "queue_entries": 1,
      "elapsed_seconds": 1234.5,
      "now_playing": {
        "title": "Example track",
        "artist": null,
        "station": "Radio 88.6",
        "metadata_available": true
      },
      "audio_ready": true,
      "route": {"expected": "sink.stereo_front", "actual": "sink.stereo_front"},
      "gain": {
        "configured_db": -7.1,
        "measured_units": [49905, 49905],
        "measured_at": "2026-10-14T17:29:58Z"
      },
      "error": null
    },
    "config_revision": 7
  }
}
```

An unreachable MPD yields `mpd_reachable: false`, current `state: null`, an
explicit error and a separately timestamped last-known observation. It must not
look stopped, healthy or successfully empty. Metadata is optional: retain
missing fields as null, render station/selection label as a clearly labeled
fallback, and never invent a current track. Hide/redact credential-bearing URLs.

### 6.2 Operations and distributed failure semantics

- Browser mutations become short HTTP submissions, returning `202` plus an
  operation ID once accepted. Acceptance is not successful playback. Reject
  invalid requests before queuing; use explicit terminal results and UI banners.
- Use bounded queue depth, request size and operation deadlines. A single
  agent mutation worker is adequate for three outputs; shared backend locking
  still arbitrates with the CLI/supervisor. No network I/O under the web's
  Russound `state_lock`, and no blocking audio work in SSE handlers.
- Require idempotency keys for retries, scoped to caller/output/action. Persist
  a small bounded operation ledger on the Pi so a lost HTTP response/restart
  can be queried without blindly replaying commands. SQLite is a suitable
  standard-library choice for that ledger; operational config/intentions remain
  backend-owned. Record application phases/revisions because a ledger transaction
  cannot atomically commit MPD commands and JSON files. Same key/different payload
  is a conflict; document expiry, after which the outcome may be unknown.
- Carry expected config/control revisions for edits. Reject stale source/volume
  edits with `409`; cancel/supersede queued targets that no longer match revisions.
  Do not retry a manual operation after a newer manual stop or source change.
- Surface typed errors: invalid input `400`, unauthorized `401/403`, missing
  output `404`, revision/lock conflict `409`, capacity `429`, agent unavailable
  `503`. Terminal operation errors distinguish MPD, audio, persistence and
  rollback failures. Add a typed backend-busy error rather than parsing text
  from today's generic `BackendError`.
- Separate `requested_intention_persisted`, `applied` and observed state.
  Existing start/stop failures can still persist an intention that the
  supervisor later fulfills. An HTTP failure or timeout is not cancellation.
  Cancellation after persistence needs a new explicit compensating intention.
- Persisted accepted intent after a crash can be reconciled, but completion
  must be re-observed. Never mark an interrupted operation applied solely
  because it was queued or its intent was saved.
- Initially mutate one output per operation. Future bulk actions return
  per-output outcomes; neither host-wide nor hardware-plus-MPD changes are
  distributed transactions. Avoid claiming atomic all-stream rollback.

Keep separate config, control and observation revisions. Observation changes
must not constantly invalidate a volume edit. Pair observation revisions with
an agent epoch so reconnecting clients recognize a revision reset.

## 7. Live status and now-playing delivery

1. Extend the backend's read-only observation surface using MPD `status` and
   `currentsong`: playback state/error, song ID, queue size, elapsed, bitrate,
   audio format and available title/artist/album/station tags.
2. Snapshot each output independently; one dead MPD must not hide the other two.
   MPD clients are connection-local, not shared concurrently between requests.
   Check song IDs around metadata reads and retry a bounded inconsistent
   snapshot instead of attaching an old title to a newly selected station.
3. Initial implementation: agent polls local MPD approximately every 2 seconds;
   web polls agent snapshots approximately every 2 seconds. Display freshness,
   with a proposed stale threshold of 10 seconds. Bound failed-output work so
   its timeout does not delay healthy outputs.
4. Later optimization: one dedicated MPD `idle player playlist mixer output`
   observer per instance, separate from command connections. `player` includes
   changing stream tags. Re-read snapshots on notification/reconnection and
   retain periodic health resync; `idle` is not a durable cross-restart event log.
5. Cache MPD data and slower audio-health checks independently (for example
   10-second route/gain/readiness checks), each with its own timestamp.
   Do not capture FFmpeg audio or run calibration on every web refresh.
6. Web caches playback separately and emits `playback-change` on the existing
   browser SSE connection. The frontend handles it and refetches the playback
   snapshot; retain existing hardware `state-change` behavior. On every
   reconnect fetch both snapshots, and retain visible-page polling fallback.

The existing SSE service uses finite-lived connections, worker limits and
per-client queues. Reuse those protections; coalesce repeated invalidations
and bound slow-client queues when adding playback events. Send metadata/state
changes, not a new event for every elapsed-time tick; interpolate elapsed
display from a timestamp and periodically resync.

Proposed acceptance target under a healthy LAN: state/title changes visible
within 5 seconds at p95, stale state labeled within 10 seconds. These are
implementation test targets, not measured current guarantees.

The [MPD protocol reference](https://mpd.readthedocs.io/en/stable/protocol.html)
documents `status`, `currentsong`, `idle`, optional values and song-ID commands.
Tags depend on broadcaster, protocol and MPD plugin; not every URL supplies a
track title, artist or useful duration.

## 8. Source selection and providers

### 8.1 Stable output identity

Accepted D08: persist an immutable output ID plus a separate editable label, preserving existing
ports, sinks and unit names during migration. Initially map proposed IDs such
as `front`, `centerlfe`, `rear` to existing exact-name entries; do not rename
existing entries to station names supplied by a catalog.

Amended 2026-10-17: because outputs now switch stations, station-named outputs
were renamed to their audio channel. A one-off `outputrename.py` tool
derived the stream name, output ID and label from the configured sink (for
example `stereo_front` / `Stereo Front`) and renames intention keys. It is an
offline maintenance step: stop the agent, supervisor and MPD units, run it,
remove the old `mpd_*` units/configs and regenerate them with
`mpdsetup.py --install`. Ports, sinks, gain and selected sources are unchanged;
historical operations keep their former output IDs. The dashboard shows the
selected station (TuneIn name, else MPD's ICY station name) next to the state. Superseded by the [configuration split](./config-split-plan.md): the tool was removed, outputs are now keyed by `id` in `config.json` and runtime state lives in `playback.json`.

A single-host stable ID is sufficient initially; multi-host identity is
`(agent_id, output_id)`. Selection edits never regenerate systemd units/audio.
Structural output changes remain separate deployment/maintenance operations.

### 8.2 Normalized selection contract

The deployed contract uses `set_tunein_station` with `station_id` and
`station_name`, or `select_favorite` with a saved station `key`; see the
[operation API](./mpd-playback-operations-api.md). TuneIn search runs in the
web gateway; stream resolution runs on the Pi, not in the browser or web host.
Favorites are capped at 20 and persisted with runtime state in `playback.json`.
Admin station seeds live in `config.json`; runtime edits do not change them.

Selection-specific fields (full requests also require expected config/control
revisions and an idempotency key):

```json
{"action": "set_tunein_station", "station_id": "s8007", "station_name": "ORF Hitradio OE3"}
```

```json
{"action": "select_favorite", "key": "tunein:s8007"}
```

Search returns normalized station identities and bounded display metadata.
The Pi resolves the selected station ID and persists its provenance and resolved
URL privately; the web does not send a resolved stream URL. A URL favorite
uses the same key-based selection operation without a catalog lookup.

Future providers should reuse these boundaries rather than implement MPD control.
Use timeouts, cache/rate limits and visible provider failures. An unavailable
catalog must not stop an already-running stream. Keep provider credentials out
of browser payloads and logs.

Direct HTTP(S) internet radio is the initial playback scope. TuneIn's bounded
M3U/PLS resolver extracts a direct stream before handing it to MPD. Arbitrary
playlist entry, authentication, expiring URLs, local libraries, subscription
services, DRM, Spotify-like integrations and casting are not interchangeable
with an MPD-readable URL. Advertise supported capabilities and reject unsupported
items rather than pretending they are playable. A provider with expiring URLs
later needs an explicit refresh resolver/deployment design; persisting the
resolved URL alone cannot guarantee autonomous recovery.

### 8.3 Individual source-change semantics

Implement a backend operation, not raw queue edits from Flask:

- Validate selection and supported URI policy, acquire the shared control
  lock, check revisions and preserve unrelated operational config fields.
- Persist the selected source atomically so the supervisor cannot restore the
  old URL. Report desired source separately from confirmed current playback.
  A successfully saved but unreachable source is a failed/degraded application,
  not a successfully playing station.
- Per D12, **selection always sets the play intention and starts playback**,
  including on paused/stopped outputs. Reuse the existing mute/route/gain/unmute
  sequence; there is no silent-selection action. Persist the station, resolved
  URL, managed song ID and play intention atomically in `playback.json`.
- For dedicated radio outputs, introduce an explicit managed-queue mode.
  Keep one backend-owned selected URI after a successful switch, using song
  IDs and bounded cleanup. Unknown/pre-existing queues require explicit
  adoption; do not silently clear them. Repeated selection must not accumulate
  stale station URLs or duplicate entries.
- Keep failures explicit. Leave the new persisted selection visible and use
  normal reconciliation retries, bounded by existing backoff. Restoring an old
  selection is another revision-checked operation, not an unlogged fallback.

Tests must cover save failure, URL failure, concurrent stop, queue cleanup
failure and restart mid-switch. Document partial desired/live state because
atomic file replacement does not make MPD and config one transaction.

## 9. Volume architecture

D13 (accepted 2026-10-17) sets the calibration-gain slider range to -60 to
0 dB with 0.1 dB steps. The minimum extends to include existing gains below
-60 dB without clamping. Each stream card's Config dialog holds play/pause/stop
and the slider, with a number + dB readout and no text/numeric gain input.
This edits the existing
`gain_db`, not a separate attenuation layer.

**User stream volume is the output gain calibration itself**, not an additional
attenuation layer. A web edit replaces the same persisted `gain_db` used by the
backend, CLI and supervisor. There is no separate user-volume field, offset or
combined-gain calculation.

There are two distinct controls:

| Control | Scope | Proposed behavior |
| --- | --- | --- |
| Existing Russound zone volume | One zone/amplifier | Keep existing zone routes and UI unchanged |
| Stream volume / output calibration gain | One Pi output, both channels and all zones using that source | Directly edit persisted `gain_db` through existing `MPDBackend.set_gain` |

Expose `set_gain` through the agent's typed operation contract. It already
validates finite nonpositive dB values, recovers/verifies the playing route,
applies absolute sink gain with read-back verification and atomically saves
the calibration. Existing start/reconcile operations reapply that same value;
no second volume mechanism or supervisor gain-composition change is needed.
Keep MPD's software mixer reserved for startup gating (0 while verifying,
100 after success). Raw MPD `setvol` is not an acceptable shortcut.

Label the UI control **Stream volume (output gain)** and display dB so users
know edits change calibration. Use the accepted slider-only Config dialog; define and
advertise its finite attenuation range and step before UI implementation.
The current backend permits attenuation only (`gain_db <= 0`); 0 dB is unity.
Do not imply a separate mute capability or encode `-Infinity` as JSON.
Stop/pause remain explicit playback actions, distinct from gain adjustment.

Volume edits while stopped/paused persist without starting playback; measured
inactive mixer gain may not match until next start. Debounce slider commits
and supersede unsent updates; do not fsync configuration for every pointer
event. All volume writers cooperate with the shared lock and atomic persistence.

No automatic link from zone-volume changes to stream calibration gain: a source
can serve several zones, and changing one shared stream gain changes all of them.
Calibration after switching stations may no longer equalize loudness; display
the retained gain and allow deliberate recalibration later. No continuous
loudness normalization is implied.

## 10. Hardware-driven start/stop policy

### 10.1 Demand choices

| Rule | Benefit | Limitation |
| --- | --- | --- |
| Any represented zone on starts all managed outputs | Simple system-wide behavior | Streams run even when no zone selects them |
| Any powered zone selects this output's mapped physical source | Independent resource usage, matches actual listening | Requires accurate source mappings and per-zone freshness |
| Explicit configured zone group demands an output | Useful for unusual wiring/use cases | More configuration and validation; not needed initially |

Recommend source-demand automation with an optional simple system-wide rule:

```text
demand(output) = any(fresh zone where
                    zone.power and zone.source in output.mapped_source_ids)
```

For this rule, if any fresh relevant zone positively demands the output, demand
is true. It is false only when all zones in the configured scope are freshly
known not to demand it. Otherwise demand is **unknown**, not false.
Hidden UI zones can still demand a source; visibility is not hardware absence.
Validate mappings against enabled physical source IDs and configured controllers.

No MPD output port-to-Russound-input mapping should be inferred. One output can
serve many zones. Starting/stopping it must consider all scoped consumers.

### 10.2 Freshness and headless evaluation

Extend existing hardware observation publication with per-zone measured
power/source timestamps, observation origin, poll completion and generation.
Distinguish optimistic command state, persisted fallback and confirmed hardware
state. Request a read after relevant commands if necessary; failed commands
must not trigger a success-shaped playback policy.

When any rule is enabled, keep periodic hardware polls active **without browser
clients**. The current configured interval is 10 seconds; faster automation
must account for serial bandwidth/read timeouts. Use unsolicited updates for
latency but periodic complete reads for coverage. Empty configuration, missing
zones, partial reads, disconnected gateway or stale fields are unknown.

Never evaluate policy from persisted web state immediately after restart.
Require fresh scoped observations and a current configuration generation.
Evaluate outside hardware locks; enqueue a target after releasing them.

### 10.3 Manual precedence and supervisor integration

Introduce a versioned Pi control record under the existing mutation lock:
manual/automatic mode, effective intention, current auto-target generation and
override provenance. Migrate the existing name-to-intention file explicitly,
preserving play/pause/stop choices. Change all agent/CLI/supervisor readers and
writers together; do not bolt on a second file that competing processes
independently interpret.

D04 is accepted (2026-10-16): manual play/pause/stop switches only that output
to manual mode until explicitly returned to automatic. S04's offline migration
preview starts every output in manual mode, preserving its saved intention.
It is not installed as runtime state; the coordinated reader/writer migration
and automatic-target metadata remain prerequisites for S10.

Semantics (manual precedence accepted; remaining automation details proposed):

- Default is **manual**, retaining today's behavior; enabling automatic mode
  is per-output and explicit.
- Manual play/pause/stop switches the output to manual mode and remains
  authoritative until "Return to automatic" is selected. The CLI follows the
  same semantics. Automatic targets cannot undo a manual stop.
- In automatic mode the web sends sequenced desired targets; the Pi persists
  the effective intention. The supervisor only reconciles it.
- Source and volume edits preserve mode unless the user explicitly requests
  otherwise. Revisions reject queued auto targets after a manual override.
- "Stop stream" stops playback, not the MPD daemon. Leave daemon units active
  for fast restart; service lifecycle remains the supervisor's responsibility.

Keep a distinction between manual override, automatic demand and observed
state in the UI. Never derive desired intention from an audio-induced pause
or a temporarily stopped MPD.

### 10.4 Debounce, outages and controller ownership

Proposed defaults to validate:

- Start on confirmed demand; stop after 30 seconds of continuously confirmed
  no-demand. Cancel the pending stop when demand returns or becomes unknown.
- Treat observations older than 30 seconds as stale at a 10-second poll cadence.
  Revalidate these values against worst-case multi-controller poll duration.
- Renew automatic-target leases independently of state changes, for example
  every 10 seconds with a 30-second receiver-monotonic lease. HTTP/SSE transport
  heartbeats are not hardware-observation freshness.
- On stale hardware/web outage, **hold the last effective intention** and flag
  automation degraded; do not infer "all zones off." Manual control stays
  available. Recovery of that last playing intention remains allowed, so this
  is not a fail-silent policy and may keep an unused stream running.
- After Pi/agent restart, old automatic leases are expired. Retain the last
  intention for recovery, require fresh control ownership/observations before
  accepting new demand transitions, and report unknown automation state.

Hold-last is a recommended availability-first default, not the only reasonable
policy. A configurable fail-stop option may be desirable for resource saving;
it needs an explicit grace interval and user approval because transient network
loss would stop audio. TTL is measured locally, not by trusting host clock sync.

Use one configured authorized policy controller initially. Automatic targets
carry controller epoch/sequence and are rejected if stale or from a second
owner. Epoch takeover must be explicit/lease-fenced, not "last packet wins."
On web restart obtain fresh ownership and resnapshot hardware before sending
targets; do not replay persisted targets or queue unbounded commands offline.

D01 is accepted: automation need not make new hardware-driven decisions while
the web host is completely offline. Keep the single policy evaluator on the web
host; do not add an independent Pi hardware-state feed. The Pi preserves and
reconciles its last intention during web-host loss. Require fresh observations
and fenced ownership before automation resumes after reconnect.

## 11. Security, observability and maintenance

### Security boundary

- Current web CSRF/DNS-rebinding protections are not login/authentication.
  D07 (accepted 2026-10-17) uses trusted-environment browser access without
  login, user permissions or roles for now. Lightweight admin authentication
  may be added later, but is not part of the current implementation. Preserve
  existing request protections and web-to-agent authentication. Anyone able
  to access the web interface can use its exposed controls; reassess browser
  access protection before exposing it beyond the trusted environment.
- Agent control requires service authentication independently of browser
  origin checks. The selected D06 design terminates TLS at Caddy on the Pi and
  forwards to the loopback-only agent. Docker trusts the proxy's self-signed
  certificate through the configured `tls_ca_file`; normal certificate and
  hostname validation remain enabled. Retain the scoped bearer credential and
  firewall the proxy to the Docker host. A LAN/IP allowlist alone is not identity.
- Keep agent credentials in mounted secrets/environment configuration, never
  JavaScript, ordinary configuration payloads, request history or log URLs.
  S07 explicitly enables the existing service credential for the four
  allowlisted manual actions on configured outputs; separate read-only/control
  credentials remain a future hardening option. Rotate credentials explicitly.
- Bind MPD to loopback and limit agent ingress to authorized web/admin hosts
  after fixing/testing the known binding-schema mismatch. Do not change current
  listeners during the endurance test. Do not publish all MPD ports from Docker.
- Run the agent as the audio-owning user, not root; use the existing private
  runtime lock directory and D-Bus/Pulse environment. Restrict API operations
  to configured output IDs, not arbitrary ports, paths, systemd units or shell
  commands.
- Treat URLs/providers as untrusted network input. Initially permit approved
  HTTP(S) feeds; reject file/device/unsupported schemes. The approved deployed
  policy rejects private-network radio servers, including admin URL favorites.
  There is no private-stream bypass. Validate redirects/resolution,
  and enforce Pi egress restrictions where MPD follows redirects itself:
  web-only validation cannot prevent later DNS/redirect access from the Pi.
- Escape tags/labels as text in the UI, bound lengths, and redact URI secrets
  and MPD error contents before returning/logging them.

### Maintainability and operations

- Keep one Python domain backend, one local observer and one thin HTTP adapter.
  Extend immutable typed results; keep transport JSON away from audio helpers.
  Introduce only the small client/provider interfaces actually needed.
- Keep this in the same repository with focused agent/client modules. Extract
  a package only when it solves import/distribution needs; avoid a separate
  repository/microservice framework for three MPDs.
- Add agent dependencies explicitly, reuse the existing venv and an independently
  restartable user systemd unit with lingering. The unit wants audio services
  but must remain diagnosable while audio is unavailable.
- Agent liveness, MPD reachability, audio readiness, policy freshness and
  delivered signal are different health indicators. `state=play` and route
  correctness do not prove audible analog output.
- Log operation IDs, output IDs, revisions, policy reasons and state transitions.
  Record retry counts, queue depth, freshness and typed failures; avoid logging
  every unchanged poll or programme tags indefinitely. Use bounded retention.
- Keep API major version/capability negotiation; unknown write capabilities
  disable controls with an explanation. Older agents can still support
  read-only views; unsupported fields must not become guessed successful values.
- Treat JSON/control-schema upgrades as explicit, backed-up migrations. Old
  CLI/supervisor processes must not run against incompatible new control files.
  Rollback needs compatible binaries/config backups and coordinated services.
- Separate agent deployment from audio reinstallation. API upgrades should not
  regenerate sinks, restart MPD or overwrite calibration/intentions.
- Apply focused changes to web config validation/editor, Docker environment/
  secret wiring, Pi Ansible provisioning, CLI and supervisor as their phases
  require. A transport service alone does not complete integration.

## 12. Implementation phases and acceptance gates

| Phase | Work | Acceptance gate |
| --- | --- | --- |
| 0. Freeze and settle decisions | No Pi mutation during endurance; confirm physical inputs, URL/provider scope, volume and policy defaults | Existing endurance artifacts/config preserved; explicit rollout window |
| 1. Read-only status | Agent inventory/observation, web client/cache, stream cards and playback SSE invalidation | State and metadata update; missing metadata labeled unavailable; one failed MPD/agent does not block hardware UI |
| 2. Manual controls | Async play/pause/stop and existing `set_gain` exposure, idempotency and outcomes | Stream volume edits persist calibration; requests survive lost responses; no bypass of intentions, route verification or startup mute |
| 3. Individual sources | Stable IDs, backend selection mutation, managed queue adoption, manual URL UI | Repeated switches do not accumulate entries; restart restores chosen source; other outputs and silent intentions preserved |
| 4. Catalog integration | First server-side search/details/resolve adapter | Catalog outage leaves playback intact; unsupported/credential-bearing sources handled explicitly |
| 5. Hardware automation | Fresh observation feed, unconditional rule polling, modes/leases/precedence | Keypad, API and out-of-band changes work with all browser tabs closed; manual stop survives subsequent hardware events |
| 6. Lifecycle acceptance | Authorized outage/reboot/deployment exercises and metrics | Recovery, stale/partial states, calibration and policy semantics verified under real faults |

Test layers:

- Backend regression tests extend existing
  [MPD backend tests](../tests/test_mpdbackend.py),
  [supervisor tests](../tests/test_mpdsupervisor.py), route/gain/readiness tests
  and CLI tests. Assert mute -> play -> route -> configured calibration gain -> unmute;
  stream volume edits use the same persisted `gain_db` and survive reconciliation;
  stop/pause never start audio; invalid config and failed persistence are explicit.
- Agent contract tests cover partial observations, missing tags, read races,
  authentication, bounded workers, busy locks, revision conflicts and durable
  operation reconciliation after process/response loss.
- Web integration tests use a fake agent; test network timeout without holding
  Russound locks, independent health banners, SSE resync/fallback and escaped
  metadata. No Pi access is required for these tests.
- Policy tests use a fake clock/observations: source changes, multiple
  controllers, hidden consumers, unknown/partial zones, debounce, lease expiry,
  manual override, stale epochs and no connected browser.
- Authorized target acceptance verifies selected-source persistence, actual
  routes/gains, stopped/paused preservation, agent/web restart and network loss.
  Measure p95 update latency and unchanged healthy-output latency with one MPD
  unresponsive. USB/reboot tests are maintenance actions, not documentation work.

Proposed module boundaries are the agent HTTP adapter, local observation
collector, existing extended backend, web playback client/cache, web demand
policy and catalog adapters. Keep synchronous safety helpers in a bounded worker
instead of rewriting them into asynchronous implementations.

## 13. Decisions to confirm before implementation

The [implementation plan's decision register](./mpd-web-integration-implementation-plan.md)
tracks these topics as individual decisions, their status and implementation gates.
Solution D, direct calibration-gain stream volume and web-hosted policy with
last-intention recovery during web-host loss (D01), plus persisted immutable
output IDs with separate labels (D08), are already accepted.

1. Actual physical Russound input mapping for each Pi output, including the
   controller/zone scope used to determine no-demand.
2. Source-demand versus all-on/all-off policy; manual-override semantics;
   hold-last versus fail-stop during stale hardware/web connectivity.
3. Resolved by D01: no new hardware-driven decisions during web-host outage;
   the Pi preserves/recovers its last intention.
4. Resolved by D13/D07: calibration-gain slider -60 to 0 dB, step 0.1 dB;
   trusted-environment access without user permissions for now. Stream volume
   directly changes output calibration, not a separate attenuation layer.
5. First external catalog provider and supported playback formats/authentication;
   expiring or non-MPD sources require separate capability work.
6. Managed-queue adoption and source-switch failure behavior; this proposal
   preserves the new desired selection rather than silently reverting it.
7. D06 TLS is deployed; D07 accepts trusted-environment browser access without
   login for now. Lightweight admin authentication is deferred.

These are product/deployment choices, not reasons to duplicate control logic.
The HTTP agent boundary remains valid whichever policy/provider choices are made.

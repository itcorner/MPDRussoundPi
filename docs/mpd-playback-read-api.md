# Playback read API v1: S01 contract

Status: S01 read-contract baseline, 2026-10-14. S02 local observation collector
implemented and tested; **HTTP agent/web endpoints are not implemented**.
Changes to this baseline require updating this document and its
[contract fixtures](../tests/fixtures/playback-read-v1.json) together.
See the [implementation plan](./mpd-web-integration-implementation-plan.md).

## 1. Scope and component boundaries

- Browser calls only same-origin web routes and its existing SSE connection.
- Web owns endpoint registry, snapshot fetching/caching and hardware-demand
  policy. It never imports Pi-local audio helpers or connects directly to MPD.
- Agent owns inventory validation and read-only local observations, using the
  audio user's environment. GET routes return caches; background collectors
  perform bounded MPD/audio reads. No GET starts/reconciles playback.
- Existing backend owns all future mutations and persistent configuration.
  Existing supervisor remains the only continuous playback recovery loop.
- Web-host loss causes no new hardware-demand decisions on the Pi; existing
  recovery preserves/reconciles the last intention (accepted D01).
- Stream volume is exactly configured calibration `gain_db` (accepted A02).
- S01 freezes reads and identity, not writes, provider resolution, security
  provisioning, automation precedence or control-file migration.

Initial agent has no mutation routes or agent event stream. Later additions
must be capability-advertised and must not change these read semantics.

## 2. Identity and persisted configuration

Accepted D08: add an immutable `id` and separate editable `label` to each Pi
operational `streams` entry during S04 migration. Preserve existing `name`,
port, sink and derived unit identity. `name` remains the legacy backend/CLI/
intention key until a coordinated migration explicitly changes that.

Example migration shape, **not a change to the current Pi**:

```json
{
  "id": "front",
  "label": "Front stream",
  "name": "Radio 88.6",
  "stream": "https://radio.example/live.mp3",
  "port": 6601,
  "gain_db": -7.1,
  "audio_output": {"type": "pulse", "sink": "stereo_front"}
}
```

`agent_id` is explicitly configured persistent identity, not hostname discovery.
Agent and output IDs use ASCII `[a-z][a-z0-9_-]{0,63}` and are case-sensitive.
Output IDs are unique per agent; agent IDs are unique within the web registry.
Identity is `(agent_id, output_id)`. Labels and station URLs never generate IDs
on every startup. IDs cannot be changed through label/source editing; changing
one is an explicit structural migration. Duplicate/missing IDs produce a
configuration error, not an auto-renumbered inventory.

`front`, `centerlfe`, `rear` are illustrative migration IDs, not physical
Russound input mappings. S04 must assign/validate them before live agent rollout.
Read-only local tests can use migrated fixture configs without editing the Pi.

## 3. Common rules

- UTF-8 JSON objects, `Content-Type: application/json`, `Cache-Control: no-store`.
- All successful GETs return `200`. `HEAD` has the same status/headers, no body.
- Fields documented below are required unless marked optional; unavailable
  scalar values use JSON null, never `NaN`, infinity, sentinel state or fake title.
- Numbers are finite. Counts/observation revisions are nonnegative integers;
  elapsed/duration are nonnegative numbers or null; gain is finite and <= 0.
- UTC timestamps use RFC 3339 with `Z`. Ages are nonnegative integer milliseconds,
  computed from receiver-local monotonic clocks, not by subtracting host clocks.
- Agents/clients tolerate unknown additive fields but reject missing required
  fields, wrong types, mismatched identity and unsupported major versions.
- Capabilities advertise support, not current health. A configured stopped
  output and an inaccessible output are not the same thing.
- No response includes raw configured/current stream URLs, MPD ports, credentials,
  private config paths or transient client IDs. Errors contain sanitized messages.
  Logs follow the same redaction requirement.

### Revision domains

| Field | Meaning |
| --- | --- |
| `api_version` | Integer major version, initially `1` |
| `epoch` | Opaque identifier regenerated at agent process start |
| `revision` | Agent cache publication counter, scoped to `epoch`; does not promise a durable event log |
| `config_revision` | Opaque persisted-config token; initially SHA-256 of exact config bytes |
| Web `epoch`/`revision` | Separate web playback-cache publication identity, not hardware SSE revision |

Treat config tokens as equality-only, not numeric ordering. Gain/selection/label/
ID edits change the token, including cooperating CLI edits. Read config bytes
once for validation/settings/token; never hash one version and parse another.
Observation revisions may advance without config edits; stale-cache responses
may retain their last revision while their age grows. Future control revisions
are a different domain, not defined by this read contract.

## 4. Agent routes

| Route | Response |
| --- | --- |
| `GET /v1/health` | Liveness object |
| `GET /v1/capabilities` | Capabilities object |
| `GET /v1/outputs` | Inventory envelope |
| `GET /v1/outputs/{output_id}` | Same envelope metadata with `output` instead of `outputs` |

All four routes require configured service authentication. Exact credential/TLS
provisioning remains D06; health must not become a public inventory backdoor.
HTTP service unavailable cannot yield JSON; clients report a transport error.

### Liveness object

`api_version`, `agent_id`, `epoch`, `status: "alive"`.
An agent can be alive with invalid audio config or dead MPDs. Liveness does not
claim audio readiness. Configuration health is reported by inventory errors.

### Capabilities object

`api_version`, `agent_id`, `epoch`, `read_features`, `write_operations`.

Initial `read_features` lists `inventory`, `playback`, `now_playing`,
`audio_diagnostics`; `write_operations` is `[]`. Metadata capability does not
mean every station supplies tags. Optional diagnostics that are unsupported
are omitted from the feature list and have explicit `unsupported` probe status.
Do not advertise writes before their handlers, authentication and semantics
are implemented. Read-only clients disable all mutation controls.

### Inventory envelope

`api_version`, `agent_id`, `epoch`, `revision`, `config_revision`,
`snapshot_at`, `snapshot_age_ms`, `stale`, `outputs`.

Outputs are deterministically sorted by ID. A valid output's individual MPD/
audio failure still returns `200` with explicit failure in that output, alongside
healthy outputs. Invalid/unreadable configuration returns `503`, not `outputs: []`.
A cold cache with known inventory returns pending probes; a cache whose
inventory is not known yet returns `503 snapshot_unavailable`.
Single-output lookup uses the same cached publication and returns `404` only
after valid inventory establishes the ID is absent.

### Output object

| Field | Producer / interpretation |
| --- | --- |
| `id`, `label` | Pi operational config ID/label |
| `selection.label` | Configured source label when available; legacy `name` as initial clearly labeled station fallback |
| `gain.configured_db` | Config `gain_db`; stream-volume calibration, not measured gain |
| `playback` | MPD observer probe |
| `audio.readiness` | Read-only `audio_ready_config(..., enforce_profile=False)` on the captured config |
| `audio.route` | Read-only `inspect_route` for freshly observed active playback; expected sink comes from config |
| `audio.gain` | Read-only two-channel `pactl` raw mixer units |

Selection kind/provider IDs and desired control state are intentionally absent
from v1 initial reads: provider provenance and mode/intention schema are not
implemented yet. Advertise later fields additively. A station label is not
now-playing metadata.

### Probe object (playback and each audio subprobe)

All probes have `status`, `attempted_at`, `observed_at`, `age_ms`, `stale`,
`error`, `value`, `last_known`.

| Status | Current value | Meaning |
| --- | --- | --- |
| `ok` | Object | Most recent attempt succeeded; still inspect age/stale |
| `error` | null | Attempt failed; `error` required |
| `pending` | null | No completed attempt; error null |
| `not_applicable` | null | Route on a known paused/stopped output; error null |
| `unsupported` | null | Capability absent; error null |

`attempted_at` identifies the latest attempt; `observed_at` and `age_ms` refer
to the current successful value, so are null when `value` is null. Failed
probes are `stale: true`; pending/unsupported are also stale. Not-applicable
routes are not stale when justified by a fresh playback observation; otherwise
use pending/error, not an assumed stopped state.

`last_known` is null or `{observed_at, age_ms, value}` from an earlier successful
probe, only when the current value is unavailable. It is never substituted
for a fresh value. Restart can clear it; no persistent telemetry is promised.
For an `ok` probe exceeding its freshness budget, preserve the value with
`stale: true`. The UI must label it old rather than showing it as current.

`error` is null or `{code, message, retryable}` with a sanitized description.
MPD failure never creates a synthetic `state: "stop"`.

### Playback value

| Field | Source / limits |
| --- | --- |
| `reachable` | true for successful MPD snapshot; reachability failure is probe error |
| `state` | MPD `play`, `pause`, `stop` |
| `song_id` | MPD song ID as string or null; no persistent identity implied |
| `queue_entries` | MPD playlist length, integer |
| `elapsed_seconds`, `duration_seconds` | MPD optional finite numeric values or null |
| `bitrate_kbps`, `audio_format` | Optional MPD bitrate number / format string or null |
| `mpd_error` | Generic "MPD reported a playback error." or null; reachable MPD can report an error without exposing raw URLs/paths |
| `now_playing` | `{title, artist, album, station, metadata_available}`; tags are nullable strings |

`metadata_available` is true if at least one nonempty normalized MPD tag exists;
configured labels/URLs do not count. Repeated MPD tag lists normalize to one
display string. Bound text to 512 Unicode characters and render as text, not HTML.
Known pause/stop may retain MPD's selected-song tags; UI must label those as
paused/last selected, not assert current audible playback.

Use bounded song-ID consistency checks around `status`/`currentsong`; races that
do not settle are `snapshot_inconsistent`, not mismatched title/state success.
Do not use route/signal checks as evidence of physical audible delivery.

### Audio values

- Readiness: `{ready: true}` on successful inspection. Missing hardware/profile/
  sink is a probe error `audio_unavailable`, not enforcing/repairing readiness.
- Route: `{expected_sink, actual_sink, correct}`. Incorrect route is a successful
  observation with `correct: false`; missing/ambiguous client is an error.
- Gain: `{measured_units: [left, right]}` with two nonnegative integers.
  Configured gain is outside this probe and survives observation failure.
  Inactive mixer readings are informational; they need not match configured gain
  until playback starts. Inspection never applies gain.

## 5. Freshness and polling

Engineering baseline: MPD collector and web fetcher approximately every 2 seconds,
playback/UI snapshot stale threshold 10 seconds. Audio diagnostics target
10-second collection, stale threshold 30 seconds. These are implementation
targets to verify, not guarantees. All probe ages are independent.

Web computes effective age from agent-reported age, full HTTP request duration
(conservative), and elapsed time since receipt. For agent `stale` values the web
never clears the flag. Same rule applies to probe/last-known ages. A successful
network fetch does not make old MPD/audio readings fresh.

Inventory envelope age covers agent publication only; it is not every probe's
age. Hardware freshness remains separate in the existing Russound domain.
GET handlers must return caches without holding MPD calls, audio checks or
Russound hardware locks. Bound output failures independently.

## 6. Same-origin web reads

| Route | Response |
| --- | --- |
| `GET /api/playback/state` | Web playback envelope with configured-agent entries |
| `GET /api/playback/agents/{agent_id}/outputs/{output_id}` | `{api_version, agent_id, output}` from web cache; effective ages as above |

Web envelope: `api_version`, `epoch`, `revision`, `configured`, `agents`.
Each agent entry: `agent_id`, `status`, `last_attempt_at`,
`last_success_at`, `error`, `snapshot`.

Agent status is `ok`, `stale`, `unavailable` or `pending`. Failed transport/auth/
schema checks yield unavailable with explicit error; retain a previous snapshot
only with effective stale ages. Cold failures have null snapshot. Registry/cache
is web-owned; do not forward browser-supplied arbitrary endpoints to agents.

Disabled integration is `200` with `configured: false, agents: []`.
This is distinct from a configured agent failure, which remains in `agents`
with explicit status. Aggregate reads return `200` for partial/all remote outages
so the UI can render failures without losing other agent entries. Invalid local
registry returns `503 configuration_invalid`, not disabled integration.

Single-output web route returns `404` for unknown configured agent/output after
inventory is known; `503 agent_unavailable` for failed agent or unknown inventory.
A cached output can be returned `200` as stale with explicit probe ages only
when inventory and output are known. Existing browser request protections apply;
D07 access authorization is resolved before write rollout.

## 7. Errors and SSE

Non-200 response:

```json
{
  "api_version": 1,
  "error": {
    "code": "output_not_found",
    "message": "Output is not configured.",
    "retryable": false
  }
}
```

| Status | Codes |
| --- | --- |
| 400 | `invalid_request` |
| 401 / 403 | `unauthorized` / `forbidden` |
| 404 | `output_not_found`, `agent_not_found`, `route_not_found` |
| 503 | `configuration_invalid`, `snapshot_unavailable`, `agent_unavailable` |
| 500 | `internal_error` (logged, sanitized response) |

Probe codes include `mpd_unavailable`, `snapshot_inconsistent`,
`observation_invalid`, `audio_unavailable`, `route_unavailable`,
`gain_unavailable`. Web remote errors include `agent_timeout`, `agent_unreachable`,
`agent_unauthorized`, `agent_configuration_invalid`, `agent_protocol_error`.
Remote credentials/schema errors are not an HTTP browser-login error.

Keep `/api/state` and hardware `state-change` SSE payloads unchanged. Add
`playback-change` on **the same** `/api/events` connection:

```json
{"api_version": 1, "epoch": "web-process-identifier", "revision": 8}
```

It is an invalidation notification: frontend refetches `/api/playback/state`.
No URLs or programme metadata in event text. Coalesce/bound slow-client
invalidations. Web revision changes for meaningful state/metadata/config/error/
freshness transitions, not elapsed-time ticks. Refetch hardware and playback on
every SSE reconnect; retain browser-visible polling fallback. Event revision
must match an already published cache. SSE is not durable/replayable.

## 8. Implementation surfaces and fixtures

| Surface | Planned responsibility |
| --- | --- |
| Existing `lib.mpdbackend` | Shared settings/connection helpers, additive detailed read result |
| [Pi-local `lib.playbackstatus`](../mpdrussoundpi/russound/lib/playbackstatus.py) (S02 implemented) | Normalization, independent probes/cache collection; no mutation |
| New `playbackagent.py` beside Pi scripts | Thin agent HTTP adapter and lifecycle entry point |
| New `web/playback_client.py` | Remote contract decoding, timeout/age handling |
| Existing web server/frontend | Optional registry, cache publication, same-origin reads, SSE and cards |

Only the S02 local collector/helpers are implemented; agent and web modules
remain future work. Use
the Pi script directory's existing `lib` import layout initially; do not perform
an unrelated package migration. Agent dependency manifest belongs with Pi
scripts. Web client dependencies stay in web requirements; the Docker image
keeps excluding Pi audio code. Define typed results without importing audio
helpers into the web domain.

Fixture file contains healthy, unreachable MPD, pending, inactive-route, stale and disabled
examples plus envelopes, errors and SSE. Timestamps/ages/epochs are illustrative;
config tokens are valid token shapes, not hashes of real operational files.
S02/S03/S05 tests consume these same fixtures. No current endpoint or output
schema is claimed implemented just because these examples exist.

S01 acceptance evidence:

- D08 recorded; ownership/read routes/identity/revisions frozen here.
- Every read field has a producer and explicit missing/error semantics.
- No mutation on reads, URL leakage or separate stream-volume layer.
- Hardware API/SSE preserved; playback transport additive.
- Fixture JSON and documentation links validated; no Pi/runtime modification.
- S01 was documentation-only; local collector acceptance is recorded below.
  HTTP, browser and live latency tests remain S03/S05/S06.

S02 acceptance (local, 2026-10-14):

- Collector/helpers and [31 collector/helper tests](../tests/test_playbackstatus.py)
  implemented. 100 focused tests ran: 99 passed, one existing real-FFmpeg test
  skipped because FFmpeg is unavailable. Modified runtime modules have no
  Pylance diagnostics.
- Existing `MPDBackend.status()` and mutation semantics preserved.
- Fake-client command records prove read-only MPD access; captured readiness
  inspection never enforces a profile; stereo gain inspection uses GET only.
- Event-controlled tests verify healthy publication while another MPD/audio
  read is blocked, without queuing duplicate jobs.
- Collector requires migrated IDs/labels and captures one config revision.
  S03 must handle periodic scheduling and background collector replacement when
  config changes; S04 still owns the production identity migration.
- No Pi deployment/playback change. HTTP/browser/real-LAN latency acceptance
  remains S03/S05/S06, not proven by these tests.

# Idea: deployment and runtime configuration overview

Status: **new, in progress** (2026-11-21): the host section is implemented,
deployed and accepted on the reference Pi; the component and audio sections
(second slice) are implemented and await deployment (see
[Implementation progress](#implementation-progress)); topology, the web host's
own facts and deployment metadata are not started. Proposed as an idea on
2026-11-12. On 2026-11-21 a **host information** section was added to the scope
(see [Host information](#host-information)). Decisions 6 to 9 were taken on
2026-11-21 (below); decisions 1 to 5 still gate the rest of the overview.

## Goal

Extend the existing Status page with a clean, complete, read-only overview of
the running deployment: where components run, how they connect, what is
configured and what is actually working. The overview should help diagnose
split-host and all-on-one-Pi deployments without requiring SSH.

Keep the current connected-client, event-history and license sections. Start
with a concise topology and health summary; expand component details on demand
so that a multi-agent, multi-output setup remains readable on mobile.

Alongside the software components, show **information about the hosts they run
on**: network connection and address, Wi-Fi signal where applicable, CPU load,
memory, temperature and similar health facts. On a Pi that streams audio over
Wi-Fi these are often the first things to check when playback misbehaves, and
they are the facts the planned
[network relocation](./general_idea_network-relocation.md) and
[Wi-Fi bootstrap](./general_idea_wifi-bootstrap.md) work needs to explain.

## Components and information

| Component | Summary and expandable details |
|---|---|
| Web application | Host or deployment identity when known; Docker vs. native; running version/revision and, for Docker, image reference and immutable digest when reliably available; web endpoint and health. |
| Playback agents and proxies | Each configured agent's identity, reachable endpoint, hosting location and Docker/native mode when known; version/image when known; capability, freshness, connectivity, proxy/TLS status and sanitized configuration. Support multiple agents independently. |
| Russound backend / serial gateway | Effective gateway host/port or serial endpoint, configured vs. observed transport, Docker/native mode and image identity when known; distinguish a listening gateway from a responding controller. Support an external gateway and disabled or absent backend. |
| Russound controller | Controller identities and configured zones where available; communication status and last successful response. This is physical hardware, not automatically a process or container: its physical location, model and firmware are unknown unless explicitly configured or actually observed. |
| MPD and audio | For each agent and output: MPD identity, host/port or endpoint **if an approved contract exposes it**, process/running and playback status when known, desired vs. observed state, audio device and channel mapping, configured gain, sink, actual route and readiness. Distinguish shared hardware from per-output settings; link identities to their agent. |

| Hosts | One card per distinct host (the audio host that runs the agent, and the web host when it is a different machine): identity, network, Wi-Fi, CPU, memory, temperature, disk and clock facts. See [Host information](#host-information). |

Show values with provenance and freshness: *configured*, *observed*,
*deployment-reported*, or *unknown/unsupported*. Include observation time/age,
stale/unavailable state and a sanitized reason. Never infer a physical host from
a Docker container hostname or DNS alias, claim a running image from a
configured tag, or equate an open TCP port with a working controller. Show
configured and observed values separately when they disagree.

## Host information

### What can be read, and where from

Probed read-only on the reference Pi (Raspberry Pi 4 Model B, Debian 13) on
2026-11-21, as the unprivileged service user. Values are an example of what the
page would show, not a specification.

| Fact | Source | Example on the reference Pi | Notes |
|---|---|---|---|
| Model, OS, kernel | `/proc/device-tree/model`, `/etc/os-release`, `uname` | Raspberry Pi 4 Model B Rev 1.5, Debian 13, 6.18.29 | Static; read once. |
| Uptime | `/proc/uptime` | 5 days | Same value seen from the web container (host-wide). |
| CPU load | `/proc/loadavg`, CPU count | 1.31 / 1.03 / 0.96 on 4 cores | Load average is host-wide inside a container too. |
| CPU busy % | delta of `/proc/stat` between two background samples | about 49 % in a one-second sample | Needs two samples, so a collector thread, never a per-request read. |
| CPU frequency | `scaling_cur_freq` | 1.8 GHz | Optional. |
| Memory | `/proc/meminfo` (`MemTotal`, `MemAvailable`, swap) | 927 MB total, 452 MB available, about 252 MB swap in use | The Pi has 1 GB; this is a real constraint. |
| Temperature | `/sys/class/thermal/thermal_zone0/temp` | 44 °C | The Pi throttles near 80 °C. Not present on every host. |
| Power and throttling | `vcgencmd get_throttled` (Pi only) | `0x0` | Bits: under-voltage, frequency capped, throttled, soft temperature limit, and "has occurred since boot" for each. |
| Disk | `statvfs` of the root and the data directories | 32 % of 62 GB used | SD-card capacity. |
| Connection | NetworkManager (`nmcli device show`) | `wlan0` Wi-Fi, connected | Also Ethernet, hotspot or disconnected. |
| Address, gateway, DNS | NetworkManager / `ip` | `192.168.1.105/24`, gateway and DNS `192.168.1.1` | DNS and gateway feed the relocation diagnostics. |
| Wi-Fi signal | `/proc/net/wireless` (dBm, no process), `nmcli ... --rescan no` (percent, rate, channel, security) | 42 %, -76 dBm, 270 Mbit/s, 5 GHz channel 40, WPA2/WPA3 | **Never trigger a Wi-Fi scan:** scanning interrupts the radio and can stutter streams. Read the current link only. |
| Wi-Fi network name | NetworkManager | the home network's SSID | Identifying; see disclosure. |
| Clock | `timedatectl` | synchronized, NTP active | Relevant to certificate validity after a move. |

Observations on this Pi, worth showing the operator: the Wi-Fi link is weak
(-76 dBm) on a system whose only uplink is Wi-Fi and which streams radio, and
swap is in use on a 1 GB device. A page that shows these makes such causes
visible without SSH.

### Which process reports what

The web app normally runs in Docker, so it cannot see the host's network:

| Source | Sees | Does not see |
|---|---|---|
| Web process in Docker | Host-wide load, memory, uptime, temperature and CPU count (these files are not namespaced) | The host's network interfaces and Wi-Fi (the container has only its own `eth0`), the host name, the machine id |
| Web process, native | Everything the user can read | none of the above is missing |
| Playback agent (native on the Pi) | All facts in the table, as the service user | none |

Consequences:

- **The audio host's information comes from the agent** through a new additive,
  read-only route, for example `GET /v1/host`, advertised by a capability
  (`host_info`) and allowlisted in the proxy (the Caddyfile currently permits
  only `/v1/health`, `/v1/capabilities`, `/v1/outputs`, `/v1/favorites`). Old
  agents report "not supported", never invented values, as the rest of this
  plan requires. This route needs the existing contract rules
  ([agent read contract](../documentation/mpd-playback-read-api.md#3-common-rules))
  extended deliberately; it exposes nothing from the excluded list (tokens,
  private paths, MPD ports, stream URLs).
- **The web host's own information** comes from the web process itself, with the
  limits above in Docker. In a Docker deployment a missing network section is
  labelled "not visible from the web container", not left blank.
- **One card per host, not per component.** On the reference layout the web
  container and the agent run on the same Pi. They can prove it without any
  identifier reaching the browser: the kernel's boot id (`/proc/sys/kernel/random/boot_id`)
  is identical inside the container and on the host (verified), so the web
  server compares its own value with the one the agent reports and merges the
  cards. The comparison happens server-side only; the boot id, machine id,
  serial number and MAC addresses are never sent to the browser. A container host
  name is never used to infer the physical host.

### Collection and cost

- Background collector threads and cached snapshots, as for the rest of this
  plan; browser count never multiplies reads. On the agent: cheap `/proc`
  reads every few seconds, process-spawning probes (`nmcli`, `vcgencmd`) less
  often (for example every 30 seconds), each with a timeout, and none under
  the playback mutation lock. The web polls `/v1/host` at a lower rate than
  playback.
- Each value carries the observation time, source and an explicit state
  (observed, unavailable, unsupported). Missing sensors (no `vcgencmd` or
  thermal zone on a non-Pi host, no Wi-Fi interface on Ethernet-only hosts, no
  NetworkManager) are reported as unsupported, not as zero.
- Counters and percentages are derived from deltas between samples; a restart
  of the collector must not produce a spike.

### Presentation

- A **Host** card (or one per host) at the top of the Status page: a one-line
  summary, for example "Wi-Fi weak (-76 dBm) · CPU 33 % · memory 51 % · 44 °C",
  with details on demand (addresses, link rate, channel, swap, disk, clock,
  power flags). Readable on a phone, no information carried by colour alone.
- Qualitative labels need agreed thresholds (see decisions). Starting
  proposal, to be confirmed: Wi-Fi good at -60 dBm or stronger, fair to -70,
  weak below; memory warning when available memory is below 10 %; temperature
  warning from 70 °C and critical from 80 °C; any set throttling or
  under-voltage flag is always shown, including "occurred since boot".
- Untrusted text such as a Wi-Fi network name can contain markup or very long
  or non-printing characters: render as text only, cap its length and strip
  control characters.

### Disclosure classes

The status page has no login, so each field needs an explicit class before it
is shown (the first decision below):

| Class | Fields | Proposal |
|---|---|---|
| A: operational | load, CPU %, memory, swap, temperature, throttling flags, uptime, disk usage, Wi-Fi signal and link rate, connection type and state, clock sync state | Show to every user of the page. |
| B: network identity | IP addresses, gateway, DNS servers, Wi-Fi network name, channel, host name | Needs the disclosure decision; users reach the page by address, so the IP is rarely secret, but the network name and DNS are more identifying. |
| C: never shown | MAC addresses, serial number, machine id, boot id, secrets, file paths | Never exposed, including through the agent route. |

## Existing foundations and gaps

- [`/status`](../../web/static/status.html) currently shows connected clients,
  frontend events and license notices, with polling and SSE coordination in
  [`status.js`](../../web/static/status.js). It is not a deployment inventory.
- [`/api/config/state`](../../web/API.md#get-apiconfigstate) and
  [`/api/rooms`](../../web/API.md#get-apirooms) expose configuration and
  Russound backend status; the latter distinguishes a reachable but silent
  controller. Effective endpoint overrides and physical location need careful
  treatment rather than assuming saved configuration is the active endpoint.
- [`/api/playback`](../../web/API.md#get-apiplayback) and
  [`/api/playback/health`](../../web/API.md#get-apiplaybackhealth) expose cached
  per-agent/output playback and audio observations with failures and freshness.
  Reuse these sources and existing background collectors where possible.
- The [agent read contract](../documentation/mpd-playback-read-api.md#3-common-rules)
  explicitly excludes MPD ports, credentials, private config paths and raw
  stream URLs. A richer deployment overview must not silently break that
  contract: decide what may be exposed and, if approved, define an additive,
  capability-advertised contract and its proxy/web allowlists.
- A process generally cannot reliably identify its host's container image or
  discover an independent remote gateway's image. Assess explicit,
  deployment-supplied metadata or a narrowly scoped local reporting mechanism;
  treat it as reported rather than independently verified unless verification
  exists. There is no assumption that the web container can inspect Docker.

## Candidate design

```text
Browser /status -- same-origin --> Web deployment overview API
                                      |-- web/runtime and effective config
                                      |-- cached Russound backend health
                                      `-- cached agent inventory/diagnostics
                                             via existing authenticated TLS proxy
```

Define a versioned, allowlisted read-only overview, potentially as a separate
web API so existing status and playback consumers remain compatible. The web
aggregates independently available sections; one unreachable agent or gateway
must not hide the rest. Old agents and unknown capabilities must produce
explicit unsupported/unknown fields, not fabricated defaults. A new agent
route, if needed, requires matching capability, authenticated proxy rule,
web client/validation and rollout compatibility.

Use bounded background collection, request timeouts and cached snapshots;
browser count must not multiply Pi probes. Integrate with existing page
visibility, polling and SSE policies without blocking the Russound state lock,
issuing hardware commands or starting/reconciling playback. Show errors and
the last known data with its age, and avoid replacing healthy values with
misleading empty inventories.

**Security and privacy:** current web access has no user authentication and
the status page can expose operational data. Decide which deployment details
are acceptable to every web user before adding them. Never expose tokens,
credentials, private keys, secret-file contents/paths, arbitrary environment
variables, raw stream URLs or unrestricted configuration dumps. Address
hostnames, IPs, ports, device identifiers, image references, client privacy and
configuration paths explicitly in the design. Render untrusted values as
text, and do not mount the Docker socket, grant elevated privileges, add
browser-to-agent access, or disable TLS verification merely for this feature.
The existing agent contract excludes MPD ports and private paths; do not expose
them until an explicit, compatible disclosure and authorization decision is
made. Share effective configuration metadata with
[inline overrides](./web_done_effective-config-overrides.md), and share image
identity and release-channel information with
[update status](./general_idea_update-status-and-restarts.md).
The complementary [guided diagnostics](./general_idea_guided-diagnostics.md)
should reuse these facts instead of adding independent probes.

## Decisions needed before implementation

1. What deployment information may unauthenticated status-page users see?
   Are hostnames, IPs, MPD ports and hardware identifiers acceptable, or does
   detailed inventory require a separately designed access control?
2. Which layouts must be supported initially: web/gateway in Docker, native
   services, external gateway, multiple agents, and a future containerized
   agent? How should user-supplied physical location labels be managed?
3. Which facts must be observed live versus declared at deployment? How should
   image tag, image digest, code revision and unknown or outdated metadata be
   distinguished, especially for remote or third-party gateways?
4. Which MPD and audio fields are essential, and can the existing read
   contract's prohibition on ports or private paths be preserved? What is the
   minimum useful display when the agent does not support new metadata?
5. What refresh rate, retention and stale thresholds are appropriate? Should
   the overview be automatic, manually refreshed, or a combination?
6. **Host information, disclosure.** *Decided 2026-11-21:* class A plus the IP
   addresses of the uplink interface. The Wi-Fi network name, channel, gateway,
   DNS servers and host name are not shown (the collector never reads the
   network name or channel). Class C is never shown. The address fields can be
   widened later by an explicit decision, because a wider contract is additive.
7. **Host information, scope of the first release.** *Decided 2026-11-21:* the
   audio host first, through a new agent route and a proxy change, as a first
   slice before the full topology overview. The web host's own facts and the
   boot-id host merge follow later.
8. **Thresholds and labels.** *Adopted as proposed (2026-11-21):* Wi-Fi good from
   -60 dBm, fair to -70, weak below; memory low below 10 % available;
   temperature high from 70 °C, critical from 80 °C; power flags as described.
   Both labels and raw values are shown. They are constants at the top of
   `web/host_status.py` and can be tuned from experience.
9. **Agent contract change.** *Approved 2026-11-21:* additive `host_info`
   capability and `GET /v1/host` route with the matching proxy allowlist entry.
   New web tolerates old agents (shows "not supported"); the agent and proxy
   change is deployed through the reviewed process.

## Implementation progress

| Date | Change | Notes |
|---|---|---|
| 2026-11-21 | First slice of host information: `mpdrussoundpi/russound/lib/hostinfo.py` (background collector, probe objects, parsers) and `GET /v1/host` with the `host_info` capability and `--disable-host-info`; proxy allowlist entry in the Caddyfile template; `web/host_status.py` (allowlist validation, assessment, display rows); a separate slow poll in the web client; `GET /api/status/host`; the Host card on the Status page with details that stay open across refreshes; read contract, web API and deployment docs. | Not deployed. Needs the proxy play, an agent candidate and a web image, in the order described in the [deployment guide](../documentation/playback-agent-deployment.md#host-information-optional). |

| 2026-11-21 | **Deployed and accepted on the reference Pi** at `d41264e`: proxy play (the Caddyfile diff was the single added `/v1/host` path), agent candidate, then the web through the reviewed `deploy --update`. | Acceptance: the live `/api/status/host` card was compared with independent observations on the Pi taken at the same moment. Load 1.71/1.56/1.29 vs 1.73/1.57/1.30; Wi-Fi -76 dBm, 270 Mbit/s, 5 GHz identical; address, uptime, clock sync, throttling flags and disk agree (disk to the rounding of `df`); temperature 44.3 vs 44 °C; memory and swap within a few MiB, the difference being the card's snapshot age (up to 5 s). Playback unchanged before and after (three outputs playing, routes ok). The served page contains the Host panel and no `innerHTML`. |

**Rollout incident (2026-11-21).** The first web update stopped the web and ser2net
containers and then failed **before taking a backup**: the operator's validation of
the old-image archive rejected registry multi-platform images ("Archive descriptor
selects a different image config"), because their index lists a BuildKit attestation
per platform and manifests for platforms that were never pulled. No apply had been
attempted, so `resume-original` restored the original runtime without touching data;
the services were down for a few minutes. The validator now accepts that layout
(PR #66, see the [deployment guide](../documentation/container-deployment.md)); the
operator tooling on the Pi was upgraded through the reviewed play, the export was
replayed read-only for both images, and the retry completed with every required check.

| 2026-11-21 | **Second slice, component and audio overview** (not yet deployed): `GET /api/status/overview` with web, gateway, agent and audio sections; additive agent route `GET /v1/audio` and `audio_info` capability (model-level hardware only) with the proxy allowlist entry; the web image carries its git revision (`BUILD_REVISION` build argument, set by CI and by the operator's local build); certificate expiry from a verified handshake; the "System overview" panel. | Decisions of this round: audio hardware included now; model-level hardware details (card profile, sink names, channel mapping, USB vendor:product; serials, bus paths and MAC addresses stay hidden); the revision is baked into the image and displayed as "reported by the build". Rollout: proxy play, agent candidate, web update. |

Not part of the first slice: the web host's own facts, merging cards of the same
host by boot id, widening the disclosure, and everything in the topology and
component overview.

## Proposed implementation stages

- [ ] Decide scope, disclosure policy, topology and UX; document an explicit
  field inventory with owner, source, provenance, redaction and freshness.
- [ ] Define and test additive contracts and mixed-version behavior across the
  agent, proxy and web, including disabled, partial and offline configurations.
- [x] Host information, first slice: parsers with fixture-based tests for
  `/proc`, NetworkManager and `vcgencmd` output; a bounded collector in the
  agent and the additive `host_info` capability and route; the proxy
  allowlist entry; web aggregation; the Host card on the Status page. Old
  agents show "not supported". (Merging by boot id and the web host's own card
  are deferred, see above.) Implemented 2026-11-21; target acceptance on the Pi
  is still open.
- [x] Host information, target acceptance: deployed the proxy, agent and web as
  described in the deployment guide and compared the card with independent
  observations on the Pi (2026-11-21, see the progress table).
- [x] Component and audio overview (second slice): web, gateway, agent and
  audio sections, `GET /v1/audio`, build revision in the image, certificate
  expiry. Implemented 2026-11-21; target acceptance is open.
- [ ] Implement bounded collectors/aggregation and deployment metadata
  provisioning for approved layouts; update the deployment and API guides.
- [ ] Add the responsive Status overview, preserving existing sections and
  showing degraded, stale, unknown and conflicting values accessibly.
- [ ] Verify automated coverage and perform read-only acceptance on the target
  Pi and relevant alternate layouts; document limitations and results.

## Acceptance and verification

- Test native and Docker web/gateway/agent combinations as supported, explicit
  remote gateways, multiple agents and outputs, and unknown image/host facts.
  Confirm the actual running image is not asserted from a tag alone.
- Exercise responding, silent, disconnected and recovering Russound hardware;
  distinguish gateway TCP health from controller responses. Test absent USB,
  offline/old agents, stale snapshots, partial output failures and recovery.
- Verify configured vs. observed MPD state, route/readiness and gain; ensure
  no UI request alters playback, routes, configuration or serial state.
- Test capability/version validation, proxy allowlists, TLS/auth failures,
  timeouts and bounded refresh under multiple tabs; ensure existing clients and
  event history continue working.
- Test redaction of tokens, stream URLs, secret paths and disallowed identifiers;
  render hostile labels safely. Cover keyboard access, mobile layout, readable
  status text and retention of expanded details during refresh.
- Host information: parsers for every source against recorded fixtures
  (Wi-Fi, Ethernet, hotspot, disconnected, no thermal zone, no `vcgencmd`,
  non-Pi Linux, a macOS development host reported as unsupported); delta
  calculations after collector restart; hostile Wi-Fi network names rendered as
  text; confirm no Wi-Fi scan is ever triggered and the collectors stay within a
  small, measured CPU budget on the Pi; check that the merged card is the same
  host only when the boot ids match, including a Docker web host plus a
  native agent and a split-host layout; verify class C values never appear in
  any response.
- Target acceptance must compare displayed information with independent
  host/container/service observations without exposing secrets. Report any
  unavailable hardware or untested layout rather than claiming full coverage.

Related ideas and plans: [network relocation](./general_idea_network-relocation.md)
(needs address, DNS and clock facts from the same collectors),
[Wi-Fi bootstrap](./general_idea_wifi-bootstrap.md),
[agent containerization](./mpd_new_agent-containerization.md)
and [user-triggered image updates](./general_idea_user-triggered-image-updates.md).
This overview is read-only and does not authorize or implement updates.

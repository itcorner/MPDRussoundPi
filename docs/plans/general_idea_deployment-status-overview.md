# Idea: deployment and runtime configuration overview

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Extend the existing Status page with a clean, complete, read-only overview of
the running deployment: where components run, how they connect, what is
configured and what is actually working. The overview should help diagnose
split-host and all-on-one-Pi deployments without requiring SSH.

Keep the current connected-client, event-history and license sections. Start
with a concise topology and health summary; expand component details on demand
so that a multi-agent, multi-output setup remains readable on mobile.

## Components and information

| Component | Summary and expandable details |
|---|---|
| Web application | Host or deployment identity when known; Docker vs. native; running version/revision and, for Docker, image reference and immutable digest when reliably available; web endpoint and health. |
| Playback agents and proxies | Each configured agent's identity, reachable endpoint, hosting location and Docker/native mode when known; version/image when known; capability, freshness, connectivity, proxy/TLS status and sanitized configuration. Support multiple agents independently. |
| Russound backend / serial gateway | Effective gateway host/port or serial endpoint, configured vs. observed transport, Docker/native mode and image identity when known; distinguish a listening gateway from a responding controller. Support an external gateway and disabled or absent backend. |
| Russound controller | Controller identities and configured zones where available; communication status and last successful response. This is physical hardware, not automatically a process or container: its physical location, model and firmware are unknown unless explicitly configured or actually observed. |
| MPD and audio | For each agent and output: MPD identity, host/port or endpoint **if an approved contract exposes it**, process/running and playback status when known, desired vs. observed state, audio device and channel mapping, configured gain, sink, actual route and readiness. Distinguish shared hardware from per-output settings; link identities to their agent. |

Show values with provenance and freshness: *configured*, *observed*,
*deployment-reported*, or *unknown/unsupported*. Include observation time/age,
stale/unavailable state and a sanitized reason. Never infer a physical host from
a Docker container hostname or DNS alias, claim a running image from a
configured tag, or equate an open TCP port with a working controller. Show
configured and observed values separately when they disagree.

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
[inline overrides](./web_idea_effective-config-overrides.md), and share image
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

## Proposed implementation stages

- [ ] Decide scope, disclosure policy, topology and UX; document an explicit
  field inventory with owner, source, provenance, redaction and freshness.
- [ ] Define and test additive contracts and mixed-version behavior across the
  agent, proxy and web, including disabled, partial and offline configurations.
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
- Target acceptance must compare displayed information with independent
  host/container/service observations without exposing secrets. Report any
  unavailable hardware or untested layout rather than claiming full coverage.

Related ideas: [agent containerization](./mpd_new_agent-containerization.md)
and [user-triggered image updates](./general_idea_user-triggered-image-updates.md).
This overview is read-only and does not authorize or implement updates.

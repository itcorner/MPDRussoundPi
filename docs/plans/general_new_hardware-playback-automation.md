# Hardware-driven playback automation

Status: **new, lower priority; not implemented or scheduled** (split from
[MPD web integration](./general_done_mpd-web-integration.md) on 2026-11-12).
This plan owns the former S10 and its automation-specific lifecycle acceptance.
The existing manual integration can be validated and closed independently.
Nothing in this plan authorizes live mutations, deployment or fault tests.

## Goal and accepted boundaries

Offer optional hardware-state-driven playback using the web application's
Russound observations. Preserve the existing manual playback, gain, source,
intentions and Pi-local recovery. The browser continues to talk only to the
web application; the web is the single hardware-demand policy owner and sends
bounded targets to the authenticated Pi agent.

- **D01, accepted:** while the web host is unavailable, the Pi preserves and
  reconciles the last playback intention; it does not independently evaluate
  hardware demand. On return, use fresh observations and fenced ownership.
- **D04, accepted:** manual play/pause/stop switches that output to manual mode
  until the user explicitly returns it to automatic. Acceptance does not mean
  automatic mode is currently implemented or enabled.
- Stable identity is `(agent_id, output_id)`; hardware input/output mappings
  must be supplied and verified, never inferred from MPD port or sink names.
  Existing control files, agent, CLI, supervisor and recorder must agree on any
  new schema; preserve play/pause/stop and use manual defaults.

## Decisions required before implementation

| ID | Open decision | Prior proposal or alternative |
|---|---|---|
| D02 | Hardware demand rule | Any powered zone selecting the mapped input; alternatively any powered zone starts all managed outputs. |
| D03 | Physical mapping and zone scope | Collect explicit user-verified mapping after D02. |
| D05 | Unknown/stale hardware observations | Hold the last effective intention and show degraded automation; alternatively stop after an agreed grace period. |
| D15 | Automatic stop delay | Proposed 30 seconds continuously confirmed no-demand; alternatively immediate or another interval. |
| D16 | Freshness and lease timing | Proposed 30-second stale/lease thresholds and 10-second poll/renew cadence, adjusted to measured poll duration. |

Recommendations are not accepted decisions. Resolve these before implementing
the corresponding behavior. D01 does not decide the stale-state policy while
the web remains running.

## Implementation work: former S10

- [ ] Extend hardware publication with successful per-zone power/source
  observations, timestamps, origin and config generation. Persisted fallback
  and optimistic command state are not fresh measured demand.
- [ ] Enable full hardware polling when automation is enabled even with zero
  browser connections; retain unsolicited updates and periodic resnapshot.
- [ ] Add a pure evaluator with fake clock and three-valued demand. Include
  all configured consumers, including hidden zones; false requires complete
  fresh negative evidence. Apply delay/unknown policy outside locks.
- [ ] Send bounded, revision-checked targets through a single policy owner.
  Add owner epoch/sequence fencing and receiver-monotonic lease expiry; never
  replay obsolete offline commands.
- [ ] Coordinate any control-schema migration and rollback across all
  readers/writers, including agent, CLI, supervisor, recorder and deployment
  initializer. Existing processes must not overwrite an incompatible schema.
- [ ] Show mode and reason and add explicit “Return to automatic”; gain/source
  edits do not implicitly change mode. During web outage show expired/unknown
  automation and preserve the last effective Pi intention. No second Pi-local
  hardware feed or policy evaluator is introduced.

## Automation-specific acceptance: split from S11

- [ ] Test keypad, API and out-of-band changes; zero-browser operation;
  shared-source consumers; partial/stale observations; rapid source handoff;
  debounce cancellation; manual override; restart/expired lease; and stale
  owner/sequence fencing. Confirm manual stop cannot be undone by delayed policy.
- [ ] In an explicitly approved maintenance window, verify mapped, opted-in
  outputs follow **confirmed** hardware demand, including physical zone/input
  changes and the accepted stale-state policy. The controller must be connected
  and responding; absent hardware is unverified, not a pass.
- [ ] Test web/agent/MPD restart and web outage with automation enabled, lease
  expiry, reconnection with fresh observations and preserved intentions.
  Verify manual/automatic mode and desired state persistence and degraded UX.
- [ ] Rehearse automation schema migration and rollback with compatible binaries
  and backups: disable policy, stop writers, restore and verify intentions,
  routes and gains. Assess any tests shared with the manual integration against
  the version and automation mode actually under test.
- [ ] Record focused and regression/contract results, observed routes and
  both-channel signal where required, deployment identity, limitations and
  operational handover. No automation behavior is inferred from manual S11
  acceptance.

The [integration architecture](../documentation/mpd-web-integration-architecture.md)
retains design context; this plan is the authoritative home for unresolved
automation decisions and work. A separate live approval is required before
playback changes, outages, fault injection, hardware tests or reboot.

# Playback stability and management follow-up

Status: **open** (created 2026-11-12). Tracks the remaining observations and
deferred ideas from the completed
[playback stall watchdog plan](./mpd_done_playback-stall-watchdog.md). The
watchdog implementation and its controlled 2026-11-02 live validation are
complete; closing that plan does not establish the underlying stall cause or
complete this follow-up.

## USB audio topology and observation

The Pi 4's onboard VIA hub has a shared single transaction translator for
full-/low-speed devices. The prior investigation found the C-Media audio card
already directly attached to the Pi; an external multi-TT hub or a high-speed
audio interface remain possible mitigations if problems recur. A serial
adapter was attached on 2026-11-11 at approximately 16:00 CET, changing the
USB topology and placing another full-speed device on that branch. The prior
operating guidance was to keep additional full-/low-speed peripherals off the
shared branch or use a separate external multi-TT hub; the present arrangement
is therefore an observation case, not evidence that the guidance is obsolete.

Read-only inspection on 2026-11-12 found no matching kernel audio USB
disconnect or `Not enough bandwidth` messages in the retained post-mitigation
window. This does not prove a continuous clean observation after the new
adapter was connected, and it does not prove the audio card is physically
stable or audible.

- [ ] Observe the current topology for at least one week after adapter
  attachment, through approximately **2026-11-18 16:00 CET**, provided topology
  remains unchanged. Record the actual start/end, hardware topology and journal
  coverage; restart the window after another relevant change.
- [ ] Review kernel USB disconnect, enumeration, bandwidth and device errors,
  plus audio route/readiness and watchdog events. Record outages and any gaps;
  investigate recurrence before claiming stability.
- [ ] Decide whether to retain the current setup, isolate peripherals on a
  multi-TT hub or replace the audio device. Hardware changes require separate
  approval and a new observation window.

## Management reachability

Wi-Fi power saving was disabled and persistent journaling enabled on
2026-11-02. On 2026-11-12 the Pi was reachable, power saving was off and
Ethernet was disconnected. NetworkManager recorded a Wi-Fi disconnect and
reassociation on 2026-11-11 at approximately 02:36 CET; the logs alone do not
establish external reachability or the complete outage duration. There was no
continuous external reachability monitor for the earlier observation window.

- [ ] Set up a bounded, independent reachability observation for at least one
  week, with an explicit start/end, sampling interval and recorded gaps. Decide
  where it runs and whether it can remain active for that duration.
- [ ] Correlate any outages with NetworkManager and kernel `brcmf` events;
  retain evidence and avoid interpreting missing logs as uninterrupted access.
- [ ] Decide whether the observed behavior is acceptable or whether to use
  wired Ethernet or investigate Wi-Fi further. Physical changes require
  separate approval.

## Watchdog observability

The watchdog writes bounded restart/deferral/escalation history to its local
state file, but the playback-agent snapshot and web health UI do not expose
that history. Read-only inspection on 2026-11-12 found automatic frozen-output
recoveries on November 3, 5 and 11, and one audio-stack escalation on
November 3; current playback and audio routes were healthy. Correlation with
the November 11 Wi-Fi event is not proof of cause.

- [ ] Review recurring frozen-output events and escalation, retaining relevant
  timestamps and logs; determine whether further investigation is warranted.
- [ ] Design optional, sanitized agent/web visibility for watchdog events and
  rate-limit/deferral status, compatible with the existing read API, caching,
  freshness, privacy and proxy contracts. Coordinate with
  [deployment overview](./general_idea_deployment-status-overview.md) and
  [guided diagnostics](./general_idea_guided-diagnostics.md).
- [ ] Test absent, stale, malformed and bounded event history; ensure reads do
  not cause recovery actions and never claim health merely from active units.

## Deferred recovery ideas

- **Signal-level stall detection:** reconsider only if elapsed/probe detection
  proves insufficient. Distinguish legitimately quiet content from faults and
  assess interaction with the optional
  [live level meter](./general_idea_live-level-meter.md).
- **Automatic USB hub rebind:** a manual rebind recovered the bus on
  2026-11-02, but automatic escalation is not implemented or authorized.
  Assess risks to all devices on the hub, authorization, recovery, rate
  limiting and explicit safeguards before any implementation.

Both remain deferred, not acceptance requirements for the completed watchdog
implementation; promote either to planned work only after a separate decision.

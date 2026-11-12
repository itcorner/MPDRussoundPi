# MPD playback stall watchdog and USB audio stability

Status: **done** (closed 2026-11-12). The stall watchdog (item 1) is
implemented per the
[implementation proposal](#implementation-proposal-stall-watchdog) and
[live-validated on the Pi on 2026-11-02](#live-validation-2026-11-02).
Open observation and deferred work, including items 2–4 and web observability,
are tracked in the [playback stability follow-up](./mpd_open_playback-stability-follow-up.md).
Closing this implementation plan does not assert those follow-ups are complete
or that the incident's root cause was established.

## Incident (2026-10-29)

All three outputs were silent although every service was `active` and no
unit had failed.

- `mpd_stereo_front` and `mpd_stereo_rear` accepted TCP connections on
  `127.0.0.1:6601`/`6603` but never sent the `OK MPD` greeting (accept queue
  full). The main, player, decoder and `output:pw_stereo_*` threads were all
  blocked in `futex_do_wait`. The processes had been running for 11.9 days.
- `mpd_stereo_centerlfe` still answered and reported `state: play`, but
  `elapsed` did not advance and its sink monitor carried only zero samples.
  Its main thread was free; player, decoder and output threads were blocked.
- The supervisor logged `Reconciliation failed for stereo_rear: MPD read
  failed` from 02:33 onward, and the agent logged `mpd_unavailable`. Both
  only reconnect and retry; neither restarts a hung MPD, so the stall lasted
  until a manual restart.
- PipeWire logged repeated `spa.audioconvert ... out of buffers` on all three
  converters (for example 2026-10-28 and 2026-10-29 14:18).
- The C-Media USB card (`0d8c:0102`, full speed, port `1-1.1` behind VIA hub
  `2109:3431`) dropped off the bus on 2026-10-23 07:55 and again on
  2026-10-29 20:15, each time with hundreds of `Not enough bandwidth`,
  `usb_set_interface failed` and `current rate 30464` kernel messages. This is
  the same failure class as in
  [USB audio recovery](./usb_done_audio-recovery.md#management-restored-usb-bandwidth-failure-2124-cest).
- DNS, egress filter, internet, disk, memory and throttling were healthy.

Recovery: `systemctl --user restart mpd_stereo_front mpd_stereo_centerlfe
mpd_stereo_rear`. Stopping took about two minutes (hung processes did not
exit cleanly). The supervisor then restored all intentions; centerlfe and rear
came back immediately and front after its 30 s retry backoff. Sink monitor
peaks afterwards: front 16121, centerlfe 32759, rear 30306.

Root cause is not proven. Most likely: a USB audio or PipeWire stall
blocks MPD's pulse output, and MPD 0.24 does not recover from it. MPD
itself logged nothing.

## Original items and historical investigation

### 1. Stall watchdog in the playback supervisor

Implemented 2026-11-02 following the
[implementation proposal](#implementation-proposal-stall-watchdog) below;
deployed and [live-validated](#live-validation-2026-11-02) the same day.

Detect and restart an MPD instance that is no longer playing although its
intention is `playing`:

- **Unresponsive:** connect or greeting/command timeout on the instance's
  port for longer than a threshold (for example 3 consecutive checks over
  ≥ 2 minutes).
- **Frozen:** `state: play` while `elapsed` does not advance for longer
  than a threshold (for example 2 minutes). Exclude legitimate cases: stopped
  or paused intentions, buffering right after start or a station change, and
  the existing retry backoff.
- **Action:** restart only the affected `mpd_<output>.service` through
  `systemctl --user`, then let normal reconciliation restore station and gain.
  Apply a restart rate limit (for example at most 3 per 30 minutes per output)
  and record each restart. Agent snapshot and web health visibility remain in
  the [follow-up](./mpd_open_playback-stability-follow-up.md#watchdog-observability).
- **Audio-path gate:** do not restart while shared audio readiness reports the
  USB card missing; restart once it is back, as hung instances do not resume
  on their own.
- Consider `TimeoutStopSec` on the MPD units so a hung instance is killed
  quickly instead of after the default stop timeout.

Validation: unit tests with a fake MPD that (a) stops answering and (b)
reports `play` with a frozen `elapsed`; assert one restart, rate limit,
no restart for paused/stopped intentions or during audio-not-ready. Live: a
controlled `kill -STOP` of one MPD on the Pi recovers without touching the
other outputs.

### 2. Optional: signal-level check

Deferred on 2026-11-02; revisit only if item 1 proves insufficient.

Read the sink monitor peak (as done manually during the incident) to detect
silent output while MPD reports progress. Only if item 1 proves insufficient;
needs a quiet-content threshold so silence in a stream is not treated as a
fault.

### 3. USB topology (hardware, no code)

Original assumption: connect the C-Media card directly to a Pi USB port
instead of through the VIA hub, or use a powered hub, and keep additional
peripherals off that branch. Re-evaluate the kernel log for `Not enough
bandwidth` and USB disconnects over at least one week afterwards.

**Premise corrected (2026-11-02):** the card already is connected directly to
the Pi. The host is a Raspberry Pi 4 Model B, and the `2109:3431` VIA Labs
hub from the incident log is the Pi 4's **onboard** hub inside its VL805 USB
controller — every USB-A socket (USB 2.0 and 3.0 alike) leads through it, so
no better-suited port exists; a 12 Mbit/s device always enumerates behind
this hub. The only electrically separate port is the USB-C connector (DWC2
controller), which is the power input and not practical.

Measured facts (2026-11-02, remote inspection):

- The onboard hub reports **Single TT** (`bDeviceProtocol 1`): all full- and
  low-speed devices on any Pi port share one transaction-translator
  bandwidth budget with the full-speed audio card. This matches the
  2026-10-29 failures coinciding with added keyboard/mouse receivers.
  Operating rule: never attach full-/low-speed peripherals directly to the
  Pi while audio plays; use a separate external multi-TT hub for them.
- Power is clean: zero over-current kernel events, `throttled=0x0`.
- The 1294 `Not enough bandwidth` messages in the current boot cluster only
  around the card's own enumeration storms (2026-10-30, 2026-11-02 10:18)
  with no peripherals attached — a symptom of failed re-enumeration (stale
  TT reservations), not of actual contention.

Revised assessment: the suspect is the card (CM106 clone) or its
cable/connector wedging, not the topology. On 2026-11-02 the card was off
the bus from 10:15 (`error -32`/`-71`, kernel port power cycle
unsuccessful) and recovered remotely without a reboot by rebinding the
onboard hub: `echo 1-1 > /sys/bus/usb/drivers/usb/unbind`, then `bind`.

Revised options, by value:

1. Status quo plus discipline: card alone on the Pi, peripherals only via an
   external multi-TT hub when needed; stall watchdog plus the proven hub
   rebind as remote recovery. In place since 2026-11-02.
2. Powered external **multi-TT** hub for the card — the inverse of the
   original assumption, but on a Pi 4 it gives the card its own TT and
   isolated power; the hub itself becomes a new failure mode.
3. Replace the card with a high-speed (480 Mbit/s) USB audio interface,
   eliminating the TT path entirely; the structural fix if wedges recur.
4. Possible software follow-up: wire the hub rebind into the watchdog's
   escalation path, since it recovers this failure class without a reboot.

Keep the original validation: re-evaluate the kernel log for `Not enough
bandwidth` and USB disconnects over at least one week after any change.

### 4. Management reachability

During verification on 2026-10-29 the Pi was unreachable (ping and SSH) for
about a minute without a reboot; Wi-Fi scan errors (`brcmf_run_escan: error
(-52)`) were logged on 2026-10-26. Investigate whether wired Ethernet or Wi-Fi
power-save settings are needed.

**Findings and mitigation (2026-11-02):** management is Wi-Fi-only
(NetworkManager, signal −59 dBm — link quality is not the problem); `eth0`
has no cable. Wi-Fi power save was enabled (brcmfmac driver default;
NetworkManager `powersave: 0 (default)` does not override it), a known cause
of brief stalls and scan errors on Pi brcmfmac. The 2026-10-26 `brcmf` log
entries had already rotated out of the volatile journal, so past windows
could not be diagnosed.

Applied on 2026-11-02 (both verified, connectivity uninterrupted):

- Wi-Fi power save disabled: runtime `iw dev wlan0 set power_save off` plus
  persistent `/etc/NetworkManager/conf.d/wifi-powersave.conf` with
  `wifi.powersave = 2`.
- Persistent journald with a 200M cap
  (`/etc/systemd/journald.conf.d/persistent.conf`, `Storage=persistent`) so
  the next unreachability window leaves diagnosable kernel/NetworkManager
  logs.

Follow-up: wired Ethernet remains a possible structural fix (physical task). The Pi 4's
Ethernet is native GbE (not USB-attached), so it cannot interact with the
item 3 USB/audio path; NetworkManager prefers the wired link automatically
once a cable is plugged in. The one-week reachability validation and its results
are tracked in the [follow-up plan](./mpd_open_playback-stability-follow-up.md#management-reachability).

## Implementation proposal: stall watchdog

Recorded 2026-11-02. Covers item 1 (and the `TimeoutStopSec` note);
the remaining items are tracked in the follow-up plan.

### Design decisions

| Decision | Choice |
|---|---|
| Placement | Inside the supervisor process, driven from the existing 2 s loop; no extra thread or service |
| Detection source | Dedicated bounded probe per output (independent connection), not reuse of reconcile failures, which conflate audio/TuneIn errors with MPD hangs |
| Thresholds | Defaults in code, optional overrides in an admin-owned `watchdog` section of `config.json` |
| Rate limit exhausted | Escalate once to an audio-stack restart (PipeWire + all MPD outputs), itself rate-limited; then hold |
| Observability | Watchdog writes `watchdog.json` (restart/escalation history) now; agent snapshot and web health consumption is a separate later step |
| `TimeoutStopSec` | 2 min on each `mpd_<id>.service` (decided 2026-11-02; note: the systemd user-manager default is 90 s, so this bounds the hang observed in the incident rather than shortening the kill deadline) |

### Components

- **`lib/playbackwatchdog.py` (new):** a `StallWatchdog` class that owns all
  per-output tracking state. Probe, restart, escalation, audio gate and clock
  are injected callables so unit tests need no real MPD, systemd or audio
  stack. The default wiring probes through
  [`MPDBackend.observation_connection`](../../mpdrussoundpi/russound/lib/mpdbackend.py)
  (3 s timeout; catches the incident's accept-without-greeting hang) and
  reads `state`, `elapsed` and `songid` from `status()`.
- **[Supervisor](../../mpdrussoundpi/russound/mpdsupervisor.py):**
  instantiates the watchdog and calls `watchdog.tick(streams)` once per loop
  iteration with the outputs it already lists (including the saved
  intention). The watchdog self-schedules probes (default every 10 s per
  output), so the 2 s loop cadence is unchanged. After a watchdog restart the
  supervisor drops its retry-backoff entry for that output so reconciliation
  restores station and gain immediately.
- **[Config parsing](../../mpdrussoundpi/russound/lib/outputconfig.py):** an
  optional top-level `watchdog` object with strictly validated fields;
  unknown fields are rejected like output fields are today.
- **[mpdsetup.py](../../mpdrussoundpi/russound/mpdsetup.py):** add
  `TimeoutStopSec=120` to each generated `mpd_<id>.service` so a hung
  instance is killed deterministically.

### Detection

Evaluated only while the saved intention is `play`; any other intention
resets all tracking for that output. A stopped or paused player with a
`play` intention stays the reconciler's job, not the watchdog's.

- **Unresponsive:** a probe failure is a connect, greeting or command error
  or timeout. Trip when `unresponsive_probes` (default 3) consecutive probes
  failed **and** the first failure is at least `stall_threshold_s` (default
  120) ago. Any successful probe resets the counter.
- **Frozen:** probe returns `state: play`. Keep a baseline of
  (`songid`, `elapsed`, monotonic time); reset it whenever `songid` changes
  or `elapsed` advances. Trip when the baseline is `stall_threshold_s` old
  and `elapsed` never advanced. Buffering after a start or station change is
  excluded structurally: a fresh start resets the baseline (new `songid` or
  reconnect), and the threshold itself tolerates up to 2 min of buffering.
  A missing `elapsed` during `play` counts as not advancing.
- **Grace:** after a watchdog restart, probing for that output is suspended
  for `restart_grace_s` (default 90) to cover unit start, MPD boot and
  stream buffering. Supervisor start needs no extra grace: baselines are
  created on the first probe, so no trip can occur before a full threshold
  has passed.

All timing uses `time.monotonic()`; wall-clock timestamps appear only in the
state file for display.

### Action, rate limit and escalation

1. **Restart:** `systemctl --user restart mpd_<id>.service`, bounded by a
   subprocess timeout of stop deadline + start margin (120 s + 60 s). The
   supervisor loop blocks during the restart; with one hung instance that is
   an acceptable pause and the other outputs keep playing unattended.
2. **Rate limit:** a sliding window per output, `restart_limit` (default 3)
   restarts per `restart_window_s` (default 1800). While exhausted, the
   output is marked rate-limited in the state file and an error is logged.
3. **Escalation:** when an output trips again while rate-limited (restarts
   are not helping), restart the audio stack once — `pipewire.service
   pipewire-pulse.service wireplumber.service`, then every
   `mpd_<output>.service` — and clear all per-output restart windows.
   Escalation has its own limit of one per `escalation_window_s` (default
   3600). If the stall persists after an escalation and all limits are
   exhausted, the watchdog holds and logs periodically; recovery is manual,
   matching the incident runbook.
4. **Audio-path gate:** immediately before any restart or escalation, run
   the shared readiness check
   ([`audio_ready`](../../mpdrussoundpi/russound/lib/audioready.py), non
   enforcing). If the USB card is missing the action is deferred and the
   stall stays latched; the action fires on the first gate pass after the
   card returns, because hung instances do not resume on their own. The gate
   runs only when an action is due, so its `pactl`/`pw-dump` cost is not on
   the probe path.

### Configuration

Optional, admin-owned, all fields optional with the defaults shown:

```json
"watchdog": {
  "enabled": true,
  "probe_interval_s": 10,
  "stall_threshold_s": 120,
  "unresponsive_probes": 3,
  "restart_limit": 3,
  "restart_window_s": 1800,
  "restart_grace_s": 90,
  "escalation_window_s": 3600
}
```

### State file

`$XDG_STATE_HOME/mpdrussoundpi/watchdog.json`, written atomically via
[`atomicjson`](../../mpdrussoundpi/russound/lib/atomicjson.py) on every
watchdog action or deferral transition (not on healthy probes). History is
bounded (last 20 entries per list). Consumption by the playback agent
snapshot and the web health status is a follow-up item.

```json
{
  "outputs": {
    "stereo_front": {
      "restarts": [{"at": "2026-11-02T00:00:00Z", "reason": "frozen"}],
      "deferred": null,
      "rate_limited": false
    }
  },
  "escalations": [{"at": "2026-11-02T00:10:00Z", "output": "stereo_front"}]
}
```

### Implementation steps

1. `watchdog` section parsing and validation in
   [outputconfig.py](../../mpdrussoundpi/russound/lib/outputconfig.py);
   tests in [test_outputconfig.py](../../tests/test_outputconfig.py). Done
   2026-11-02.
2. `lib/playbackwatchdog.py` with `StallWatchdog` (tracking, rate limits,
   escalation, gate, state file); new `tests/test_playbackwatchdog.py` with
   a fake clock and scripted probe results: one restart for unresponsive and
   for frozen-elapsed, baseline reset on `songid` change and advancing
   `elapsed`, rate limit, escalation and its window, no action for paused or
   stopped intentions, deferral while audio is not ready and firing once it
   is, grace after restart. Done 2026-11-02.
3. Supervisor wiring (instantiate, `tick`, backoff clearing) in
   [mpdsupervisor.py](../../mpdrussoundpi/russound/mpdsupervisor.py); tests
   in [test_mpdsupervisor.py](../../tests/test_mpdsupervisor.py). Done
   2026-11-02.
4. `TimeoutStopSec=120` in
   [mpdsetup.py](../../mpdrussoundpi/russound/mpdsetup.py) unit generation;
   tests in [test_mpdsetup.py](../../tests/test_mpdsetup.py). Done
   2026-11-02.
5. Documentation: supervisor row in
   [software-architecture.md](../documentation/software-architecture.md),
   headless-deployment troubleshooting notes, this plan's status line and
   the [docs index](../README.md). Done 2026-11-02.
6. Live validation on the Pi: `kill -STOP` one MPD; the watchdog restarts
   only that output within ~2–4 min and the others keep playing. Record the
   acceptance here and rename the plan when done. Done 2026-11-02, see
   below.

### Implementation notes (2026-11-02)

Implemented as proposed, with one addition: failed restart attempts and
audio-gate deferrals back off (`restart_grace_s` and `probe_interval_s`
respectively) instead of retrying every 2 s supervisor iteration, so a
failing `systemctl` cannot burn the restart window within seconds and the
`pactl`/`pw-dump` gate stays off the hot loop. A watchdog restart also still
counts toward the rate limit when `systemctl` itself fails. The full test
suite passes (586 tests). Deployment note: `mpd_<id>.service` units must be
regenerated (MPD setup play) to pick up `TimeoutStopSec=120`.

### Live validation (2026-11-02)

Deployed as an explicitly staged local deployment: the four changed files
(`lib/outputconfig.py`, `lib/playbackwatchdog.py`, `mpdsupervisor.py`,
`mpdsetup.py`) were copied onto the Pi's clone, units regenerated with
`mpdsetup.py --install` (verified `TimeoutStopSec=120` in
`mpd_stereo_front.service`), instances restarted, `mpdsupervisor.py --once`
passed and the supervisor was restarted. The staging was converted into a
normal git deployment the same evening: PR #9 was merged into `develop`
(`ebd529d`) after CI passed, every staged file on the Pi was verified
byte-identical to the merged tree, and the Pi clone was switched to a clean
checkout of `origin/develop` at that commit with services untouched and
healthy afterwards.

**Incidental outage found during deployment:** the C-Media card had been off
the bus since 10:15 (`device descriptor read/64, error -32`, `device not
accepting address`, error `-71`, kernel port power cycle unsuccessful); all
three outputs were silent while units were `active` — the incident class
this plan addresses. Remote recovery without a reboot: unbind/rebind of the
VIA hub (`echo 1-1 > /sys/bus/usb/drivers/usb/unbind`, then `bind`)
re-enumerated the card at 20:50 and the supervisor restored all outputs
within 35 s. This again implicates the hub branch (item 3) and documents a
remote recovery path short of a reboot.

**Watchdog acceptance test:** with all three outputs playing,
`kill -STOP` was sent to `mpd_stereo_front` (PID 2792700) at 20:53:11.

- 20:55:29 — `Watchdog: stereo_front is unresponsive (11 failed probes
  over 121s)`
- 20:55:31 — `Watchdog: restarted stereo_front (unresponsive)`; the
  SIGSTOPped process stopped immediately (systemd's SIGCONT-after-SIGTERM),
  so the restart took ~1.5 s
- 20:55:33 — supervisor reconciled immediately (watchdog cleared the retry
  backoff); front playing again ≈ 2 min 22 s after the fault
- `mpd_stereo_centerlfe` (PID 2792690) and `mpd_stereo_rear` (PID 2792710)
  kept their PIDs and played continuously throughout
- `~/.local/state/mpdrussoundpi/watchdog.json` recorded exactly one restart
  (`reason: unresponsive`), no deferrals, no escalations
- Stability check 3 min after recovery: no further watchdog actions, all
  elapsed counters advancing

Accepted: detection threshold, single-output restart, backoff clearing,
non-interference with healthy outputs and state-file observability all
behaved as designed.

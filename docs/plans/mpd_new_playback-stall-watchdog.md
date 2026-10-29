# MPD playback stall watchdog and USB audio stability

Status: planned, not started (recorded 2026-10-29). Long-term stability
improvement; nothing below is implemented or deployed.

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

## Open items

### 1. Stall watchdog in the playback supervisor

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
  and log/observe each restart so repeated restarts are visible in the
  agent snapshot and the web health status.
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

Read the sink monitor peak (as done manually during the incident) to detect
silent output while MPD reports progress. Only if item 1 proves insufficient;
needs a quiet-content threshold so silence in a stream is not treated as a
fault.

### 3. USB topology (hardware, no code)

Connect the C-Media card directly to a Pi USB port instead of through the VIA
hub, or use a powered hub, and keep additional peripherals off that branch.
Re-evaluate the kernel log for `Not enough bandwidth` and USB disconnects
over at least one week afterwards.

### 4. Management reachability

During verification on 2026-10-29 the Pi was unreachable (ping and SSH) for
about a minute without a reboot; Wi-Fi scan errors (`brcmf_run_escan: error
(-52)`) were logged on 2026-10-26. Investigate whether wired Ethernet or Wi-Fi
power-save settings are needed.

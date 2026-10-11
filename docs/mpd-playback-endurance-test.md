# Read-only playback endurance and USB reconnect recorder

[playbackaudit.py](../mpdrussoundpi/russound/playbackaudit.py) observes the existing
recovery supervisor. It never calls play/stop, moves clients, changes gains,
selects profiles or starts/restarts audio/MPD services. Monitor recordings
create temporary capture clients but do not store programme audio.

## Start a test

Run as the audio-owning user using its Python environment (`python-mpd2`).
FFmpeg with Pulse input, pactl, systemctl, journalctl, lsusb and Git must be
available. A private `XDG_RUNTIME_DIR` is required for the recorder's own
nonblocking lock. The production operational configuration is used below.

From the script directory on the Pi:

```sh
CONFIG="$HOME/.config/mpdrussoundpi/config.json"
RUN="$HOME/.local/state/mpdrussoundpi/audits/$(date +%Y%m%d-%H%M%S)"
PYTHON="$HOME/russound-venv/bin/python3"

# Optional one-pass smoke test; use a separate output directory.
"$PYTHON" playbackaudit.py "$CONFIG" --output "${RUN}-smoke" --once

# Generate the user unit. This does NOT enable/start it.
"$PYTHON" playbackaudit.py "$CONFIG" --output "$RUN" --hours 72 --install-service
systemctl --user enable --now playback_audit.service
```

The recorder runs independently of SSH. Lingering must already be enabled.
The audit service does not require/bind to audio services, so it continues
recording their disappearance. On process failure systemd retries after 30
seconds, with five starts per five minutes. The duration starts on the first
actual recorder execution, not service generation.

On restart/reboot the same run directory resumes with its original wall-clock
deadline, journal cursors, counters and open incidents. Sampling downtime is
not magically recovered; `recorder_resumed` includes the last summary time.
An expired run exits successfully rather than starting another 72 hours.
Use a new directory for a new run. Bad metadata/configuration is an explicit
failure, not silently reset.

```sh
systemctl --user status playback_audit.service
journalctl --user -u playback_audit.service -n 40 --no-pager
cat "$RUN/summary.json"

# End early, or retire the enabled unit after completion:
systemctl --user disable --now playback_audit.service
```

## Sampling and retained evidence

| Cadence | Evidence |
|---|---|
| Startup | Config snapshot/hash, Git revision, USB topology, boot ID |
| 10 seconds | Saved intentions; MPD status/errors, elapsed progress, queue length, current URL |
| 30 seconds | Hardware/profile/sink readiness without enforcement; card inventory; actual routes for intended-playing streams; both-channel raw gain units; MPD PID/state/restart/resource counters |
| 30 seconds | System journal entries for the audio user's UID (including supervisor/audio/MPD) and accessible kernel events, cursor-based |
| 5 minutes | Two-second stereo monitor RMS per stream; memory, load, free disk, thermal data and Pi throttling flags when supported |

Cadences are scheduling targets, not hard real-time guarantees. Calls have
timeouts (MPD 3 seconds, ordinary commands 5, pactl 2, signal capture 12).
Samples are sequential: outages can delay other streams. Each sample group
records lateness; measurement errors are retained. Recovery times are measured
between observations, including sampling latency, not exact audible outage
duration. Fresh journal batches cap at 1000 entries and explicitly flag possible
loss when the cap is reached. Permission/no-journal warnings count as
measurement errors; fix access before relying on journal coverage. The service
does not automatically grant privileges or switch to root.
The system journal is queried because musicpi previously had no separate user
journal files. Access normally requires membership in `adm` or
`systemd-journal`; a newly granted group requires a refreshed user manager
session. Entries from other processes belonging to the same user can also
appear in the private audit logs.

Files:
- `run.json`: immutable baseline/configuration and persistent deadline.
- `observations.jsonl` and numbered backups: UTC/wall/monotonic timestamps,
  measurements, errors, incidents, recovered transitions and lifecycle events.
- `summary.json`: current status, attempted sample counts, problem sample
  counts, open incidents and journal cursors. Counts are not unique failures.

Observations rotate at 10 MiB with ten backups (approximately 110 MiB total,
plus small metadata). Rotation discards the oldest detailed records; archive
them separately if full evidence is required or unusually noisy logs occur.
Failed log writes stop the recorder instead of silently claiming coverage.
On-disk files include configured/current stream URLs, which may contain
credentials or tokens; keep them private and redact before sharing.

## Interpretation

Anomalies include unintended playback, intended-playing output errors,
unconfigured URL, queue length over one, elapsed time not advancing,
incorrect route and active mixer gain mismatch. Existing multi-entry queues
are flagged rather than modified; record that baseline before testing.

Inactive sinks are observed but not required to reflect configured gain.
A paused intention may be observed stopped after queue loss; it must remain
silent. Missing intentions are explicitly flagged. Missing cards/sinks and
inaccessible MPD clients produce measurement errors while other checks proceed.

RMS below -80 dB on either intended-playing channel is **suspected silence**,
not a proven failure: programmes can be quiet. Signal above -80 dB for a
stopped/paused intention is flagged as unexpected. These samples cannot prove
physical jack mapping, analog voltage, tiny startup transients or every short
dropout. Use continuous/external metering for those requirements.

An exit status of zero means the recorder completed its work, **not** that
playback passed. Review incidents, measurement errors, sample coverage and
journals; investigate signal flags. No automatic pass/fail certification is
produced.

## Physical reconnect procedure

Keep the recorder and recovery supervisor running. In an approved maintenance
window, note exact unplug/replug timestamps (for example in a separate
`usb-actions.log` in the run directory), then unplug only the sound card.
Test five 10-second disconnections followed by one two-minute disconnection,
reconnecting to the same USB port each time. Do not manually repair playback.

Repeat with one deliberately stopped and one paused stream, using the shared
controller rather than raw `mpc`, so saved intentions are updated. Verify the
remaining intended-playing stream recovers with 5.1, correct actual route,
calibrated gain and signal; stopped/paused streams must stay silent. Confirm
physical channel mapping by listening/measurement. Restore original intentions
afterward.

A suggested acceptance target is recovery within 120 seconds **after**
configured hardware/sinks become available. This is a test target, not a
guarantee. Use the observations and kernel/journal timestamps to separate USB
enumeration delay from playback recovery. No automated USB/network disruption,
login suppression, reboot or playback restoration is performed by this script.

## Validation

[Recorder tests](../tests/test_playbackaudit.py) exercise read-only MPD calls,
state/progress/route/gain/signal anomalies, error isolation, persisted deadlines
and counters, journal cursors/overflow, bounded rotation and log-write failures,
signal duration parsing, expired runs, lock contention and independent unit
generation. Commands/protocol effects are mocked; these tests do not certify
72-hour playback or physical reconnect.

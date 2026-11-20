# Headless MPD deployment and recovery

The [setup playbook](../../ansible/playbooks/setup-musicpi.yml) stages a
revision-pinned audio source tree and isolated candidate virtual environment.
It does not activate or modify the current checkout, audio configuration,
native gateway, MPD/PipeWire services or playback state.

Legacy in-place audio activation is intentionally blocked in this playbook.
An approved native transaction must supply verified recovery and explicit
activation before the staged candidate can replace an active audio runtime.
The [deployment guide](./playback-agent-deployment.md) describes the related
component boundaries.

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/setup-musicpi.yml \
  --tags stage -e mpd_russoundpi_revision=<full-40-or-64-character-commit>
```

The play checks out the exact requested commit into a new private candidate
directory, installs only its bounded audio dependencies into a separate
virtual environment, and validates the existing operational config if present.
The candidate parent and each revision directory are owner-private mode `0700`;
the play atomically reserves a new revision directory and refuses to reuse it.
Inspect any previous candidate separately.
It reports the candidate revision and explicitly reports `activation:
not-performed`. `--tags init,new` is the retired in-place path and must not be
used for native activation. No audio service is stopped, restarted, enabled,
or disabled by the `stage` tag.

Operational configuration is not copied or rewritten by candidate staging.
The existing runtime state `~/.local/state/mpdrussoundpi/playback.json` is
also untouched.

## Configuration: `config.json` (admin-owned)

`config.json` describes the hardware, the MPD instances (outputs) and the seed
station list. The agent and supervisor only read it, so `config_revision`
changes only when an admin edits it. The
[seed configuration](../../mpdrussoundpi/russound/config.json) shows every option:

```json
{
  "outputmappers": { "audio_devices": [ "..." ] },
  "outputs": [
    { "id": "stereo_front", "label": "Stereo Front", "port": 6601, "sink": "stereo_front",
      "default_station": "radio886", "default_gain_db": -7.1 },
    { "id": "stereo_centerlfe", "label": "Stereo CenterLFE", "port": 6602, "sink": "stereo_centerlfe" },
    { "id": "stereo_rear", "label": "Stereo Rear", "port": 6603, "sink": "stereo_rear",
      "default_station": "tunein:s8007", "default_gain_db": -0.6 }
  ],
  "stations": [
    { "key": "radio886", "name": "Radio 88.6", "type": "url",
      "url": "https://edge85.streamonkey.net/radio886-onair/stream/mp3?aggregator=886Website" },
    { "key": "tunein:s8235", "name": "ORF Radio FM4", "type": "tunein", "station_id": "s8235" },
    { "key": "tunein:s8007", "name": "ORF Hitradio OE3", "type": "tunein", "station_id": "s8007" }
  ]
}
```

Outputs:

- `id` (`[a-z][a-z0-9_-]{0,63}`) is the only identity. It names the MPD unit
  (`mpd_<id>.service`) and its config (`~/.config/mpd/mpd_<id>.conf`), and it is
  the output ID in the agent and dashboard APIs. `label` is display text only.
- `port` and `sink` must be unique; `sink` must exist in `outputmappers`. MPD
  always listens on `127.0.0.1` only.
- `default_station` (optional) names a `stations` key. It applies while the
  output has no runtime selection yet: the output starts on that station,
  intended playing. Without it the output starts with no station, stopped and
  silent; the dashboard shows Play disabled until a station is selected.
- `default_gain_db` (optional, `<= 0`, default `0`) seeds the runtime gain
  the same way.

Stations (at most 20) seed the dashboard favorites:

- `type: "url"`: a direct HTTP(S) stream. These can only come from
  `config.json`; the dashboard never sends or receives stream URLs.
- `type: "tunein"`: a TuneIn station ID (`s` + digits). The key must be
  `tunein:<station_id>`. The backend resolves the playable stream when it
  starts the station and re-resolves whenever the cached URL is not playing.

An optional top-level `watchdog` object tunes the supervisor's stall
watchdog; every field is optional and unknown fields are rejected. Defaults:

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

Only the schema above is supported; there is no compatibility/migration mode.

## Runtime state: `playback.json` (agent-owned)

`~/.local/state/mpdrussoundpi/playback.json` holds everything that changes at
run time: the favorites list and, per output ID, the selected station (a copy,
so removing a favorite never breaks a playing output), the cached resolved
URL, the managed MPD song ID, the gain and the play/pause/stop intention. The
backend writes it atomically under the control lock; `control_revision` is
derived from it. Missing entries take the `config.json` defaults, unknown
output IDs are dropped on the next write, and a missing `favorites` key is
initialized from `stations`. Deleting the file resets every output to its
defaults.

`streamlevels.py` measures each output's current (or default) station and
`--output-config` writes the recommendations as `default_gain_db`.

### Isolated audio dependencies

The audio/control/supervisor dependency manifest is
[`requirements-audio.txt`](../../mpdrussoundpi/russound/requirements-audio.txt)
(`python-mpd2>=3.1,<4`), separate from Flask/Waitress and Ansible.
The setup play installs this bounded manifest without an unconditional upgrade.
It still targets its configured environment; **do not run it against a working
installation merely to prepare an environment**.

For a candidate, choose a new unused path and leave the active candidate and its
service units untouched. From a reviewed checkout on the intended host:

```sh
test ! -e "$HOME/russound-audio-candidate" &&
python3 -m venv "$HOME/russound-audio-candidate" &&
"$HOME/russound-audio-candidate/bin/python" -m pip install \
  -r mpdrussoundpi/russound/requirements-audio.txt &&
"$HOME/russound-audio-candidate/bin/python" -m pip check &&
"$HOME/russound-audio-candidate/bin/python" -I -c 'import mpd; print("Audio dependency import OK")'
```

Stop if any command fails; do not proceed using a pre-existing candidate.
Also check the target Python/system packages, repository imports, generated
audio assets and existing configuration before requesting a maintenance-window
switch. Do not run audio setup, change units or restart playback during this
preparation. Keep the old environment and backed-up units/config/state for an
explicitly approved switch/recovery. Local clean-environment import tests are
not Pi hardware acceptance.

Use the operational configuration for **all** control and calibration commands:

Run the following commands on the Pi as the audio user. `AUDIO_BIN` is the `bin` directory of the active audio candidate
(`~/.local/share/mpdrussoundpi/audio-candidates/<revision>/venv/bin`), read
from the supervisor unit so it always matches the running revision. The
commands are console scripts of the installed `mpdrussoundpi` package.

```sh
AUDIO_BIN=$(dirname "$(systemctl --user show -p ExecStart --value mpd_playback_supervisor.service | sed -n 's/.*path=\([^ ;]*\).*/\1/p')")
"$AUDIO_BIN/mpdctrl" --stop --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
"$AUDIO_BIN/mpdctrl" --pause --output stereo_front ~/.config/mpdrussoundpi/config.json
"$AUDIO_BIN/mpdctrl" --start ~/.config/mpdrussoundpi/config.json
"$AUDIO_BIN/mpdctrl" --check-route ~/.config/mpdrussoundpi/config.json
```

`--start` without `--output` starts every output that has a station.

Generated MPD units want/order after the audio services. Their readiness
command actually queries cards/sinks and enforces the configured 5.1 profile,
retrying for 30 seconds. Daemons restart on failure after five seconds, with
ten starts per minute. Missing hardware is a visible failure, not a successful
start. The supervisor makes later attempts with per-output backoff, so an
absent device/network does not permanently consume the restart budget.

The supervisor honors saved intentions, never treating an audio-induced pause
as a user pause. For playing outputs it restores the selected station's URL as the
single managed queue entry, clears errors, starts with the software
mixer muted, verifies routes, reapplies/read-verifies absolute gains, and
unmutes to software volume 100. It verifies final playback state. Stopped and
paused intentions remain silent after reboot; an empty paused queue is left
stopped. This is playback recovery, not retired Russound power polling.

The supervisor also runs a stall watchdog for outputs intended to play: a
bounded probe (default every 10 s) detects an MPD that no longer answers or
reports `play` with a frozen `elapsed` (default threshold 2 minutes), then
restarts only that `mpd_<id>.service` — at most 3 times per 30 minutes per
output. If restarts do not help, it restarts the PipeWire stack and all
outputs once per hour, then holds. Restarts are deferred while the shared
audio readiness check reports the USB card missing and fire once it is back.
Restart and escalation history is written to
`~/.local/state/mpdrussoundpi/watchdog.json`; generated MPD units carry
`TimeoutStopSec=120` so a hung instance stops deterministically. Thresholds
are tunable through the optional `watchdog` section in `config.json`; design
background is in the
[stall watchdog plan](../plans/mpd_done_playback-stall-watchdog.md).

The former `init,new` in-place deployment sequence has been disabled because
it stopped services and rewrote active unit/configuration paths without the
shared reservation, off-host backup and recovery contract. Candidate staging
does not run `mpdsetup.py`, `audiosetup.py` or the supervisor. For any
previously provisioned host, do not use `mpd_russoundpi_start_playback` as an
activation control; a separately reviewed native activation runner is still
required.

```sh
loginctl show-user "$USER" -p Linger
systemctl --user status mpd_playback_supervisor.service
journalctl --user -u mpd_playback_supervisor.service -n 60 --no-pager
"$AUDIO_BIN/mpd-supervisor" --once ~/.config/mpdrussoundpi/config.json
```

`--once` is useful while the supervisor is stopped; concurrent mutations use
one nonblocking lock and can fail with contention. API/CLI errors remain
explicit, including a failure to save an intention or gain.

## Removing managed audio configuration

Cleanup is separate from MPD service removal. Stop the playback supervisor and
MPD instances first during an approved maintenance window; otherwise they will
keep trying to restore outputs whose virtual sinks you are removing:

```sh
"$AUDIO_BIN/mpd-setup" --stop ~/.config/mpdrussoundpi/config.json
"$AUDIO_BIN/audio-setup" --cleanup --audio-processor pipewire
```

The MPD stop command selects exact output IDs from the supplied config and
requires MPDRussoundPi ownership markers and matching effective systemd
fragment/drop-in paths. Legacy or overridden units are refused for explicit
migration; unrelated/stale matching units are reported and left unchanged.

### Migrating units written before ownership markers

Units, MPD configs and egress drop-ins written by releases before the ownership
markers start without a `# Managed by MPDRussoundPi; …` line, and older setups
may have left `~/.config/mpd` or the files group-writable. `mpd-setup` and the
egress playbook then refuse with "Unsafe MPD directory" or "Unowned, unsafe or
legacy file". Migrate only after proving the files are generator output:

1. Back up `~/.config/systemd/user` and `~/.config/mpd`.
2. Generate the expected files into a temporary directory with the target
   environment's code (`mpdsetup.create_mpd_config`, `create_mpd_systemd_service`,
   `create_mpd_metaservice` and `create_supervisor_service` with
   `SYSTEMD_USER_CONFIG_DIR` and `MPD_CONFIG_DIR` pointed at it) and diff them
   against the live files. Proceed only if the differences are the marker line,
   the command paths and the temporary config path.
3. Prepend `# Managed by MPDRussoundPi; component=mpd; schema=1` to the five
   units and the MPD configs, and
   `# Managed by MPDRussoundPi; component=egress; schema=1` to each
   `50-egress.conf`; remove group/other write from the files, the drop-in
   directories and `~/.config/mpd`; run `systemctl --user daemon-reload`.
4. Confirm with the read-only preflight
   `python -m mpdrussoundpi.russound.lib.mpdownership --config <config>`, then
   run `mpd-setup --install <config>`.

Anything else in the diff (other settings, extra drop-ins, overridden fragment
paths) needs separate inspection; do not add markers to files you did not
prove equivalent.

Cleanup requires no JSON file or running audio server. Omitting
`--audio-processor` checks both backends. It uses the audio setup lock and
removes only the known generated PipeWire/WirePlumber fragments whose ownership
header matches. Unowned files or symlinks cause an explicit error before any
configuration is removed. Unrelated fragments, MPD configuration, persisted
gains and playback intentions are not deleted.

PulseAudio installation now writes a separate managed fragment and adds a
bounded include block to the existing user `default.pa`, preserving its original
bytes. Updates use a flushed/fsynced temporary file and atomic replacement,
preserving file permissions rather than truncating the user's file in place.
With no user file, it creates a wrapper that includes the distribution
defaults. Repeated installs do not duplicate the block. Cleanup removes only
the managed include and fragment; it deletes the wrapper only if it has no
additional user content. Modified/malformed include blocks are refused.

Affected running user audio services are restarted with checked
`systemctl --user try-restart`; inactive services are not deliberately started.
Repeated cleanup with nothing left does not restart services. Restart failure
returns nonzero; removed configuration is not rolled back, so inspect/fix the
service and retry its restart before reinstalling or resuming playback.

Reinstall using the operational configuration, then restart MPD/supervision:

```sh
"$AUDIO_BIN/audio-setup" --install --audio-processor pipewire ~/.config/mpdrussoundpi/config.json
"$AUDIO_BIN/mpd-setup" --start ~/.config/mpdrussoundpi/config.json
```

## Audio graph and hardware notes

Lessons from the October 2026 hardware audit that still constrain the design:

- **Reference platform.** Debian 13 on aarch64 with MPD 0.24, PipeWire/Pulse
  1.4 and WirePlumber 0.5, driving C-Media `0d8c:0102` USB cards. The hardware
  PCM is six-channel S16_LE at 48 kHz.
- **Linked channels and an MPD `play` state do not prove delivered audio.** The
  old loopback layout showed valid links while rear and LFE were digitally
  silent. Only per-channel digital metering or physical listening confirms
  output.
- **Loopback layout.** [sink.py](../../mpdrussoundpi/russound/lib/sink.py)
  keeps the FL/FR input layout in `capture.props`, sets the pair-specific
  `audio.position` in `playback.props` and sets `stream.dont-remix = true`. A
  global loopback `audio.position` makes PipeWire convert to a common layout
  instead of remapping the pair directly.
- **Use the 5.1 profile (`analog-surround-51`).** With 7.1, the rear pair goes
  to a side jack that one of the deployed cards does not have. A saved
  WirePlumber profile can override the configured fallback, so readiness sets
  the profile explicitly. Generated ALSA rules belong under
  `monitor.alsa.rules`.
- **USB full speed is expected.** The card advertises USB 1.1 and negotiates
  12 Mb/s. The six-channel stream needs about 4.6 Mb/s (calculated, not
  benchmarked). Bandwidth errors seen with a second card on the same controller
  are a separate problem; see the
  [USB recovery plan](../plans/usb_done_audio-recovery.md).
- **Treat warnings as clues, not proof.** WirePlumber stalls, shutdown warnings
  and wrong-port errors were correlated with USB loss or profile changes. A
  warning alone is not evidence of a dropout, and a restart that clears it does
  not prevent it.
- **Keep target backups.** Manual backups made before audio changes are under
  `~/.local/state/mpdrussoundpi/backups/` on the Pi. Keep them, and don't
  broadly clean the Pi checkout: it can contain unrelated work in progress.

## Validation evidence

For an independent read-only endurance/USB recorder, see
[long-term test instructions](./mpd-playback-endurance-test.md). This recorder
does not add another recovery loop and is installed/started explicitly, not by
the normal deployment playbook.

Focused tests cover first-boot defaults, preserving intentions, audio-error
recovery, mute/route/gain/unmute ordering, failed readiness, atomic persistence,
queue idempotence, per-output failure isolation/backoff, CLI delegation and
generated service policies. Ansible syntax is checked separately. These do
not prove analog output, startup transients, USB unplug or network recovery.
Outstanding hardware and lifecycle acceptance is tracked in the
[implementation plan](../plans/general_done_mpd-web-integration.md#6-deployment-acceptance).

# Headless MPD deployment and recovery

The [setup playbook](../../ansible/playbooks/setup-musicpi.yml) provisions
PipeWire, its Pulse server, WirePlumber, FFmpeg and python-mpd2. Run it as the
audio-owning user with passwordless sudo:

For a fresh network-enabled playback installation, follow the ordered
[deployment guide](./playback-agent-deployment.md): stage units without playback,
install public-only egress enforcement, then start/reconcile. The audio-only
play can regenerate and restart running services; it is not a web or agent
update command.

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/setup-musicpi.yml \
  --tags init,new -e mpd_russoundpi_start_playback=false
```

The staging option leaves MPD and supervision stopped, but still prepares and
restarts audio services. `--tags update` explicitly requests an OS package
upgrade; it is not part of the normal `init,new` procedure.

It enables lingering, starts the persistent user manager, grants audio-group
access without a graphical seat, and supplies the user runtime directory and
D-Bus address. The distribution-wide MPD service is disabled. If audio-group
membership is newly added to an already running manager, reboot so its
supplementary groups are refreshed.

The first deployment copies the repository seed into
`~/.config/mpdrussoundpi/config.json`. Redeployment preserves that file and
the runtime state `~/.local/state/mpdrussoundpi/playback.json`.

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

Use the operational configuration for **all** control and calibration commands:

Run the following commands on the Pi as the audio user, from the script
directory with its playback environment activated:

```sh
cd ~/MPDRussoundPi/mpdrussoundpi/russound
. ~/russound-venv/bin/activate
python mpdctrl.py --stop --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --pause --output stereo_front ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --start ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --check-route ~/.config/mpdrussoundpi/config.json
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

With `mpd_russoundpi_start_playback=true` (the audio play's default),
deployment stops an existing supervisor, regenerates/restarts audio and MPD,
then runs one reconciliation/verification pass before enabling supervision.
Failures stop deployment with diagnostics. Review a failed deployment before
re-enabling the supervisor; it is intentionally not started after unsuccessful
configuration changes. Git updates can be skipped for an explicitly staged
local deployment with `-e mpd_russoundpi_update_repo=false`; normally keep this
enabled. No `force` checkout/reset is used. For new outputs, stage with playback
disabled and rerun the egress play before allowing them to start.

```sh
loginctl show-user "$USER" -p Linger
systemctl --user status mpd_playback_supervisor.service
journalctl --user -u mpd_playback_supervisor.service -n 60 --no-pager
python mpdsupervisor.py --once ~/.config/mpdrussoundpi/config.json
```

`--once` is useful while the supervisor is stopped; concurrent mutations use
one nonblocking lock and can fail with contention. API/CLI errors remain
explicit, including a failure to save an intention or gain.

## Removing managed audio configuration

Cleanup is separate from MPD service removal. Stop the playback supervisor and
MPD instances first during an approved maintenance window; otherwise they will
keep trying to restore outputs whose virtual sinks you are removing:

```sh
python mpdsetup.py --stop ~/.config/mpdrussoundpi/config.json
python audiosetup.py --cleanup --audio-processor pipewire
```

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
python audiosetup.py --install --audio-processor pipewire ~/.config/mpdrussoundpi/config.json
python mpdsetup.py --start ~/.config/mpdrussoundpi/config.json
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
[implementation plan](../plans/general_open_mpd-web-integration.md#6-deployment-acceptance).

# Headless MPD deployment and recovery

The [setup playbook](../ansible/playbooks/setup-musicpi.yml) provisions
PipeWire, its Pulse server, WirePlumber, FFmpeg and python-mpd2. Run it as the
audio-owning user with passwordless sudo:

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/setup-musicpi.yml --tags init,new
```

It enables lingering, starts the persistent user manager, grants audio-group
access without a graphical seat, and supplies the user runtime directory and
D-Bus address. The distribution-wide MPD service is disabled. If audio-group
membership is newly added to an already running manager, reboot so its
supplementary groups are refreshed.

The first deployment copies the repository seed into
`~/.config/mpdrussoundpi/config.json`. Redeployment preserves that file and
`~/.local/state/mpdrussoundpi/playback.json`, including calibrated gains and
pause/stop intentions. Initially all configured streams are intended playing.
New streams default playing when intentions are initialized again.
Use the operational configuration for **all** control and calibration commands:

```sh
python mpdctrl.py --stop --stream "FM4 Radio" ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --pause --stream "Radio 88.6" ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --start ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --check-route ~/.config/mpdrussoundpi/config.json
```

Generated MPD units want/order after the audio services. Their readiness
command actually queries cards/sinks and enforces the configured 5.1 profile,
retrying for 30 seconds. Daemons restart on failure after five seconds, with
ten starts per minute. Missing hardware is a visible failure, not a successful
start. The supervisor makes later attempts with per-stream backoff, so an
absent device/network does not permanently consume the restart budget.

The supervisor honors saved intentions, never treating an audio-induced pause
as a user pause. For playing streams it restores the configured URL without
duplicating matching queue entries, clears errors, starts with the software
mixer muted, verifies routes, reapplies/read-verifies absolute gains, and
unmutes to software volume 100. It verifies final playback state. Stopped and
paused intentions remain silent after reboot; an empty paused queue is left
stopped. This is playback recovery, not retired Russound power polling.

Deployment stops an existing supervisor, regenerates/restarts audio and MPD,
then runs one reconciliation/verification pass before enabling supervision.
Failures stop deployment with diagnostics. Review a failed deployment before
re-enabling the supervisor; it is intentionally not started after unsuccessful
configuration changes. Git updates can be skipped for an explicitly staged
local deployment with `-e mpd_russoundpi_update_repo=false`; normally keep this
enabled. No `force` checkout/reset is used.

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
keep trying to restore streams whose virtual sinks you are removing:

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

Older generated PulseAudio `default.pa` files lack a safe ownership boundary.
Install/cleanup refuses to overwrite or delete them. Back up such a file,
manually migrate any custom settings into a normal user configuration, then
rerun the command. No automatic migration can establish which edits were yours.
Explicit PipeWire-only cleanup does not inspect or alter this legacy Pulse file.

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

## Validation evidence

For an independent read-only endurance/USB recorder, see
[long-term test instructions](./mpd-playback-endurance-test.md). This recorder
does not add another recovery loop and is installed/started explicitly, not by
the normal deployment playbook.

Focused tests cover first-boot defaults, preserving intentions, audio-error
recovery, mute/route/gain/unmute ordering, failed readiness, atomic persistence,
queue idempotence, per-stream failure isolation/backoff, CLI delegation and
generated service policies. Ansible syntax is checked separately. These do
not prove analog output, startup transients, USB unplug or network recovery;
hardware lifecycle acceptance must be recorded in the hardware audit.

On 2026-10-12 the updated playbook was executed successfully on musicpi,
including a repeat deployment preserving calibration and nonplaying choices.
All 78 focused target tests passed. A forced MPD crash and an active audio-stack
restart recovered intended playback, routes and calibrated gain while a
deliberately stopped stream remained stopped. A root pre-login reboot snapshot
showed lingering audio/MPD services and intended playback without console,
graphical or SSH login; stopped and paused intentions stayed silent.
Two-channel digital measurements were exactly zero during muted startup before
gain verification and nonzero for every playing stream afterward.
Analog transient behavior, network outage and physical USB unplug remain
unverified. The audit retains raw evidence and backup locations.

# Test Coverage Review

This review covers the Python production modules in `web/` and `tool/`.

## Baseline

- The documented `python -m unittest -q` command discovers zero tests in this checkout.
- The working command is `python3 -m unittest discover -s tests -p 'test_*.py' -q`.
- No `coverage` or `pytest` package is installed, so this is a test-to-module and behavior review, not a measured line-coverage report.
- The suite contains 103 named test methods across six test modules. This review maps the 8 non-package-marker modules in scope.
- In the project virtual environment, 101 tests pass and 2 existing controller tests fail because their `DummyClient` doubles do not implement `get_zone_extended_info` during state refresh.

## Covered Areas

- Web zone model, state/controller orchestration, backend adapter, connector protocol behavior, and Flask/SSE server behavior have dedicated tests.
- `tool/dummy_backend/dummy_backend.py` has TCP integration tests for zone reads/writes, broadcasts, persistence, checksums, and keypad displays.
- Connector tests cover checksum generation, selected frame parsing, zone information, user parameter normalization, displays, audit logging, and connection-reset handling.

## Blind Spots

- `web/config_types.py` has no dedicated tests for malformed configuration, boolean-vs-integer rejection, endpoint defaults, or controller-limit normalization.
- The `web/` and `tool/` package marker files are intentionally not documented as modules because they contain no application behavior.
- Frontend JavaScript and HTML files are not covered by the Python suite.
- Ansible playbooks and templates are not covered by automated tests.
- Audio setup has a regression test for the generated WirePlumber ALSA rule
  section, device match, and requested profile. The service restart is mocked.
- Audio setup locking tests cover contention, inode preservation, exception
  cleanup, invalid runtime directories, and kernel lock release after SIGKILL.
  Installation requires a current-user-owned mode-0700 `XDG_RUNTIME_DIR`.
  The lock file stays in that directory; its existence does not indicate an
  active lock, and it must not be deleted while setup may be running.
- Several web tests exercise private helpers or mocks rather than full HTTP request flows for every route.
- Hardware-dependent behavior, systemd/PulseAudio/PipeWire commands, and real MPD connections are not tested in this environment.

## Test Links

- [Zone tests](../tests/test_zone.py)
- [Backend tests](../tests/test_russound_backend.py)
- [Connector tests](../tests/test_russound_connector.py)
- [Controller tests](../tests/test_russound_controller.py)
- [Server tests](../tests/test_russound_server.py)
- [Dummy backend integration tests](../tests/test_dummy_backend.py)
- [Audio setup tests](../tests/test_audiosetup.py)
- [MPD setup tests](../tests/test_mpdsetup.py) verify explicit meta-service
  dependencies and ordering, including an empty instance list. Installation
  reloads the user systemd manager after writing units; enabling the meta-service
  pulls in the configured instances on its next activation. Reloading unit
  definitions alone does not start previously inactive instances.
- MPD setup and audio setup share the kernel-lock implementation in
  [setuplock.py](../mpdrussoundpi/russound/lib/setuplock.py), with separate
  per-user filenames. MPD installation holds `mpd_setup.lock` through
  daemon reload; cleanup acquires the same lock before stopping services or
  removing files. Tests cover contention, audio/MPD lock independence,
  invalid configuration, reload failure, cleanup exclusion and normal exit.
  The shared lock's exception, interrupt and SIGKILL behavior is covered by
  the audio setup tests. Both require a private mode-0700 `XDG_RUNTIME_DIR`;
  persistent lock files must not be deleted while setup may be running.
- [MPD controller tests](../tests/test_mpdctrl.py) cover individual start/stop
  for every configured name, all-stream defaults, selected/all status,
  unknown/ambiguous names, removed/conflicting options, kernel-lock contention,
  exception release and inode preservation. MPD protocol effects are mocked.

## Manual MPD Stream Control

From `mpdrussoundpi/russound/`, with MPD services already running:

```sh
python mpdctrl.py --start --stream "FM4 Radio" config.json
python mpdctrl.py --stop --stream "FM4 Radio" config.json
python mpdctrl.py --status --stream "FM4 Radio" config.json
python mpdctrl.py --start config.json
python mpdctrl.py --stop config.json
python mpdctrl.py --status config.json
```

Names are exact, case-sensitive configured stream names. Omitting `--stream`
selects all streams. Exactly one action is required; invalid/ambiguous names
fail before any MPD action. Start/stop hold `mpd_control.lock` in the private
user runtime directory; status does not require a lock.

Automatic Russound power polling and controller service management options
(`--auto`, `--install`, `--enable`, `--disable`, `--cleanup`) have been removed.
The obsolete system-info helper and its `system_on` JSON output have also
been removed; no remaining repository consumers were found.
Provisioning no longer installs that controller or starts playback.
Existing installations should retire the old service before deploying the
manual-only controller, running as the owning user:

```sh
systemctl --user disable --now auto-mpd-control.service
```

Remove the obsolete user unit after backing it up, then run
`systemctl --user daemon-reload`. This is separate from MPD instance service
management, which remains in `mpdsetup.py`. The new controller starts/stops
playback, not daemon services. Its existing MPD helper still appends the URL
on each start; queue deduplication and protocol error handling remain separate
follow-up work.

Each module document below links its closest test cases and names the remaining untested behavior.

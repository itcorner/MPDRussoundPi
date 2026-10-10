Mount point for the Russound web configuration.

The container reads `/config/russound_config.json`. This directory may be empty:
open the web UI to create a configuration in setup mode. The directory must be
writable by the container user so the editor can save atomically.

Alternatively, copy `web/config_example.json` to `russound_config.json` and adapt
the backend endpoint and hardware layout. Invalid files enter setup mode and
are preserved as `russound_config.json.invalid-*.bak` when replaced by a save.

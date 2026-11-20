# `web/config_store.py`

## Documentation

Single in-process owner of `russound_config.json` and `russound_state.json`.
`JsonFileStore` keeps the parsed file in memory and revalidates it with one
`stat()` per access (device, inode, size, modification time). It rereads the
file only when that signature changes or when the file was modified within the
last two seconds: coarse file-system timestamps can hide a same-size rewrite, so
recent files are compared byte for byte (like git's "racy clean" check). A
reread with identical bytes keeps the parsed value and generation.

Each snapshot carries `load_state` (`missing`, `valid`, `invalid`,
`unreadable`), the parsed value, an error message and a `generation` counter
that increases whenever the content changes. `ConfigStore` adds validation and
the opaque, process-keyed config `revision` used by `X-Config-Revision`.
Callers receive private deep copies; after replacing a file the controller calls
`record_write()` so the next read sees the new bytes.

The web process is the only writer while it runs. Hand edits are picked up on
the next access; deployment restores stop the runtime first.

## Requirements

- Python 3.11+.
- Config and state on a local file system with working `stat()` timestamps.

## Test Cases

[Store tests](../../../tests/test_config_store.py) cover load-state transitions
and generations, no reread of a settled file, same-size rewrites inside the racy
window, touch without content change, private copies, revisions and the
writer notification. Controller and server tests exercise it through the
controller.

## Blind Spots

A same-size in-place edit that keeps the exact nanosecond timestamp after the
two-second window is not detected. Network file systems are not supported.

# Schema version for persisted application data

**Status:** open (2026-11-27). Stages 1 to 3 implemented on 2026-11-27, not yet
rolled out; stage 4 (the rehearsal on the Pi) is open. Proposed on 2026-11-27 together with
[Release channels](./general_new_release-channels.md), which depends on it.
Decision B (below) confirmed by the user on 2026-11-27: an application that
finds newer data keeps running read-only for that file instead of refusing to
start.

## Why

The web application stores its settings and state in plain JSON files without a
format version. An image update, a restore after a failed update and, soon, a
switch between the development and the stable channel can put an application
version in front of data written by another version. Today nothing can tell
whether that version understands the data:

- the [image update plan](./general_open_user-triggered-image-updates.md#cross-version-restore-deferred)
  deferred its cross-version restore rehearsal for exactly this reason;
- the [CI/CD plan](./general_open_cicd-docker-release.md) requires rollback and
  "persistent configuration/state compatibility" before the release path;
- a switch from the development to the stable channel usually goes to an
  *older* version (see the release channel plan), which may not read data that
  a newer development build wrote.

The deployment tooling's own records (deployment state, reservations,
candidates, manifests, completion events) already carry `schema_version: 1` and
are out of scope.

## Inventory (reference Pi, 2026-11-27)

| File (in the web data directory) | Writer | Today | Proposed |
|---|---|---|---|
| `config/russound_config.json` | Configuration page, setup | JSON object, unknown top-level keys ignored | versioned |
| `data/russound_state.json` | controller (zone state) | JSON object | versioned |
| `data/admin-lockout.json` | admin login | JSON object | versioned |
| `secrets/russound-admin.scrypt`, `data/russound-admin.scrypt` | password setup | `scrypt$N$r$p$…`, self-describing | unchanged |
| `secrets/musicpi-playback-token`, `musicpi-proxy.crt` | Ansible | opaque secrets | unchanged |
| `data/protocol-audit.log` | backend | append-only text log | unchanged |

The playback agent on the Pi is native, has its own API version
(`GET /v1/capabilities`) and its own data; it is outside this plan.

## Proposed design

1. **A version field in each versioned file.** A top-level
   `"schema_version": 1`. A file without the field is version 1 (today's format);
   the next write adds the field. Today's code already ignores unknown top-level
   keys, so a version-1 file stays readable by the currently released code.
2. **Each application version declares what it understands**, per file: the
   version it writes and the oldest it can read (and migrate). Migrations go
   forward one step at a time (1 → 2 → 3) in code, run when the file is loaded,
   and write the file once, atomically, like today.
3. **The image says it too.** The web image carries an OCI label, for example
   `net.itcorner.mpdrussoundpi.data-schema="config=1;state=1;lockout=1"`, built
   from the same constants. The updater reads it from the registry (it already
   reads the image configuration for the platform check) *before* anything is
   pulled or stopped.
4. **The updater refuses an incompatible change before it starts.** An update,
   a restore or a channel switch is refused in the review when the target image
   cannot read the data on the Pi: "This version cannot read the settings
   written by the installed version (schema 3, it reads up to 2)." Restores are
   unaffected in the normal case, because a restore brings back the old images
   *with their own backed-up data*.
5. **The application refuses to overwrite newer data (decision B, confirmed).**
   As the last line of defence (an SSH deployment, a manual image change), an
   application that finds a file with a newer schema than it knows keeps
   running read-only for that file: it shows the data it can, rejects saves
   with a clear message and never rewrites the file. The alternative is to
   refuse to start, which takes the whole page away, including the update
   controls that could fix the situation.
6. **Downgrades never migrate backwards.** Going back to an older version is
   possible only when that version reads the current schema, or by restoring a
   backup taken before the migration. Migrations should therefore be rare and
   additive where possible; an additive change (a new optional field) does not
   need a new schema version.

## Rules

- A change that an older application version would misread or drop needs a
  schema version step and a migration; a purely additive optional field does
  not. The PR template and the contributor documentation say so.
- The configuration editor and every writer preserve `schema_version`.
- The schema constants, the image label and the updater check come from one
  place, so they cannot drift apart.

## Stages

- [x] **1. Version field and reading rules** (implemented 2026-11-27). Constants per file, load with
  "missing = 1", write the version, preserve it in the configuration editor and
  in `admin_auth`'s lockout file, read-only handling of newer files (decision B),
  status/API field showing the data schema. Tests: missing field, current,
  newer (read-only, save rejected, file unchanged), malformed version.
- [x] **2. Image label and updater check** (implemented 2026-11-27). Label in the Dockerfile from the
  constants (build argument checked in CI), the registry read in
  `tool/deployment_images.py`, the refusal in the update and restore reviews,
  the schema in the candidate and the review summary.
- [x] **3. Migration framework** (implemented 2026-11-27, no migration yet). One-step migrations with tests from recorded
  version-N files; a first real migration only when a change needs it.
- [ ] **4. Cross-version rehearsal on the reference Pi** (the deferred item of
  the image update plan): update to a build with a test schema step on a
  disposable copy, verify the refusal of the older image, and restore.

## Implementation notes (2026-11-27)

- **One module**, `mpdrussoundpi/common/dataschema.py`: per file the version it
  writes (`current`) and the oldest it reads (`oldest`), the one-step
  `MIGRATIONS`, the image label (`config=1;lockout=1;state=1`, or
  `config=1-2` once a file reads more than one version) and the comparison the
  updater uses. All three files are at version 1.
- **The stores** (`web/config_store.py`) take a `schema` name: reading
  migrates in memory (the file changes only at the next save), `prepare()`
  stamps the version on writes, and a newer file is published as valid but with
  `read_only` set, so `writable()` refuses.
- **Newer settings** (decision B): the configuration page shows them with the
  reason and without the save bar; a save returns `409 config_read_only` and
  writes nothing (neither the settings nor the room state).
- **Newer room state** keeps the rooms working; only saving the state is
  skipped, with one warning in the log. Refusing room control because of a cache
  file would be worse than the problem.
- **Newer lockout file**: failed logins are counted in memory, the file is left
  alone.
- **The label** is set by CI (validation and image builds) and the local build
  in `tool/deployment_build.py`, for the web image only; the smoke test fails
  when the label differs from what the code in the image reports.
- **The updater** reads the label of the candidate's web image by digest (a
  separate registry read: adding a field to the candidate would change the
  deployment records already on the Pi) and the versions of the three files in
  the deployment's data directory. A target that cannot read them fails the
  update before anything stops ("The new version abc1234 cannot read the data on
  this Pi: the settings (format 2; that version reads up to 1). Nothing was
  interrupted."). An image without the label reads version 1 only. A broken
  file is not a newer format; the application reports it. A restore needs no
  check.
- **Contributor rule** in [Development workflow](../documentation/development-workflow.md#persisted-data-formats).

## Related

- [Release channels](./general_new_release-channels.md): channel switches rely
  on the compatibility check.
- [User-triggered image updates](./general_open_user-triggered-image-updates.md#cross-version-restore-deferred).
- [Safe configuration changes](./web_open_safe-configuration-changes.md): the
  configuration writers.

# Release channels: stable and development

**Status:** new (2026-11-27). Proposed on 2026-11-27; the user asked to work out
the channel selection and its implications before implementing it, and
confirmed decisions 1 to 3 the same day. Builds on
[Schema version for persisted data](./general_open_persisted-data-schema-version.md)
and on stage 6 of the [CI/CD plan](./general_open_cicd-docker-release.md), which
prepared the release publication.

## Decisions (confirmed by the user on 2026-11-27)

1. **The first version is `0.9.0`**, published first as the release candidate
   `0.9.0-rc.1` and installed on the reference Pi before `0.9.0` is tagged. A
   short pre-1.0 phase; `1.0.0` follows later (for example once guided setup
   ships). The alternatives were `1.0.0` directly and `0.1.0`.
2. **Development → stable waits for stable to catch up** (option A below). The
   alternatives were offering the older release at once, and going back through
   a backup.
3. **Two channels: Stable and Development.** Release candidates are installed
   only by the operator for the acceptance. The alternative was a third channel
   for release candidates.
4. **Defaults** (proposed, not asked): new installations follow Stable; the
   reference Pi stays on Development.

## Goal

Publish versioned releases (`0.9.0`, `0.9.1`, `0.10.0`) from `main` next to the
development builds from `develop`, and let the administrator choose on the
Status page whether this installation follows **stable releases** or
**development builds**, and switch between them safely.

## Today (2026-11-27)

- `develop` publishes `dev-<commit>` and moves `dev`; this is the only active
  channel. The reference Pi runs `dev-f91c774…`.
- Release publication (`vX.Y.Z` tags → `X.Y.Z`, `X.Y`, `latest`; release
  candidates `vX.Y.Z-rc.N` → `X.Y.Z-rc.N`) is designed, tested build-only and
  disabled. No version tag exists. Git `v*` tags and Docker Hub `X.Y.Z` tags are
  immutable: a published version can never be corrected, only superseded.
- The web's update check already compares a release image with `latest` and a
  development image with `dev`; the channel is implied by the running image.
- The updater (`tool/deployment_images.py`) resolves only `dev`, and its
  candidate format allows only `channel: "dev"`.
- The host tooling (`/opt/mpdrussoundpi-deploy`) and the native playback agent
  are installed by the operator over SSH (Ansible), from a reviewed commit.

## The first version number

Decided: `0.9.0` (decision 1). The options were:

| Option | Says | Fits when |
|---|---|---|
| `1.0.0`, rehearsed as `1.0.0-rc.1` first | "Stable and in daily use; breaking changes raise the major version." | The appliance runs in production with update, restore and Pi system updates, and the data format gets schema version 1 at the same time. |
| `0.1.0` | "Early; anything may change between minor versions." | We want freedom to change the configuration format or APIs without a major version. |
| **`0.9.0`** (chosen) | "Nearly 1.0." | A short pre-1.0 phase, e.g. until guided setup ships. |

Because version tags are immutable, the first stable version is preceded by a
release candidate (`0.9.0-rc.1`, `-rc.2`, …) that goes through the whole
publication and is installed on the reference Pi before `0.9.0` is tagged.

Versioning rules after that: **patch** (`0.9.1`) for fixes only, **minor**
(`0.10.0`) for new features. Before 1.0, SemVer allows incompatible changes in a
minor version; this project still treats them as it will after 1.0: an
incompatible change of the configuration, a data schema step without an
automatic migration, or an external API change is announced in the release
notes and needs a minor version at least. From `1.0.0` on such a change needs a
new major version.

## Channels

- **Stable** (default for new installations): exact release versions
  `X.Y.Z` from `main`, after a release candidate passed the acceptance on the
  reference Pi. Follows `latest`, never pre-releases.
- **Development**: every `develop` build (`dev-<commit>`), as today. Newest
  features and fixes first, CI-tested but not hardware-accepted. The reference
  Pi stays on it.
- **Release candidates** are not a channel (decision 3): they are installed only
  for the acceptance, by the operator. A third "beta" channel can be added later
  without changing the design.

## What switching means

The two channels are not two versions of the same thing at different speeds:
development is normally **ahead** of stable. That makes the two directions
different.

**Stable → development** is an upgrade: the newest `dev` build contains
everything the installed release has. It is offered right away as a normal,
reviewed update (backup, checks, restore on failure). Afterwards the
installation may have data in a newer schema, so going back is not immediate
(next case).

**Development → stable** usually means going to an **older** version. The
installed development build may contain features, settings and a data schema
the newest release does not know. Options:

- **A. Wait for stable to catch up (decision 2).** The channel changes at once,
  but no update is offered until a stable release *contains* the installed
  build (its commit is a descendant of the installed build's commit). The page
  says: "Stable channel. You are on a development build newer than the newest
  release (0.10.0); the next release that includes it will be offered." Nothing
  goes backwards, and no data is put in front of an older version.
- **B. Offer the older release now when it can read the data.** The review shows
  that it is a downgrade, what is lost and the schema check (an incompatible
  schema refuses it). Faster, but an older configuration editor can drop settings
  it does not know when it saves (zone entries are re-serialised by the older
  code), and features disappear.
- **C. Go back through a backup.** Restore the data from before the last
  migration together with the release. Loses every change since then; only for
  recovery, not a normal switch.

In every direction the update itself is the existing reviewed update with
backup, checks and restore, and the schema check of the other plan refuses an
incompatible target before anything is stopped.

## Where the channel is stored and who may change it

- The channel is installation state, owned by the updater on the host (next to
  its `state.json`), not by the web container. The web asks for a change through
  one new fixed request (`channel` with `stable` or `development`), only for the
  logged-in administrator with the CSRF token, and records it in the audit log.
- The web's update check reads the channel from the updater instead of
  inferring it from the running image; the Status page shows "Channel: Stable"
  or "Channel: Development" in "Software updates", and for the administrator a
  selector with a short explanation and a confirmation that names the
  consequences of the chosen direction.
- Changing the channel changes no software by itself; it changes what the next
  check offers.

## Tooling and components

- The updater resolves a release as `latest` → exact `X.Y.Z` for each image,
  pinned by digest, with the same agreement checks as for `dev`; the candidate
  gets `kind: "release-image-candidate"`, `channel: "stable"` and the version.
- Web and ser2net images are released together with one version.
- The host tooling and the native playback agent stay operator-managed for now
  (SSH/Ansible). The tooling must support both channels; a stable installation
  can run tooling from `develop` as long as the review checks pass. Releasing
  the tooling and the agent with the same version is a later step.

## Stages

- [x] **0. Decisions** (confirmed 2026-11-27): first version, switching
  behaviour, channels, defaults.
- [ ] **1. Release publication** (CI/CD stage 6): serialized, non-regressing
  alias publication, environment approval for release tags, the release PR
  `develop` → `main` with a merge commit, and the release notes.
- [x] **2. Updater: release candidates and the stored channel** (implemented
  2026-11-27). Resolution of
  `latest`, the release candidate format, the `channel` request, the channel in
  status and review, the "contains the installed build" rule for development →
  stable.
- [x] **3. Web: channel display and selector** (implemented 2026-11-27), update check by stored channel,
  version display ("0.9.0" or "Development build f91c774, after 0.9.0").
- [ ] **4. First release candidate** `0.9.0-rc.1` (after explicit approval of the
  tag): publication, installation on the reference Pi by the operator, the
  acceptance, then the stable tag `0.9.0` (separate approval).
- [ ] **5. Channel acceptance on the reference Pi**: switch development →
  stable (waits), stable → development (update offered), and the schema refusal
  with a test build.

## Implementation notes (2026-11-27, stages 2 and 3)

- **Resolution** (`tool/deployment_images.py`, `--channel dev|stable`): stable
  reads `latest`, requires a release version label `X.Y.Z` (never `-rc.N`),
  resolves every image by its exact version tag and checks that alias and tag
  agree, like `dev`. The candidate is `kind: release-image-candidate`,
  `channel: stable`, plus `version`; the development format is unchanged, so the
  deployment records on the Pi stay valid.
- **The channel** is stored by the updater in
  `/var/lib/mpdrussoundpi-updater/channel.json` (with who changed it and when);
  without it, the channel of the installed version applies. The request
  `channel` is refused while an update runs. A result of the other channel
  ("up to date", "waiting") is cleared when the channel changes.
- **When a release is offered:** over a release only with a higher version; over
  a development build only when the release's commit contains it, read from
  GitHub's public compare API (`mpdrussoundpi/common/sourcehistory.py`, commit
  identifiers only, no credentials). Otherwise `waiting_for_release`; when GitHub
  cannot answer, nothing is offered and the page says so.
- **The page:** "Channel" in "Software updates" for everyone; for the
  administrator a selector with a confirmation that names the consequences of
  the chosen direction, hidden during an update. The web's update check follows
  the updater's choice and is repeated at once after a change. The review names
  the release version.
- **Not yet:** release publication (stage 1) is still disabled, so the stable
  channel finds no release until `0.9.0` is published; the updater then reports
  that the newest release could not be resolved.

## Related

- [CI/CD pipeline and Docker image release path](./general_open_cicd-docker-release.md):
  publication rules and stage 6.
- [Schema version for persisted data](./general_open_persisted-data-schema-version.md).
- [User-triggered image updates](./general_open_user-triggered-image-updates.md):
  the updater and the review.

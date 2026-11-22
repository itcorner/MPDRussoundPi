# Publish development images only when they changed

**Status:** open. Implemented 2026-11-22 (stage 1). The first pushes after the merge are
the acceptance (stage 2). Recorded on 2026-11-22 after investigating which develop
pushes actually changed an image.

## Why

Every push to `develop` built and published both images (`dev-<commit>` plus the
moving `dev` alias), and every installation that follows `dev` was offered an
update. Most pushes do not change any image:

- Between 2026-10-01 and 2026-11-22, `develop` received 114 pushes. **61 (54%)** changed
  neither image's inputs: docs, plans, tests or deployment tooling only.
  53 changed web inputs, and **none** changed ser2net inputs.
- Updates offered from such pushes still stop both containers for the backup,
  take a backup copy and restart the web application, for no software change.

## Decision

A develop push publishes only when it is worth an update (idea 1 of the
investigation). Validation runs on every push regardless.

A push builds and publishes when any of these is true:

1. **Image inputs changed** since the revision the `dev` images were built from:
   - web: `Dockerfile`, `.dockerignore`, `web/`, `mpdrussoundpi/__init__.py`,
     `mpdrussoundpi/common/`;
   - ser2net: `Dockerfile.ser2net`, `Dockerfile.ser2net.dockerignore`,
     `docker/entrypoint-ser2net.sh`;
   - the image workflow, whose build arguments and labels shape both images.

   Markdown is not an input. It is now excluded from the web build context, so
   `web/API.md` no longer ships in the image.
2. **Forced:** a commit since then contains the line `Publish-Images: force`
   (for test candidates and manual refreshes).
3. **Refresh:** the published images are 7 days old or more, so base-image and
   security updates still arrive with the next push.
4. **Uncertain:** anything that cannot be read with certainty publishes, as
   before. Examples: a registry error, the two images on different revisions, no
   build time, or a published revision outside the pushed history.

The comparison is against the **published** revision, not the previous commit.
So a failed or skipped publication never hides a change.

**Not in scope (idea 2, decided against for now):** publishing an unchanged
ser2net image under the new revision. Every update stops all project containers
before its backup anyway, so ser2net would still restart. It would also need a
mixed-revision candidate in the deployment tooling, publisher, journal,
recovery and verification. If it ever matters, the update review can say
"ser2net unchanged since `<revision>`" instead.

## Impact

- **Fewer, real updates.** The Pi, the updater, the update check and the
  deployment tooling are unchanged. They still compare the `dev` tag.
- **Revision meaning.** The image revision is the commit it was built from, not
  necessarily the tip of `develop`. The Status page now says "build commit"
  instead of "latest commit".
- **Base images.** These used to be refreshed incidentally by every publishing
  push. Now they are refreshed by the 7-day rule at the next push. Without pushes
  there is no refresh: a scheduled run would start from `main` (the default
  branch), and publication and recovery accept only develop push runs. A
  scheduled refresh is a separate decision.
- **Testing the updater** needs a real web change or the `Publish-Images: force`
  line, because docs PRs no longer produce candidates.
- **Build time label.** Both images now carry `org.opencontainers.image.created`.
  Fully cached ser2net builds kept their config time (2026-11-06) and could not
  be used for the age rule.

## Stages

- [x] **1. Implementation** (2026-11-22):
  - `tool/ci/image-changes.mjs`, with a pure decision function and anonymous
    read-only registry access that never sends the token to the blob CDN;
  - the `changes` job gating `images` in `images-build-only.yml`, and through it
    the publication;
  - the build-time label;
  - Markdown excluded from the web image;
  - tests in `tests/test_image_changes.js`: decisions, registry reading, input
    lists against the Dockerfiles' `COPY` instructions, and workflow wiring;
  - documentation in `docs/documentation/docker-image-release.md`.

  Replay of the final rules on all 113 develop pushes since 2026-10-01 (each
  compared with the one before): 54 would have published (image inputs or the
  image workflow changed) and 59 would have skipped. Of the last eleven, only
  #77, #80, #82 and #87 publish. #83, a docs PR that touched only `web/API.md`,
  now skips.
- [ ] **2. Acceptance:**
  - [x] this change itself publishes (run 45042430789 for `aafa099`, 2026-11-22):
    the `changes` job decided "Image inputs changed since 948a23e: web."
    (`.dockerignore` and the Status page wording); both images were built and
    published, and both `dev` images now carry `org.opencontainers.image.created`
    (16:14 UTC) and revision `aafa099`;
  - a docs-only push afterwards skips, with its reason in the run summary, and
    `dev` does not move;
  - the Pi's update check stays `up_to_date`;
  - a later web change publishes again.

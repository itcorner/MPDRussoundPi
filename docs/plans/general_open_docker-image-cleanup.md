# Clean up old Docker images after a successful update

**Status:** open. Planned, decided, implemented (stages 1, 2 and 5) and rolled out with the backlog removed (stage 3), all on 2026-11-22. Acceptance on the Pi (stage 4) is still open. Related to the
[user-triggered image updates](./general_open_user-triggered-image-updates.md)
(every web or SSH update adds images) and to
[publishing only changed images](./general_open_publish-only-changed-images.md)
(fewer updates, but the growth stays).

## Why

Nothing removes this project's Docker images once they stop being used. On the
reference Pi on 2026-11-22, after about two weeks of development updates:

| What | Tags | Source |
|---|---|---|
| `mpdrussoundpi-recovery/{web,ser2net}:<operation>` | 51 | `ComposeDeployment.stop()` tags the old images of every operation before saving them into the backup |
| `mpdrussoundpi-local/{web,ser2net}:<operation>` | 19 | local build bundles (`deployment_build.py`) |
| `itcornernet/mpdrussoundpi-{web,ser2net}`, old `dev-*` or untagged | 8 | earlier registry candidates (two of them run now) |
| Outside the project namespaces: `mpdrussoundpi-{web,ser2net}:latest` (two months old), `mpdrussoundpi-web:source-outage-ui-20261112` (used by the exited legacy container `russound-web`), `mpdrussoundpi-s3-dummy`, `mpdrussoundpi-deployment-test`, `python:3.12-slim` | 6 | earlier native, test and manual builds |

`docker system df` reports 60 images, 1.49 GB, of which **724 MB are reclaimable**.
Disk use is 40% of 58 GB, so this is not urgent. But every update adds a recovery
tag pair, and the space grows with every update whose images do not share all
layers with the ones before. The tooling's own per-operation
files take much more (see [Later](#later-operation-file-retention)):
backups 2.9 GB, assets 2.2 GB, incoming bundles 1.2 GB, restores 0.2 GB and updater
evidence 0.5 GB.

**Key fact for safety:** a restore never needs these Docker tags. The backup of
an operation contains the old images as archives (`web.tar`, `ser2net.tar` in
its assets). `load_recovery_images()` loads them back with `docker image load`,
which recreates the recovery tag. Removing a tag therefore does not reduce
what can be restored, as long as the backup itself is kept.

## Decisions (confirmed by the user on 2026-11-22)

1. **Keep the current and the previous version as Docker images** and remove the
   rest. This keeps a manual rollback quick without loading archives, at the cost
   of at most one extra image pair. The alternatives were only the current
   version, or the current plus the last two.
2. **Automatic after every verified-complete update** (web or SSH), **plus a manual
   command** with a dry run for the existing backlog and for checks. The
   alternatives were manual only, or automatic only.
3. **Only this project's namespaces.** The six images outside them are never
   touched automatically. The dry run lists them for a manual decision. One of
   them is still used by the exited legacy container.
4. **The page shows a short line** in the update result: "Removed N old images
   (X MB)". It ships with the next web change.

## Design

**What is kept (never removed):**

- every image used by any container, running or stopped (removal without `--force`
  refuses anyway; the tool checks it first and reports it);
- the committed pin: the images of the running deployment, matched by index,
  platform or config digest, or the loaded local images of a local candidate;
- the previous version: the images of the operation the current one replaced,
  identified from its retained backup's `images.json`;
- everything when the deployment state is `blocked`, holds a reservation or has a
  pending operation. Then nothing is removed and the reason is reported;
- anything outside the three project namespaces.

**What is removed:** the remaining images in exactly these namespaces:

- `mpdrussoundpi-recovery/{web,ser2net,dummy}:<operation>`;
- `mpdrussoundpi-local/{web,ser2net,dummy}:<operation>`;
- `itcornernet/mpdrussoundpi-{web,ser2net}`: old tags and untagged images.

They are removed one by one with `docker image rm <id or tag>`, never with
`--force`, and never with `docker system prune` or `docker image prune -a` (which
would touch unrelated images). Shared layers stay until their last image is gone;
Docker handles that. Build cache is out of scope (no builds run on the Pi).

**Where it runs:**

- **Automatically:** in the target side of the reviewed operator, after the
  update is committed (`verified-complete`), still under the operator lock, so no
  other operation can run at the same time. It is best effort: a failure to
  remove an image is reported in the result and the evidence, but never turns a
  successful update into a failed one and never touches the committed state.
  It does not run after a failed update, a restore or an `outcome_unknown`. The
  failed operation's images are exactly what someone may want to inspect.
- **Manually:** `python -m tool.deployment_cleanup images`
  (as root in the tooling directory). It is a dry run by default and lists the
  keep set with reasons, the removal candidates and their sizes. With
  `--apply` it removes them. It takes the operator lock like an operation.
- **Review and result:** the update review states that old images of this project
  outside the current and previous version are removed after success. The
  result records `cleanup: {removed, skipped, freed_bytes}`. The updater passes
  it through, and the Status page can show "Removed N old images (X MB)" in the
  update result (decision 4). That page change lands with the web image.

**Not changed:** backups, assets, incoming bundles, restores and evidence (see
[Later](#later-operation-file-retention)), the restore path, the preflight space
check (`3 × size + incoming + 256 MB`), and the updater's protocol, apart from the
result field.

## Stages

- [x] **1. Keep set and manual command** (implemented 2026-11-22):
  - `tool/deployment_cleanup.py`, with a pure decision (`keep_set`, `plan`) from
    the state store, the backup's `images.json` and `docker image ls` /
    `container inspect` output;
  - `python -m tool.deployment_cleanup images [--apply] [--json]` under the
    operator lock;
  - tests in `tests/test_deployment_cleanup.py` with a fake Docker runner. They
    check that:
    - only the project namespaces are touched;
    - the running, committed (all three digests), previous and local-candidate
      images are kept;
    - images used by stopped containers are kept;
    - a blocked, reserved or pending state, or a missing commit, removes
      nothing and asks Docker nothing;
    - tags are removed before the ID, and an image also tagged outside the
      project is never removed by ID;
    - nothing is removed with `--force` or `prune`;
    - a refused removal is reported and the cleanup continues;
    - freed space is measured with `docker system df`.

  A read-only dry run against the reference Pi's real state on 2026-11-22
  would remove 50 images and kept exactly the running `aafa099` pair and the
  previous `6418324` pair, which are the images the backup of operation
  `33548cc0` saved. The images outside the namespaces were only listed.
- [x] **2. Automatic cleanup after a verified-complete update** (implemented
  2026-11-22): `after_update()` runs in the operator's `serve` after
  `runner.execute()` returns, still under the lock. It never raises, and the
  `complete` event carries `cleanup`. The SSH review prints the cleanup note.
  The updater's review summary has a `cleanup` sentence (none for a restore),
  and its result keeps only the counts (`_cleanup`). Failed updates, restores
  and unknown outcomes never reach it, because only the successful deploy path
  calls it.
- [x] **3. Rollout** (2026-11-22 on the reference Pi):
  - tooling upgraded from `6418324` to `0388399` with `upgrade-deployment.yml`
    (no failed tasks), and the idle updater restarted; the web container
    reaches it;
  - the dry run listed 50 images (74 references: 49 recovery, 19 local and
    6 old registry references). It kept the running `aafa099` pair and the
    previous `6418324` pair, and listed the six images outside the namespaces;
  - the user approved `--apply`. All 50 were removed and none skipped.
    `docker system df` went from 60 images and 1.486 GB to 10 images and
    1.229 GB (257 MB freed). The containers were not restarted, all three
    outputs kept playing, and a second dry run listed nothing to remove.
  - The dry run's byte estimate ("up to 10190 MB", the sum of full image
    sizes) was misleading, because the images share most layers. It was
    replaced by Docker's own reclaimable total with a note on what that
    includes. `--apply` reports the space it actually freed.
- [ ] **4. Acceptance on the Pi:**
  - a web-triggered update removes the images that just became old and keeps
    the current and previous versions;
  - **a deliberately failing update followed by "Restore previous version"
    still works after the cleanup**: the restore loads the archives from the
    backup, so the removed recovery tags do not matter;
  - an SSH operator update cleans up the same way;
  - the manual dry run lists nothing removable right after an update.
- [x] **5. Page line** (implemented 2026-11-22): "Removed N old images (X MB)."
  after a successful update, and an "Old images" row in the review
  (`sanitize_status` passes `removed` and `freed_bytes` only; UI test). The line
  appears once both the web image and the tooling with the new result field are
  on the Pi.

## Later: operation file retention

The larger growth is the tooling's per-operation files under
`/var/lib/mpdrussoundpi-operations` (backups, assets, incoming bundles, restores)
and `/var/lib/mpdrussoundpi-updater/evidence`. That is about 7 GB after two weeks.
Deleting them **does** reduce what can be restored: an older backup is the only
way back to that version's data and images. That needs its own decision. Options:

- keep the last N successful operations' backups and assets;
- delete incoming bundles once committed;
- keep evidence records but drop their large copies.

It also needs a matching rule in the preflight space check. It is not part of
this plan.

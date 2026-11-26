# Operation file retention

**Status:** done (2026-11-26). Decisions 1 to 3 confirmed by the user on 2026-11-26;
stages 1 to 5 implemented and rolled out on the reference Pi on 2026-11-26
(the retention core, running it after each verified update, the space-check
note, pruning tooling checkouts, the backlog). Disk use went from 27 GB (47%)
to 16 GB (29%). Acceptance (stage 6) passed on the same day, including a
deliberately failed update and its restore. Follows the
[Docker image cleanup](./general_done_docker-image-cleanup.md#later-operation-file-retention),
which removed old images but left the much larger per-operation files as a separate
decision. Related to the
[user-triggered image updates](./general_open_user-triggered-image-updates.md).

## Why

Every deployment, update and recovery leaves files on the Pi, and nothing removes
them. On the reference Pi on 2026-11-26, after about two weeks:

| Where | What | Size | Entries |
|---|---|---|---|
| `/var/lib/mpdrussoundpi-operations/backups/<op>` | the target's own backup of the data before each operation | 4.0 GB | 43 |
| `…/assets/<op>` | the old images as `web.tar`/`ser2net.tar`, plus `spec.json`, `compose.json`, `baseline.json`, `images.json` | 3.1 GB | 66 |
| `…/incoming/<id>` | uploaded bundles and backups received for recoveries | 1.3 GB | 15 |
| `…/restores/<recovery>` | trees restored by recoveries | 0.3 GB | 5 |
| `/var/lib/mpdrussoundpi-updater/evidence/<op>/` | the updater's copy of each backup (the controller's copy, used by "Restore this backup") | 1.5 GB | 14 |
| `/opt/mpdrussoundpi-deploy.upgrade-<rev>` | tooling checkouts kept by `upgrade-deployment.yml` | 1.3 GB | 17 |
| `…/evidence`, `…/runtime`, `…/staging`, updater `*.json`/`*.log` | small records: reviews, results, intents, compose inputs | about 3 MB | several hundred |

That is about 11 GB. Disk use is 47% of 58 GB, so this is not urgent, but each
update adds about 300 MB (backup about 104 MB, assets about 87 MB, updater copy
about 104 MB).

**Unlike the image cleanup, deleting these files reduces what can be restored.**
A recovery needs:

- the controller's copy of the operation's backup: the updater's
  `evidence/<op>/`, or the SSH operator's private evidence on the Mac;
- the target's `assets/<op>` (old images and the previous compose input);
- for `resume-original`, `assets/<op>` and the pending record.

The image cleanup reads `assets/<committed backup>/images.json` to find the
previous version. Running containers can use a compose file under
`runtime/<op>/`.

## Decisions (confirmed by the user on 2026-11-26)

1. **Keep the current and the previous operation**, and remove the large files of
   older finished operations. "Current" is the committed operation, "previous" is
   the verified-complete operation before it. This matches the image cleanup
   (current and previous version as images), and the page restore only ever uses
   the latest failed update's backup. Frees roughly 9 GB now. The alternatives
   were the last three, or only the current.
2. **Run automatically after each verified-complete update**, next to the image
   cleanup and reported the same way on the page and by the SSH operator. It is
   also available as an SSH command with a dry run, for the backlog and for
   manual use. The alternative was a manual command only.
3. **The tooling upgrade play keeps the two most recent previous checkouts** and
   removes older ones after a successful upgrade. The alternatives were one, or
   all.

## Rules

Whatever the count, these are never removed:

- anything belonging to the pending operation, a held reservation, or an
  updater record that is unfinished, `failed` or `outcome_unknown` (its restore
  source and assets);
- anything the committed record or the running project refers to: the committed
  operation, its `selection.backup`, and the `runtime/<op>/` directory whose
  compose file the containers use;
- the small records (`evidence`, `runtime`, `staging`, the updater's JSON and log
  files). They are the history of what happened and take only a few megabytes;
- anything not named by an operation or recovery ID that the state or the
  evidence knows, and anything outside the two storage roots and the `/opt`
  checkouts.

If the state is unreadable, blocked or uncertain, nothing is removed. Like the
image cleanup, the retention runs only after the update has been committed as
verified-complete. A failure is reported but never changes the update's
outcome.

## Stages

- [x] **1. Retention core** (`tool/deployment_retention.py`, 2026-11-26): compute the keep set
  from the state store, the operations' evidence and the updater record. Then
  list the large entries outside it (`backups`, `assets`, `incoming`,
  `restores`, the updater's backup copies) with their sizes. Dry run by default,
  `--apply` to remove. Unit tests with fixture trees, including the
  never-remove cases above.
  - Entries are attributed to an operation by their name. Recovery IDs are
    mapped through the reviewed `<op>.recovery-attempt.json` (`review.recovery`);
    a received backup in `incoming/<random>/` through its single `<op>/` child.
    "Previous" is the newest operation of the same deployment with apply attempt,
    backup and completion records and no recovery, other than the committed one.
  - The running containers' compose labels (project name, config files, working
    directory) are read so that nothing in use is removed. On the Pi, the
    containers use `runtime/<committed op>/active.json`, and the project name
    `pi-handover-e651357…` comes from the handover operation.
  - *Dry run on the reference Pi* (2026-11-26, temporary copy, nothing
    removed): it would remove 119 directories, about 9.8 GB. It keeps the
    committed `027b449e`, the previous `a97b8190` and the handover operation
    `e6513571`. 17 small `assets` directories without evidence (reviews that
    never started) are listed as not identified and left alone.
- [x] **2. After each update** (2026-11-26): called right after the image cleanup
  in the operator's verified-complete path; the `complete` event carries
  `retention` with `{"operations", "removed", "skipped", "freed_bytes", "error"}`.
  The updater stores those counts. The page shows one more line, "Removed the
  backups of N older updates (X MB, or GB from 1 GB).", and the review has an "Old backups" row
  ("Kept for this and the previous update; older ones are removed after
  success."). The web client passes on only `operations` and `freed_bytes`. A
  restore never runs it.
- [x] **3. Preflight space check** (2026-11-26): the existing free-space rule
  is unchanged (room for the incoming images, the backup and an isolated
  restore). The deployment documentation now says that the retention runs only
  after success, so a full disk still needs the manual command first.
- [x] **4. Tooling checkouts** (2026-11-26): after a successful upgrade,
  `upgrade-deployment.yml` runs `deployment_tool_upgrade.py prune --keep
  <target> --keep <expected>`. After an upgrade from A to B, `upgrade-<B>`
  holds A, and the previous upgrade left `upgrade-<A>` holding the checkout
  before A. So the two names identify the two most recent previous checkouts
  without timestamps. It removes only real, root-owned, not shared-writable
  directories matching the exact pattern, and refuses while a deployment is
  reserved or unresolved, so a stage being fetched is never touched. The task
  runs outside the block and its rescue, with `failed_when: false`.
- [x] **5. Backlog on the reference Pi** (2026-11-26). Rollout:
  - The tooling was upgraded from `d4ccbcc` to `c646764` (#127) and the idle
    updater restarted.
  - The installed dry run listed 119 directories, about 9.8 GB: backups 40
    (3.9 GB), assets 47 (2.9 GB), updater copies 13 (1.4 GB), incoming 14
    (1.2 GB), restores 5 (0.3 GB). The user reviewed the list.
  - Instead of a separate `--apply`, the backlog was removed by the first
    automatic run: the update `3b437cd` → `c646764` from the page (operation
    `a073a7de`, verified-complete, all five checks passed). It removed the files
    of 46 operations: 121 directories, 9,977 MB, nothing skipped, no error. The
    page showed "Removed the backups of 46 older updates (9977 MB)." The count
    was higher than the dry run because the update itself became the committed
    operation: `027b449e` moved to previous, and `a97b8190` was no longer kept.
  - Left afterwards: the committed `a073a7de`, the previous `027b449e`, and the
    handover operation `e6513571` (the running project's name); 3 backups,
    2 updater copies, no restore trees. The 17 unidentified `assets`
    directories stayed, and a new dry run finds nothing more to remove.
  - The tooling upgrade from `c646764` to `bb4abbd` (#128) then pruned 17 old
    checkouts (1.3 GB). It kept `upgrade-bb4abbd…` (holding `c646764`) and
    `upgrade-c646764…` (holding `d4ccbcc`), as designed.
  - Disk use: 27 GB (47%) before, 18 GB (31%) after the retention, 16 GB (29%)
    after the pruning.
- [x] **6. Acceptance** (2026-11-26, on the reference Pi):
  - [x] *The next update reports the retention and leaves exactly the current
    and previous operation* (the update to `c646764` above).
  - [x] *The review shows the "Old backups" row.* At the update to `c646764`,
    the review was drawn by the web container still running `3b437cd`, which
    drops the unknown field. At the next update (to `26f4cc6`, #130, which also
    shows sizes from 1 GB in GB), the user saw the row in the review.
  - [x] *A deliberately failed update keeps its backup, and "Restore this
    backup" still works.* The update to `26f4cc6` (operation `41b91eaf`) was
    made to fail as in the earlier failure test: the health and routing checks
    did not pass within the deadline, and the pending state was kept. The
    updater reported `failed` with the restore offered (backup of `c646764`,
    108 MB). The retention did not run, and the backup stayed. The user then
    pressed "Restore this backup": recovery `49f0568f` passed the backend,
    containers, http and playback checks.
  - [x] *The retry after the restore cleans up.* "Retry update to dev-26f4cc6"
    (operation `8562ef5f`) was verified-complete with all five checks. The
    retention removed the files of 2 operations (9 directories, 814 MB): the
    failed `41b91eaf` with its recovery, and `027b449e`, which was no longer the
    previous one.
  - [x] *Final run:* the update to `16ffaae` (#131, Enter confirms the review)
    from the page (operation `6fffde49`) was verified-complete with all five
    checks; the retention removed the files of 1 operation (3 directories,
    307 MB, `a073a7de`). Left: the committed `6fffde49`, the previous
    `8562ef5f` and the handover operation `e6513571`; a dry run finds nothing
    more to remove. Disk use stays at 16 GB (29%).

## Left open

- The 17 `assets` directories without evidence (reviews that never started)
  are small and stay until someone decides by hand.
- Images outside the project namespaces are never removed automatically (see
  the [Docker image cleanup](./general_done_docker-image-cleanup.md)).

## Related

- [Docker image cleanup](./general_done_docker-image-cleanup.md): the image side,
  same trigger and reporting.
- [Container deployment](../documentation/container-deployment.md): storage
  layout, recovery and the preflight space check.

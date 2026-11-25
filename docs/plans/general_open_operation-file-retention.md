# Operation file retention

**Status:** open (2026-11-26). Decisions 1 to 3 confirmed by the user on 2026-11-26;
stage 1 (the retention core with a manual dry run) implemented. Follows the
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
- [ ] **2. After each update:** call it after the image cleanup in the operator's
  verified-complete path. Report `{"removed", "kept", "freed_bytes", "error"}`
  next to the image cleanup counts. The page's existing cleanup line covers both,
  or gets one more line.
- [ ] **3. Preflight space check:** keep the existing free-space rules (they
  already require room for a new backup); document that retention runs after
  success, so a full disk still needs the manual command first.
- [ ] **4. Tooling checkouts:** `upgrade-deployment.yml` removes all but the two
  most recent `/opt/mpdrussoundpi-deploy.upgrade-*` after a successful upgrade.
- [ ] **5. Backlog on the reference Pi:** dry run, review the list with the user,
  then `--apply`. Record the sizes before and after.
- [ ] **6. Acceptance:** the next update from the page reports the retention and
  leaves exactly the current and previous operation. A deliberately failed update
  keeps its backup, and "Restore this backup" still works.

## Related

- [Docker image cleanup](./general_done_docker-image-cleanup.md): the image side,
  same trigger and reporting.
- [Container deployment](../documentation/container-deployment.md): storage
  layout, recovery and the preflight space check.

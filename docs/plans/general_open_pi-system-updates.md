# Pi system updates from the Status page

**Status:** open (2026-11-26). Decisions 1 to 4 confirmed by the user on
2026-11-26; stages 1 to 4 implemented on 2026-11-26 (`tool/system_updates.py`,
the "Pi system" section of the Status page, the automatic security updates in
`deploy-updater.yml`), not yet rolled out. Takes the Pi package part and the reboot
action out of [Update status and controlled restarts](./general_open_update-status-and-restarts.md)
and builds them like the
[user-triggered image updates](./general_open_user-triggered-image-updates.md):
the administrator updates the Pi from the Status page, without OS access.

## Why

The reference Pi (Raspberry Pi 4, Debian 13 "trixie", kernel 6.18.29) had
496 pending package updates on 2026-11-26, after 10 days without a reboot, and
nothing installs them: `unattended-upgrades` is not installed, and the only
way is an SSH session with `apt-get`. Among them are the kernel and firmware
(6.18.60), Docker 29.7 → 29.10, systemd, openssh, sudo, libc and the audio
libraries. A user of the appliance should not need a shell for this, and
security fixes should not wait for someone to remember.

## Decisions (confirmed by the user on 2026-11-26)

1. **Debian security updates install automatically, without an automatic
   reboot.** `unattended-upgrades` installs only packages whose candidate
   version comes from the `Debian-Security` origin, once a day. When one needs a
   reboot, the Status page says so and offers the restart button (decision 3).
   The alternatives were a reboot at a fixed night time, or announcing only.
2. **"Install system updates" on the page installs everything pending**: all
   configured repositories (Debian, Debian security and updates, Raspberry Pi,
   Docker), including the kernel, firmware and the Docker engine, as a
   `full-upgrade` that keeps changed configuration files. A Docker upgrade
   restarts both containers once. The alternative was everything except Docker.
3. **A reboot is a separate action**: after an update that needs one, the page
   shows "Restart needed" and a "Restart the Pi" button with its own typed
   confirmation, so the administrator chooses when playback stops. The
   alternative was rebooting as part of the update.
4. **Counts are public, details need the login.** Everyone sees "N system
   updates (M security), restart needed" next to the image check; package
   names, the last automatic run's details and the install and restart buttons
   need the administrator login. The alternative was everything
   administrator-only.

## Facts that shape the design

- **What counts as security.** Raspberry Pi OS lists Debian's
  `trixie-security` (`o=Debian, l=Debian-Security`), the Raspberry Pi
  repository, Docker's repository, and Debian `stable`/`stable-updates`. The
  simulated upgrade names only one origin per package (`openssh-server` shows
  as `Debian:13.8/stable` although the same version is also in
  `trixie-security`), so a package counts as a security update when its
  candidate version is offered by the `Debian-Security` origin, which is the
  rule `unattended-upgrades` uses too. The Raspberry Pi repository has no
  security label: the kernel, firmware and its rebuilds (on the Pi, `libc6` is
  a Raspberry Pi rebuild) come only through the manual update.
- **There is no undo.** Unlike an image update, a package upgrade has no backup
  and no restore. The review says so. The containers' data is not touched.
- **When a restart is needed.** Debian does not reliably create
  `/run/reboot-required` (it was absent on the Pi). The updater reports a restart
  as needed when that file exists, or when a package from a fixed list (the
  kernel images, `raspi-firmware`, the `firmware-*` device firmware, `libc6`,
  `systemd`, `dbus`) was installed or
  upgraded after the current boot (from the dpkg log and the boot time).
- **A Docker upgrade restarts the web container**, which serves the page. The
  page already reconnects during image updates; the same applies here. The
  updater is a host service and keeps running.
- **apt must survive the updater.** An upgrade of Python, systemd or the
  updater's own dependencies can restart the updater. The upgrade therefore runs
  as its own transient systemd unit, and the updater records the operation
  durably and reads the unit's state and log, also after its own restart.

## Rules

- The updater service stays the only privileged component. The web container
  gets three more fixed requests on the existing socket (`system_status`,
  `system_update` review/approve/cancel, `reboot` review/approve/cancel); no
  package names, commands or arguments are accepted from the web.
- An install or a restart is refused while another operation runs: an image
  update or restore, an SSH operator run (the operator lock), a reserved or
  pending deployment, a running `unattended-upgrades` or any other holder of the
  dpkg lock. An image update is likewise refused during a system update.
- Each install and restart is reviewed and approved with the typed word, like an
  image update, and recorded with the administrator and the time.
- The upgrade runs non-interactively (`DEBIAN_FRONTEND=noninteractive`,
  `--force-confdef --force-confold`); kept configuration files whose package
  ships a new version (`*.dpkg-dist`) are listed in the result. An interrupted
  dpkg (`dpkg --audit` not clean) is reported as "outcome unknown" with the
  command to finish it; the updater does not run `dpkg --configure -a` on its
  own.
- After the install the updater checks what an image update checks (containers
  healthy, page reachable, backend connected) and reports failures; it never
  reboots on its own.
- The restart records the boot ID before rebooting. After the boot the updater
  reports success only when the boot ID changed and the checks pass; otherwise
  it reports what is missing.
- The package index is refreshed by the system once a day (as configured for
  `unattended-upgrades`), and by "Check now" for the administrator at most once
  every 10 minutes. A stale or failed index is shown as such and never as "up to
  date".

## Stages

- [x] **1. Read-only check** (implemented 2026-11-26). `system_status` in the updater: pending count,
  security count (rule above), restart needed and why, index age, last
  automatic run; package names and versions for the administrator. The web
  shows the counts in the Software updates block and the overview header, and
  the details in the Administrator panel; "Check now" refreshes the index for
  the administrator. Unit tests with recorded `apt-get -s` and
  `apt-cache policy` output, stale and failed indexes.
- [x] **2. Automatic security updates** (implemented 2026-11-26). A playbook task installs
  `unattended-upgrades` with `Debian-Security` as the only allowed origin,
  `Automatic-Reboot "false"`, the daily index refresh and the daily upgrade,
  and its own log. The page shows the last automatic run (time, packages,
  result) from that log.
- [x] **3. Install from the page** (implemented 2026-11-26). Review (counts, security count, Docker
  restart note, "restart needed afterwards" when a listed package is included,
  no undo), approval, the transient unit, progress, result with kept
  configuration files, checks, and recovery after an updater restart or an
  interrupted dpkg. Tests for the refusals, the restart of the updater during
  the upgrade, and a failed package.
- [x] **4. Restart the Pi** (implemented 2026-11-26). The button when a restart is needed, the review
  (expected interruption about one to two minutes), the boot ID check and the
  checks after the boot.
- [ ] **5. Rollout and acceptance on the reference Pi.** The 496 pending
  packages are the first real install: the check shows them, the install
  succeeds from the page with the containers healthy, the restart from the page
  boots kernel 6.18.60 and everything comes back. Then one automatic security run
  is observed on the page.
  - [x] Rolled out on 2026-11-26: tooling `f75d67f`, `deploy-updater.yml`
    (unit with `Wants=docker.service`, `unattended-upgrades` allowing only
    `Debian-Security`, `Automatic-Reboot "false"`), web image `dev-f75d67f`.
  - [x] Check: 496 upgrades, 37 new, 2 removals, 122 security, shown on the page.
  - [x] Install from the page (operation `07ecc2e9`, 19:16 to 19:45 UTC): apt
    exit code 0, `dpkg --audit` clean, no kept configuration files, Docker
    29.10.1 restarted both containers, which came back healthy. Reported as
    "outcome unknown" first (findings below); after the fix the updater picked it
    up again and reported "installed", checks passed, restart needed.
  - [x] Restart from the page (19:52 UTC): kernel 6.18.60, "restarted", checks
    passed, both containers healthy, all `mpdrussoundpi-*` services active,
    HTTPS 200, `/run/reboot-required` gone.
  - [ ] One automatic security run seen on the page (needs the next Debian
    security update).

## Implementation notes (2026-11-26)

- **Where it shows.** A separate "Pi system" section directly below "Software
  updates", with its own state pill ("N updates", "Restart needed", "Up to
  date", "Updating", "Restarting", "Problem"), instead of rows inside the image
  block: the two have different actions and outcomes. Hidden where the updater
  is not installed. The overview header's update icon also shows pending system
  updates or a needed restart.
- **The review** shows the counts with security, removed packages, the age of
  the package lists, a Docker note when the Docker engine is included, "A restart
  is needed afterwards" when a listed package is included, the interruption, the
  configuration note and "There is no undo"; it is confirmed by typing
  `install` (or `restart` for the restart), Enter included.
- **Binding.** The confirmation digest is a hash of the planned package list;
  right before the start the list is computed again, and a different list
  cancels with "review them again".
- **Checks after the install and the restart** compare with a snapshot taken
  right before: every container that ran must run again and not be unhealthy or
  still starting, and every active `mpdrussoundpi-*` service and Docker must be
  active again, within 5 minutes. A failure is reported ("These did not come
  back: …"), never repaired automatically.
- **Index refresh.** The updater recomputes its cached result when the package
  lists or dpkg's status change, and at least every 6 hours; it refreshes the
  lists itself only for "Check now". The daily refresh comes from the
  `APT::Periodic` settings of stage 2.
- **The updater unit** changes from `Requires=docker.service` to
  `Wants=docker.service`: with `Requires`, the Docker package stopping Docker
  during its upgrade would stop the updater cleanly, and `Restart=on-failure`
  would not bring it back. The install itself never depended on it (its own
  unit), but the watching and the checks afterwards do.
- **Who may act.** Like an image update: the session and the CSRF token, no
  second password prompt.

## Findings of the first real install (2026-11-26)

- **The watcher gave up while apt still ran.** Operation `07ecc2e9` (496
  upgrades, 37 new, 2 removals, 122 security) was reported as "outcome unknown"
  at 19:28 UTC while dpkg was still unpacking: one `systemctl is-active` call
  failed while dpkg replaced `libssl3t64` right after `libc6`, and the watcher
  read the failure as "the unit ended". Fixed: the unit's cgroup under
  `/sys/fs/cgroup/system.slice` counts as running, an unclear `systemctl`
  answer keeps watching, and "ended" needs three agreeing observations 10 s
  apart. An install recorded as "outcome unknown" whose unit still runs or which
  has written its exit code since is watched again after the updater restarts.
  An image update is also refused while anything holds the dpkg lock, not only
  during a recorded system operation.
- **The package list lost its scroll position** (and whether it was open) on
  every refresh of the page, so a long list could not be read. Fixed: both carry
  over to the rebuilt list.
- **The review appeared late.** It was ready about 52 s after "Install system
  updates", but the page showed it with only about 20 s of the 120 s left
  (started 19:13:55, approved 19:16:17 UTC; the page had just asked for the
  login, and a page in a background tab polls late). Fixed: the 120 s start when
  an administrator's page first receives the review (`system_status` with
  `seen`, sent only by the administrator endpoint); a review nobody fetches ends
  after 10 minutes. The package list is still compared again right before the
  install. The page says that computing takes about a minute.
- **"There is no pending review for this operation"** after confirming the
  restart, which did start: the confirmation row stayed enabled, so a second
  Enter or click sent a second approval. Fixed for every review (image update,
  restore, install, restart): the row is disabled once confirmed or cancelled and
  enabled again only when the request fails.
- **The finish time of a recovered install** was the time the updater noticed
  it. Fixed: the time apt wrote its exit code.
- **Device firmware did not ask for a restart.** The second install (operation
  `26b98ef4`, 8 packages on 2026-11-26) updated five `firmware-*` packages,
  among them `firmware-brcm80211` for the onboard Wi-Fi the reference Pi uses,
  and the page said no restart was needed although the old firmware stays loaded
  until one. Fixed: `firmware-*` packages count like the kernel, and the
  restart reason is no longer cut at 100 characters.

## Related

- [Update status and controlled restarts](./general_open_update-status-and-restarts.md):
  the broader idea (service restarts, other hosts); its Pi package and reboot
  items move here.
- [User-triggered image updates](./general_open_user-triggered-image-updates.md):
  the updater service, the review and approval flow and the reconnection.
- [Container deployment](../documentation/container-deployment.md): the updater
  and the deployment state.

# Administrator mode: global sign-in and protected settings

**Status:** open. Planned and decided on 2026-11-22. Stages 1 to 4 implemented on 2026-11-24 (after a local preview session): configuration saves need the administrator, the password can be changed on the page. Acceptance on the Pi (stage 5) remains. The
mockups are in the session that produced this plan; their decisions are recorded
below. Related to [user-triggered image updates](./general_open_user-triggered-image-updates.md)
(the first user of the administrator account) and
[safe configuration changes](./web_open_safe-configuration-changes.md).

## Why

Today the administrator login is a section of the Status page, inside the update
workflow. That has three problems:

- Sign-in is tied to one feature. Nothing else can use the account.
- The configuration page is open to anybody on the network. It writes the whole
  installation (controller address, zones, sources, shortcuts).
- Nothing on other pages tells an administrator that the session is still
  elevated.

## Decisions (user, 2026-11-22)

1. **Sign-in is global.** A shield button next to Menu in the existing header,
   plus one row at the bottom of the existing Menu. The Administrator section
   leaves the Status page. The header design and the page menu stay.
2. **Session:** ends after **15 minutes without activity**, or when the user
   leaves admin mode. (Already implemented: `IDLE_SECONDS`; the absolute limit
   of 8 hours stays.)
3. **Guests may see all settings.** Editing needs the account. Hiding
   sensitive fields from guests is a possible later step, not now.
4. **Password again only for two actions:** changing the administrator password
   and restoring the previous version. Installing an update and saving settings
   do not ask again.
5. **Elevation is visible everywhere**, in amber (a colour used for nothing
   else): a frame around the window, a sticky strip with the countdown and an
   exit, the shield button, and a small shield on every privileged action.
   It must work on a phone and take no layout space.

## Design

### Server

Already present in `web/admin_auth.py` and `web/russound_server.py`: sessions,
idle and absolute expiry, lockout, CSRF token, `authorize(..., recent=)` and
`POST /api/admin/reauthenticate` (valid for `REAUTH_SECONDS`, 5 minutes).

Changes:

- **Settings writes need the account.** `POST /api/config` calls
  `admin_required()` (session and CSRF) before the revision check. Without a
  session: `403` with the existing error codes. Reading (`GET /api/config`)
  stays public.
- **Reauthentication only where decided.** `recent=True` stays on restore.
  It is removed from update review and approve (today all three require it). It
  is added to the new password change.
- **Other mutating endpoints** (`/api/system/power`, `/api/source`,
  `/api/shortcuts/*/activate`, zone controls, playback operations) are
  everyday controls and stay open for guests. Their protection is not part of
  this plan; the list above is the decision record.
- **Change password** (new): `POST /api/admin/password` with the current and new
  password, `recent=True`. It validates the new one with the existing length
  rules, writes the scrypt hash atomically, and ends every other session
  (the file generation already invalidates sessions). It is audited without the
  password.
  - **Open check first:** the hash file is mounted into the web container with
    mode 0400 (README, Administrator login). The container may not be able to
    replace it. Verify on the Pi; if it cannot, the options are a writable
    secrets directory for this one file or leaving the change to the existing
    `python -m web.admin_auth set` and showing that instruction in the page.
- `GET /api/admin/session` already reports `idle_expires_in_seconds`; the page
  uses it for the countdown and extends the session by any admin request.
  **Activity** means a request to an administrator endpoint or the explicit
  Extend button. Plain page polling must not keep the session alive. This is
  how `touch=False` is already used for `describe`; confirm that for every
  poll.

### Page

- **Shared module** `static/admin.js` and styles, loaded by every page, owning:
  the session state, the shield button, the Menu row, the login sheet, the
  strip, the countdown, logout and the "idle" drop to guest. No page keeps its
  own login code.
- **Login sheet** opens where the user is and says what they asked to do
  ("You asked to: Edit settings"), then resumes it. On phones it is a bottom
  sheet. The plain-HTTP warning (only shown on HTTP) and the HTTPS link for
  `https_required` move into it.
- **Amber elevation**:
  - frame: a fixed overlay with `pointer-events: none` and inset border;
  - strip: sticky above the header, shortened text on narrow screens, Extend
    appears in the last minute and the strip turns orange;
  - header button: grey lock for guests; amber shield with the countdown
    while elevated (word "Administrator" only on wide screens);
  - Menu row: "Sign in as administrator" / "Exit admin mode";
  - shield marker on privileged buttons and settings section headings.
- **Status page:** the Administrator section is removed. The update card is
  visible to everyone. Install and restore are disabled with "Sign in to
  install updates" for guests, which opens the sheet. The
  `adminUpdate` logic in `status.js` keeps its behaviour but reads the shared
  session.
- **Configuration page:** read-only for guests (inputs `readonly`, a lock box
  with a Sign in button), editable while elevated, one sticky Save bar for the
  whole page (the page already saves the whole configuration with the revision
  header). When the session ends during editing, the edits stay visible,
  read-only, and Save is disabled until the user signs in again; the revision
  check still protects against stale writes.
- **Reauthentication prompt:** the existing inline prompt (`showReauthPrompt`)
  is reused for restore and password change.
- **Accessibility and touch:** targets of at least 44 px; the strip and the
  frame are not the only signal (the shield button has an accessible name and
  state); `prefers-reduced-motion` respected; no layout shift when signing in
  or out.

### Documentation and tests

- `web/API.md`: `POST /api/config` requires the administrator session and
  `X-CSRF-Token`; the reauthentication rules per endpoint; `POST
  /api/admin/password`.
- `README.md` (Administrator login) and the container deployment documentation:
  the global sign-in, the session rules and who may change what.
- Tests: `tests/test_admin_auth.py` and the server tests for the config write
  (401/403/csrf, accepted with a session), reauthentication only on restore and
  password change, activity extending the session and polling not. Page tests
  for the shared module (states, countdown, idle drop, resume after login) and
  the Status and Configuration page flows.

## Stages

- [x] **1. Server.**
  - [x] update review and approve no longer ask for the password again (only a
    restore does); `POST /api/admin/extend` for "Extend" (2026-11-24);
  - [x] `POST /api/config` needs the administrator session and `X-CSRF-Token`
    once an administrator password exists; without one (first setup) it stays
    open (2026-11-24);
  - [x] password change from the page (2026-11-24). The secrets directory is
    read-only in the container (checked on the Pi), so a changed password is
    written next to the state (`/data/russound-admin.scrypt`, writable, no
    deployment change) and wins over the deployment's file. Menu → *Change
    password* while signed in: current password, the new one twice; this
    session continues, every other one ends; wrong current passwords count
    towards the lockout. The login dialog shows the SSH reset command under
    "Forgot the password?" (`docker exec … python -m web.admin_auth set --file
    /data/russound-admin.scrypt`, checked on the Pi with `--help`).
- [x] **2. Shared admin module** (2026-11-24): `web/static/admin.js` on every
      page. Tuned in a local preview session with the user:
  - amber frame and sticky strip ("Administrator mode", countdown, Extend in
    the last minute, Exit); the strip no longer says "changes affect the whole
    installation";
  - **no header button:** the strip already shows the state, so the header
    holds only the title and Menu. Sign-in and exit are a row in the Menu;
  - **login as a modal dialog** (`<dialog>`, a bottom sheet on phones), opened
    from the Menu or from any privileged button. It says what was asked and
    continues the action after signing in. It also handles the password
    confirmation for a restore, the HTTPS-only case and lockout. Login errors
    appear only in the dialog;
  - privileged buttons are amber with the shield for guests too;
  - menu entries got icons.
- [x] **3. Status page** (2026-11-24):
  - the Administrator section is gone; "Software updates" shows the version
    overview, "Check now" in one group with the age of the last check, and
    "Update to …" (after a failure "Retry update to …");
  - a separate **"Restore backup"** section lists the restorable backups with
    when they were taken, version and size (updater field `backups`, ready
    for more than one). It opens by itself after a failed update and for a
    pending restore review;
  - reviews show short image digests and short updater texts;
  - level badges have icons (also the connection states on the Configuration
    and Playback pages); events and license tables become cards on phones;
  - all page headers are consistent ("Russound …"; title, Menu; banners below).
- [x] **4. Configuration page** (2026-11-24): the save button is replaced by a
      save bar at the bottom inside the amber frame, shown only with unsaved
      changes. It reports success (shrinks away after 2.5 s) or failure (stays,
      with the buttons). Guests see a read-only view: every editor control is
      disabled (values stay readable, dashed frames), a note offers the login,
      the zones' live sound controls stay usable. Saving sends the session's
      token; an expired session opens the login and then saves the same edits.
- [ ] **5. Acceptance on the Pi**, desktop and phone: sign in from each page,
      edit and save settings, a guest write is refused, idle logout after 15
      minutes, manual exit, restore asks for the password, update install does
      not, password change, lockout after wrong passwords, keyboard use and a
      screen reader pass over the dialog and the strip.
  - [x] rolled out with `979da5f` on 2026-11-24 (web update over HTTPS; tooling
    upgraded first for the updater's `backups` field). The user confirmed on the
    Mac and the phone: the page reloaded into the new version, the login dialog
    (bottom sheet on the phone) and the amber mode work, and the Configuration
    save bar appears with changes and saves;
  - [ ] the rest above, together with stage 1.

Order: stage 1 closes the open configuration page for guests, so it ships with
the rest of stage 4.

## Risks and notes

- **HTTPS (done 2026-11-23).** The UI runs over HTTPS with a local CA, and the
  server refuses logins over plain HTTP
  ([plan](./general_done_web-ui-https.md)). The sheet keeps the plain-HTTP
  warning only for installations without the HTTPS proxy, and the shared
  admin module must handle the `https_required` answer (link to the HTTPS
  address instead of a password field), as the Status page already does.
- **One shared password**, no user names; the audit log records a client address
  and a short session reference only.
- **Lock-out of the owner.** Password change must verify the current password
  and keep the old hash until the new file is written and re-read.
- **Phone header.** The real header stacks vertically below 700 px. The mockups
  kept title and buttons in one row; decide while implementing stage 2.

## Later

- Hide sensitive settings (addresses, device names) from guests.
- Protect everyday controls (power, source) if the installation needs it.
- Second factor or per-user accounts.

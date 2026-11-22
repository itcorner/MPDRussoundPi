# Administrator mode: global sign-in and protected settings

**Status:** open. Planned and decided on 2026-11-22; nothing implemented yet. The
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
  sheet. The plain-HTTP warning moves into it.
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

- [ ] **1. Server.** Admin check on `POST /api/config`; reauthentication moved
      to restore and password change; password change endpoint (after the mount
      check); tests and `web/API.md`.
- [ ] **2. Shared admin module and header.** `admin.js`, login sheet, shield
      button, Menu row, strip, frame, countdown; tests; phone check.
- [ ] **3. Status page.** Remove the Administrator section; guest and
      elevated states of the update card.
- [ ] **4. Configuration page.** Read-only guest view, edit mode, Save bar,
      behaviour on session end; tests.
- [ ] **5. Acceptance on the Pi**, desktop and phone: sign in from each page,
      edit and save settings, a guest write is refused, idle logout after 15
      minutes, manual exit, restore asks for the password, update install does
      not, password change, lockout after wrong passwords, keyboard use and a
      screen reader pass over the sheet and the strip.

Order: stage 1 can ship alone and is safe, but it closes the open configuration
page for guests, so ship it together with stage 4 (otherwise nobody can save
settings from the page). Stages 2 and 3 can go first.

## Risks and notes

- **Plain HTTP.** The password and the session cookie can be observed on the
  network (documented limitation, HTTPS is a separate open item). Settings
  protection is therefore only as strong as the network. Say so on the sheet.
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

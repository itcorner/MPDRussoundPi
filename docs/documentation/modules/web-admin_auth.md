# `web/admin_auth.py`

## Documentation

Administrator login for privileged actions, stage 1 of the
[web-triggered update plan](../../plans/general_new_user-triggered-image-updates.md).
One shared password, stored only as an scrypt hash (memory-hard, per-hash salt)
in a file in the secrets mount. The file is re-read when it changes, so a local
reset with `python -m web.admin_auth set` (or `hash` piped into the secrets
file) takes effect without a restart and revokes every session and the lockout,
both of which are bound to the hash's generation.

`AdminAuth` keeps the global lockout (five failures, then 15 minutes; attempts
during the lock extend it; persisted in `admin-lockout.json` next to the state
file, robust to restarts and clock rollback), a per-client throttle, in-memory
sessions keyed by the hash of an opaque id (15 minutes idle, 8 hours absolute,
rotated on login), a synchronizer CSRF token per session and the
"recently authenticated" window (5 minutes) for disruptive actions. The server's
`admin_required(recent=...)` is the single check every privileged route calls.
Audit records (login, failures, lock, logout, reauthentication) go to the web log
and `admin-audit.log`; the password, cookie and token are never logged.
Sessions do not survive a web restart.

The UI runs over plain HTTP for now (plan decision 6); the cookie gets `Secure`
automatically when the request arrives over HTTPS.

## Requirements

- Python 3.11+, standard library only (`hashlib.scrypt`).

## Test Cases

[Administrator login tests](../../../tests/test_admin_auth.py) cover hashing and
malformed hashes, the global lockout and its extension, persistence and clock
rollback, local reset, idle and absolute expiry, reauthentication, the per-client
throttle, the audit log without secrets, the local command, the endpoints with
cookie attributes, CSRF and same-origin rejection, `admin_required` on a
privileged route, and that the password never reaches logs or frontend events.

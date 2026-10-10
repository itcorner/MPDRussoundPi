# Long-uptime reliability backlog

Findings from a code review of the web interface (2026-10-10) for memory leaks
and other issues that degrade the service over long uptimes. Work through the
items in order; tick the checkbox and note the commit when an item is done.

Priority: **H** = will eventually break functionality, **M** = gradual
degradation / resource growth, **L** = edge cases.

| # | Prio | Topic | Status |
|---|------|-------|--------|
| 1 | H | Update listener not restarted after reconnect | [x] |
| 2 | H | Update listener never started if gateway is down at boot | [x] |
| 3 | H | Peer close / half-open TCP not detected | [x] |
| 4 | H | Open UI pages die silently after server restart (API token) | [ ] |
| 5 | H | SSE streams pin waitress worker threads | [ ] |
| 6 | M | Protocol audit log grows unbounded | [ ] |
| 7 | M | SSE client registry never pruned | [ ] |
| 8 | M | Unbounded per-client SSE queues | [ ] |
| 9 | M | Silent zones hold `state_lock` for ~9 s per poll | [ ] |
| 10 | L | Receive buffers unbounded on line noise | [ ] |
| 11 | L | Frontend fetches have no timeout | [ ] |
| 12 | L | `_response_transaction_depth` modified outside the lock | [ ] |

---

## 1. Update listener not restarted after reconnect (H)

> **Done:** `RussoundBackend` keeps the callback and re-attaches it to every
> (re)connected client in `_connect()`.

- **Where:** `web/russound_connector.py` `_update_listener_loop()`,
  `web/russound_backend.py` `_connect()`.
- **Problem:** On a socket error the listener thread closes the socket and
  exits. The next poll's `_connect()` sees the dead client, calls
  `_close_client()` (which also clears the callback) and creates a **new**
  `Russound` instance without a listener.
- **Impact:** After any ser2net restart, network blip or USB hiccup, instant
  keypad/volume/source/power feedback stops until the container restarts;
  only the regular poll keeps working.
- **Fix idea:** Keep the update callback on `RussoundBackend` and re-attach it
  (`client.start_update_listener(callback)`) whenever `_connect()` creates a
  new client. Optionally let the listener trigger a reconnect itself.
- **Verify:** Unit test: simulate a connection reset, call `connect()` again,
  assert the new client has a running listener with the original callback.
  Manual: restart ser2net, press a keypad key, UI updates instantly.

## 2. Update listener never started if gateway is down at boot (H)

> **Done:** the shared backend is always created; the watcher loop calls
> `_ensure_update_listener()` at least every 5 s and reconnects with
> exponential backoff (5 s → 60 s).

- **Where:** `web/russound_server.py` `_start_unsolicited_update_listener()`.
- **Problem:** Tried exactly once at startup; if `start_update_listener()`
  fails, it returns and is never retried. Without a shared backend every
  operation also opens a fresh TCP connection (ser2net allows one connection
  by default).
- **Impact:** Typical boot-order race (Pi boots, ser2net / USB adapter ready
  later) disables instant updates for the whole uptime.
- **Fix idea:** Retry from the watcher loop while `_event_backend is None`
  (with backoff), or always create the shared backend and let item 1's
  reconnect logic start the listener once connected.
- **Verify:** Start web container with gateway unreachable, then start
  ser2net; instant updates must work without restarting the web container.

## 3. Peer close / half-open TCP not detected (H)

> **Done:** empty TCP reads reset the connection; TCP keepalive
> (30 s idle / 10 s interval / 3 probes) is enabled on connect.

- **Where:** `web/russound_connector.py` `_read_available_locked()`,
  `connect()`.
- **Problem:** A non-blocking `recv()` returning `b""` means the peer closed
  the connection, but it is treated as "no data". No TCP keepalive is set, so
  a silently dead link (Wi-Fi drop, Pi reboot) is only noticed after TCP
  retransmission timeouts (~15–30 min).
- **Impact:** Stale connection, polls time out, UI shows "hardware not
  responding" although hardware is fine; combined with item 1 the listener is
  lost afterwards.
- **Fix idea:** For TCP sockets treat `b""` as disconnect
  (`_reset_connection_on_error()` + raise). Enable `SO_KEEPALIVE` and, on
  Linux, `TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT` (e.g. 30 s / 10 s / 3).
  Do not apply the `b""` rule to the serial connection.
- **Verify:** Unit test with a fake socket returning `b""` → socket reset.
  Manual: restart ser2net, connection re-established within seconds.

## 4. Open UI pages die silently after server restart (H)

- **Where:** `web/russound_server.py` (`self.api_token =
  secrets.token_urlsafe(32)` per process), `web/static/app.js`,
  `web/static/status.js`, `web/static/config.js`.
- **Problem:** The token is regenerated on every start. Already open pages
  keep the old token → every API call returns 401; `EventSource` gets 401 and
  stops reconnecting permanently. `fetchState()` silently ignores non-OK
  responses.
- **Impact:** Wall tablets / phones show stale state without any indication
  until manually reloaded.
- **Fix idea:** In the frontend, on a 401 (fetch) or on a closed
  `EventSource`, reload the page (rate-limited) to obtain the new token.
  Alternative: persist the token across restarts (e.g. env var or file in
  `/data`).
- **Verify:** Open UI, restart container, UI recovers automatically and
  reflects new state.

## 5. SSE streams pin waitress worker threads (H)

- **Where:** `web/russound_server.py` `_serve_events()` / `event_stream()`;
  waitress `threads=16` default.
- **Problem:** Each open SSE stream occupies a worker thread for its whole
  lifetime. Half-open connections (sleeping phones) keep their thread until
  TCP gives up. When the same session reconnects, the old generator keeps
  running (only its registry entry is replaced).
- **Impact:** A few devices repeatedly sleeping/waking can exhaust all
  threads → all pages and API requests hang.
- **Fix idea:**
  - In `event_stream()`, exit when the client's current `connection_id` no
    longer matches this stream's id (superseded by a reconnect).
  - Cap stream lifetime (e.g. 5 min); `EventSource` reconnects automatically.
  - Consider raising `waitress_threads` moderately.
- **Verify:** Unit test: second registration for the same session ends the
  first stream generator. Manual: many reconnects do not grow thread count.

## 6. Protocol audit log grows unbounded (M)

- **Where:** `web/russound_connector.py` `_audit_frame()`.
- **Problem:** Appends forever and opens/closes the file per frame. With the
  10 s poll interval this is roughly 10–15 MB/day plus constant SD-card writes.
- **Fix idea:** Use a dedicated `logging` logger with `RotatingFileHandler`
  (e.g. 5 MB × 3) for the audit file; keep it optional via config.
- **Verify:** Unit test that the audit logger uses a rotating handler; audit
  file size stays bounded.

## 7. SSE client registry never pruned (M)

- **Where:** `web/russound_server.py` `_event_clients`,
  `_event_client_index`, `unregister_event_client()`.
- **Problem:** Entries are only marked inactive, never removed. Each new
  browser, private window or cookie-less client adds an entry forever.
- **Fix idea:** Remove inactive entries (and their index entry) on
  unregister, or prune entries inactive for longer than e.g. 1 h.
- **Verify:** Unit test: register/unregister many sessions → registry size
  bounded.

## 8. Unbounded per-client SSE queues (M)

- **Where:** `web/russound_server.py` `register_event_client()` (`Queue()`),
  `broadcast_state_change()`.
- **Problem:** If a stream is blocked (waitress output buffer full on a dead
  connection), every broadcast adds another full state payload.
- **Fix idea:** Only the latest state matters: use `Queue(maxsize=1)` and
  replace the pending item on `Full` (drop oldest).
- **Verify:** Unit test: many broadcasts to a non-consuming client keep queue
  size ≤ 1 and the newest payload is delivered.

## 9. Silent zones hold `state_lock` for ~9 s per poll (M)

- **Where:** `web/russound_backend.py` `read_zone()`.
- **Problem:** After `get_zone_extended_info()` fails, the fallback issues
  `get_power()`, `get_source()`, `get_volume()` – the same All Zone Info
  request three more times, each with its own ~2.2 s timeout. The watcher
  holds `state_lock` throughout, blocking all API requests.
- **Impact:** With a configured but absent controller (currently controller 2)
  every poll blocks the UI for ~9 s.
- **Fix idea:** Drop the redundant fallback reads (return `None` when the
  extended read fails), and/or skip zones that failed repeatedly for a backoff
  period. Consider releasing `state_lock` during hardware I/O.
- **Verify:** Unit test: failed extended read results in exactly one request.
  Audit log shows one request per silent zone per poll.

## 10. Receive buffers unbounded on line noise (L)

- **Where:** `web/russound_connector.py` `_rx_buffer`, `_audit_rx_buffer`.
- **Problem:** Data without an `F7` terminator accumulates forever.
- **Fix idea:** Cap buffers (e.g. 4 KB); when exceeded, discard up to the
  last `F0` and log.
- **Verify:** Unit test feeding noise without `F7` keeps buffer bounded.

## 11. Frontend fetches have no timeout (L)

- **Where:** `web/static/app.js` `authorizedFetch()`, `fetchState()`.
- **Problem:** A request hanging on a dead connection keeps
  `stateFetchInFlight` true and suppresses the 5 s refresh until the browser
  gives up.
- **Fix idea:** Use `AbortController` with a timeout (e.g. 10 s) in
  `authorizedFetch()`.
- **Verify:** Manual: block the server temporarily, UI resumes polling after
  it returns.

## 12. `_response_transaction_depth` modified outside the lock (L)

- **Where:** `web/russound_connector.py` `get_zone_info()`.
- **Problem:** `+= 1` happens before acquiring `self.lock`; not thread-safe.
  Harmless today, but fragile.
- **Fix idea:** Move increment/decrement inside the `with self.lock:` block.
- **Verify:** Existing tests pass.

---

## Not issues (checked)

- `_event_history` is a `deque(maxlen=50)`.
- State/config persistence writes are atomic (temp file + `replace`).
- Application log uses `RotatingFileHandler` (10 MB × 5).
- Frontend uses event delegation on static containers; re-rendering does not
  accumulate listeners.
- Unhandled RNET frames are dropped (no longer accumulate in
  `_pending_frames`).

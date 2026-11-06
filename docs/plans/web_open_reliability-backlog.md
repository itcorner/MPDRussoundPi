# Web and delivery reliability backlog

Findings from the web reliability review (2026-10-10), reassessed against
`develop` on 2026-11-06. The list covers long-uptime runtime behavior and
delivery-pipeline reliability. Tick items only after implementation and
verification.

Priority: **H** = will eventually break runtime functionality, **M** = gradual
degradation / resource growth or a release pipeline that can be blocked, **L** =
edge cases.

| # | Prio | Topic | Status |
|---|------|-------|--------|
| 1 | H | Update listener not restarted after reconnect | [x] |
| 2 | H | Update listener never started if gateway is down at boot | [x] |
| 3 | H | Peer close / half-open TCP not detected | [x] |
| 4 | H | Open UI pages survive server restart | [x] |
| 5 | H | SSE streams pin waitress worker threads | [x] |
| 6 | M | Protocol audit log grows unbounded | [x] |
| 7 | M | SSE client registry never pruned | [x] |
| 8 | M | Unbounded per-client SSE queues | [x] |
| 9 | M | Nonresponsive zones hold `state_lock` during fallback reads | [x] |
| 10 | L | Receive buffers unbounded on line noise | [x] |
| 11 | L | Frontend fetches have no timeout | [ ] |
| 12 | L | `_response_transaction_depth` modified outside the lock | [ ] |
| 13 | M | Playback freshness threshold test flakes in CI | [ ] |

Reassessment on 2026-11-06: items 1–8 were complete; items 9–12 remained open.
Item 13 was a new delivery-pipeline reliability finding. Focused
backend/connector/server tests passed (177 tests), as did the playback-status
tests (35 tests). Item 6 was fixed and verified on 2026-11-06. Current status
for each finding is recorded in the table below.

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

> **Done:** state-changing requests no longer depend on a per-process API
> token. They require same-origin/browser provenance (or a non-browser request),
> JSON bodies, and optionally an allowed Host. Reopening an existing page after
> a server restart does not leave it with an expired token.

- **Evidence:** `web/russound_server.py` `_is_same_origin_request()` and the
  same-origin request tests in `tests/test_russound_server.py`.

## 5. SSE streams pin waitress worker threads (H)

> **Done:** each stream has its own registry entry; streams have a 5-minute
> lifetime and reconnect with a 2-second retry. Waitress defaults to 24 worker
> threads and reserves four from SSE use; Linux client connections use a
> 30-second TCP user timeout. Pages close streams and pause polling while
> hidden.

- **Evidence:** `web/russound_server.py` stream lifecycle/limits and tests in
  `tests/test_russound_server.py`.

## 6. Protocol audit log grows unbounded (M)

> **Done:** writes go through a dedicated rotating audit logger, bounded to
> 5 MiB plus three backups per configured path. Same-process connector instances
> share a handler, and the final close releases it. The timestamp/direction/frame
> line format and optional configuration remain unchanged.

- **Where:** `web/russound_connector.py` `_audit_frame()` and connector
  lifecycle.
- **Evidence:** `test_protocol_audit_log_rotates_and_reuses_one_handler_per_path`
  and `test_protocol_audit_logs_tx_and_rx_frames` in
  `tests/test_russound_connector.py`.

## 7. SSE client registry never pruned (M)

> **Done:** `unregister_event_client()` removes the stream entry. The status
> endpoint groups simultaneously open streams by browser session without
> retaining completed sessions.

- **Evidence:** `test_unregister_removes_stream_from_registry` and
  `test_status_client_payload_groups_streams_from_same_session` in
  `tests/test_russound_server.py`.

## 8. Unbounded per-client SSE queues (M)

> **Done:** each queue is bounded at `EVENT_QUEUE_MAXSIZE` (8); overflow drains
> queued invalidations before enqueueing the newest event. Playback updates are
> coalesced separately to one outstanding event per stream.

- **Evidence:** `test_playback_sse_is_coalesced_and_client_queue_is_bounded` in
  `tests/test_playback_status.py`.

## 9. Nonresponsive zones can hold `state_lock` during fallback reads (M)

> **Done:** a failed extended zone read no longer triggers three repeats of
> the same All Zone Info request. Full and targeted hardware refreshes run
> outside `state_lock`; their results are committed only when the state,
> configuration, and backend generation still match the captured snapshot.

- **Evidence:** `test_read_zone_does_not_repeat_all_zone_info_when_extended_info_is_missing`,
  `test_backend_poll_does_not_hold_state_lock_during_hardware_read`, and
  `test_backend_poll_does_not_commit_over_concurrent_state_change`.

## 10. Receive buffers unbounded on line noise (L)

> **Done:** incomplete receive and audit buffers are capped at 128 bytes.
> The live `musicpi` audit contained 86 complete frames; its longest was 39
> bytes, so the cap retains over 3x headroom. Overflow discards leading noise
> while retaining a bounded newest frame start, or clears an oversized
> incomplete frame, and logs the discard.

- **Evidence:** `test_receive_and_audit_buffers_discard_unterminated_noise` and
  `test_receive_and_audit_buffers_keep_only_a_bounded_latest_frame` in
  `tests/test_russound_connector.py`.

## 11. Frontend fetches have no timeout (L)

- **Where:** `apiFetch()` in `web/static/app.js`, `status.js`, `config.js`, and
  `playback.js`. Some playback operations and station search wrap the fetch
  with `AbortController`, but ordinary reads, polling and other requests do
  not.
- **Problem:** A request hanging on a dead connection keeps
  `stateFetchInFlight` / `statusFetchInFlight` true and suppresses later
  polling until the browser gives up.
- **Fix idea:** Add a shared timeout/abort policy to `apiFetch()` while
  preserving caller cancellation and requests that intentionally outlive the
  default timeout.
- **Verify:** Test a stalled fetch, ensure the in-flight flag clears, then
  verify the next poll recovers after the server returns.

## 12. `_response_transaction_depth` modified outside the lock (L)

- **Where:** `web/russound_connector.py` `get_zone_info()`.
- **Problem:** `+= 1` happens before acquiring `self.lock`; not thread-safe.
  Harmless today, but fragile.
- **Fix idea:** Move increment/decrement inside the `with self.lock:` block.
- **Verify:** Existing tests pass.

---

## 13. Playback freshness threshold test flakes in CI (M)

- **Where:** `tests/test_playbackstatus.py`,
  `test_freshness_uses_monotonic_time_and_exact_thresholds`.
- **Evidence:** The test failed in
  [develop run 41739176843](https://github.com/itcorner/MPDRussoundPi/actions/runs/41739176843)
  before the first image upload attempt, and again in
  [develop run 42260788316](https://github.com/itcorner/MPDRussoundPi/actions/runs/42260788316)
  after development publishing was re-enabled. In the latter run, the exact
  threshold assertion observed route status `not_applicable` rather than
  `pending`; image building and publication were skipped. Related validation
  runs passed, so this is intermittent rather than a consistently failing test.
- **Impact:** A transient test failure blocks image builds and publication for
  an otherwise eligible `develop` push.
- **Next step:** Make this boundary test deterministic across playback/audio
  collector jobs and avoid relying on wall-clock scheduling at the exact
  threshold. Re-run it repeatedly and verify the full validation workflow.

---

## Not issues (checked)

Rechecked on 2026-11-06; no new long-uptime runtime issue was confirmed in this
bounded pass beyond the open items above.

- `_event_history` is a `deque(maxlen=50)`.
- State/config persistence writes are atomic (temp file + `replace`).
- Application log uses `RotatingFileHandler` (10 MB × 5).
- Frontend uses event delegation on static containers; re-rendering does not
  accumulate listeners.
- Unhandled RNET frames are dropped (no longer accumulate in
  `_pending_frames`).

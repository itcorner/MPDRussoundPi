# Audio diagnostic freshness: incident, correction and acceptance

Status: **fix deployed; 15-minute diagnosis and repeat six-hour acceptance passed**
(2026-11-14). This incident is separate from guided first-time setup.

## Finding and provenance

The approved Pi deployment's six-hour read-only observation completed at
07:29:07 CET, with 349 samples over 21,600.258 seconds. Four web-API assertion
failures at approximately 02:18, 03:08, 05:58 and 06:30 CET failed its zero-failure
acceptance gate. Original records retained only `AssertionError`, not individual
response fields, so their exact failed conditions cannot be reconstructed.
Each next sample passed. Container/native-service identities, zero restarts,
protected files and independent gateway TCP/authenticated TLS passed during those
failed samples. No evidence establishes an audible playback interruption.

The defect predates the reviewed deployment workflow:

- Commit `8afe01d` (2026-10-15) introduced the 30-second audio freshness threshold.
- Commit `cafb548` (2026-10-15) introduced the 30-second agent audio refresh.
- The current native agent matched the checkout byte-for-byte before this fix:
  SHA-256 `b55f756a2d79e44b0009b03f59e206ccf1af9aad521cf3715ce609db549a597a`
  for `lib/playbackagent.py`.
- The recent web/ser2net rollout did not replace the native playback agent.
  Prior installations using the same agent had this timing defect; comparable
  historical failures are not proven without retained observations.

## Improved observation and confirmed failure

Original Pi evidence is under
`~/.local/state/mpdrussoundpi-acceptance-20261114/observation/`.
A separate observer version 2 uses individually named checks and allowlisted
timestamps, statuses, ages and error codes; no full config, credential or raw
exception payload is logged. Six synthetic observer tests passed.

The separate unchanged-runtime diagnosis began at approximately 08:31:56 CET,
under `russound-diagnostic-20261114-v2.service`, with records in
`~/.local/state/mpdrussoundpi-diagnostic-20261114-v2/`. API reads occur about
every five seconds; full identity/service/protected-file/TCP/TLS checks about
once a minute. Preflight immediately captured ready/correct but stale audio
observations at approximately 30.1-30.2 seconds old.

Recurring samples at 08:47:26, 08:48:56 and 08:49:26 CET failed only readiness
freshness for all three outputs:

- `status: ok`, `ready: true`, no observation error.
- Readiness ages 30,002-30,144 ms; routes still correct/fresh at 29,870-29,966 ms.
- Controller connectivity, agent health and inventory freshness passed.
- Next samples about five seconds later passed with new observations
  approximately 4.2-4.4 seconds old.

These new records prove a readiness freshness gap; they do not identify the
four original assertions retroactively.

## Root cause

[Agent scheduling](../../mpdrussoundpi/russound/lib/playbackagent.py) refreshed
audio diagnostics every 30 seconds, while
[probe serialization](../../mpdrussoundpi/russound/lib/playbackstatus.py) marks
them stale at age >=30 seconds. Scheduling drift, asynchronous collection and
publication after readiness/route/gain allow an old successful probe to expire
before its replacement appears. The web's approximately two-second cached
polling can preserve the stale flag until another successful poll.

An isolated mocked-worker test against the deployed agent reproduced the old
successful observation becoming stale while a replacement job was scheduled,
then becoming fresh after replacement publication. No hardware calls were made
by that test.

## Required correction and invariants

1. Replace the hard-coded scheduler interval with
   `AUDIO_REFRESH_INTERVAL_SECONDS = 20.0`.
2. Keep all 30-second stale thresholds and observer acceptance checks unchanged.
3. Preserve collector per-output job deduplication, separate audio/playback
   pools, failure reporting and all read-only behavior.
4. Add regression coverage for the 20-second scheduling boundary, delayed
   successful publication and old/failed observations still becoming stale.
5. Expect roughly 50% more periodic audio diagnostic submissions; verify actual
   probe ages and failures on the Pi. Ten seconds is headroom, not a guarantee
   under arbitrary load or genuinely blocked hardware.

Only the native agent helper needs deployment; web/ser2net images, token, config,
playback state, proxy, MPD, PipeWire and supervisor must remain unchanged.
Restart only the agent to activate its imported helper. Preserve the previous
helper and before/after identities so the intervention is auditable.

## Delivery and evidence gates

- [x] Retain original failed six-hour evidence.
- [x] Name observer conditions and capture recurring unchanged-runtime failures.
- [x] Verify installed source and reproduce scheduler/freshness race.
- [x] Implement 20-second scheduler correction and regression tests.
- [x] Run focused agent/collector/client/deployment tests and record results.
- [x] Preserve/stop pre-fix diagnosis before the explicitly approved agent update.
- [x] Deploy exact corrected helper and verify hash, agent restart and health.
- [x] Capture a new post-change baseline; explicitly account for the one agent
  PID change rather than hiding other identity/config changes.
- [x] Run a **full 900-second diagnostic**, API interval five seconds, full checks
  about every minute, zero failed samples; retain completion and service exit.
- [x] Only after the short run passes, start repeat **21,600-second acceptance**
  with the improved observer, unchanged checks and no automatic remediation.
- [x] Verify completed repeat acceptance: full duration, zero failures, final
  `passed: true` and matching successful service exit.

If short diagnosis fails, preserve the named errors and investigate rather than
launching the six-hour run. Do not silently reset counters, declare premature
success or weaken thresholds. A pending/failed observation is not acceptance.
The long run certifies only its actual observed deployment and scope, not audible
sound, physical fault recovery or the proposed guided setup feature.

## Execution record

Focused agent/collector/web-client/agent-deployment run:
**91 tests passed** using
`PYTHONPATH=tests .venv/bin/python -m unittest test_playbackagent test_playbackstatus test_playback_status test_playback_agent_deployment`.
New scheduler tests cover the exact 20-second deadline, delayed successful
publication and genuine old/error probes still becoming stale at 30 seconds.
Collector coverage also checks repeated audio refresh submissions retain the
same blocked jobs without overlap.

The pre-fix diagnosis was stopped explicitly before update; its retained final
sample counter was **357 samples, seven failures, 1,869.540 seconds**. It was
diagnostic evidence, not a completed endurance pass; original records remain
unchanged. Every failed readiness/route report retained `status: ok`; stale ages
were **30,002-30,305 ms**. Across the seven failed samples, readiness freshness
failed 5/6/6 times for centerlfe/front/rear and route freshness failed 2/2/3 times,
respectively. No other named conditions failed.

A narrowly scoped Ansible update retained the old helper, installed only
`lib/playbackagent.py` and restarted only the user agent. All seven tasks
succeeded. New SHA-256:
`09f4c8aaf44897097ff3bed6d9282296d4019ad926ec6c8bf578a68bccd25f07`.
Agent PID changed from 992 to 335358. Verified before/after source/config/token/
unit metadata and container/protected-file identities differed only in that
helper and agent process; MPD/audio/supervisor remained active with unchanged
PIDs. No dependency installation, token copy, proxy update or container change
was performed.

The first post-restart baseline check retained pending route failures from cold
observation startup. This is recorded separately, not silently erased or counted
as a timed acceptance pass. All checks passed during bounded warm-up at
**09:03:40 CET**, before starting the 900-second test.

Post-fix evidence directory:
`~/.local/state/mpdrussoundpi-diagnostic-20261114-fixed/`.
Unit: `russound-diagnostic-20261114-fixed-15min.service`.
The short run started at **09:03:49 CET** and completed at **09:18:49 CET**:

| Measured gate | Verified result |
|---|---|
| Duration | 900.056 seconds |
| Samples / failures | 172 / 0 |
| Final event / outcome | `complete` / `passed: true` |
| Observer exit | `Result=success`, `ExecMainStatus=0` |
| Maximum readiness age | 20,287 ms |
| Maximum route age | 20,210 ms |
| Maximum observed local API request duration | 105 ms |

The increased collection rate did not cause failed checks in this short window.
This is latency/freshness evidence, not a CPU-load benchmark or an endurance
guarantee. Full baseline/TCP/TLS checks also passed; no thresholds were relaxed.
Pre-fix and short diagnostic JSONL records were copied to private controller
session evidence in addition to remaining on the Pi.

Only after verifying the short result and successful exit, the repeat six-hour
run started at **09:19:16 CET**, with expected completion no earlier than
**15:19:16 CET**. Unit: `russound-acceptance-20261114-fixed-6h.service`.
Evidence: `~/.local/state/mpdrussoundpi-acceptance-20261114-fixed/`.
It reuses the verified post-fix baseline instead of accepting another unexplained
identity change. Duration is 21,600 seconds; full checks run approximately once
a minute; automatic mutation/restart is disabled. The first sample passed.
The completed run was verified read-only at approximately **17:54 CET**:

| Measured gate | Verified result |
|---|---|
| Completion | 2026-11-14 15:19:18 CET |
| Duration | 21,602.064 seconds |
| Samples / failures | 350 / 0 |
| Final status and last JSONL event | Both `complete`, `passed: true` |
| Observer exit | `Result=success`, `ExecMainStatus=0` |
| Maximum readiness age | 20,315 ms |
| Maximum route age | 20,234 ms |

**The repeat six-hour zero-failure acceptance gate passed.** The current web
and ser2net containers were also healthy, up approximately 16 hours at the
verification. The observation covered its original identity/service/file,
reported connectivity, audio freshness/readiness/routes and independent TCP/TLS
checks; it does not certify audible output, unobserved between-sample events or
the future setup feature. The original failed run remains a failed record.

Related: [coverage and Pi acceptance](../documentation/test-coverage.md),
[guided setup implementation gate](./general_open_guided-first-time-setup.md).

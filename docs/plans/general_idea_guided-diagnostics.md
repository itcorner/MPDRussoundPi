# Idea: guided diagnostics and recovery guidance

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Help users understand which link in the deployment is failing and what to do
next, using a clear, read-only diagnostic flow available from Status and setup.
Avoid requiring a user to interpret raw service logs for common problems.

## Candidate flow

Show independently evaluated checks with timestamps, provenance and explicit
*healthy*, *failed*, *stale*, *unknown* and *not configured* states:

1. Web application is reachable and configuration is valid.
2. Configured gateway is reachable; separately, the physical Russound
   controller responds. A listening ser2net port is not controller health.
3. Each configured playback agent is reachable and authenticated, with fresh
   observations; show failure independently for each agent.
4. Each configured MPD/audio output's reported playback, route and readiness
   status, without implying audio is audible from route readiness alone.

Supply concise next steps specific to the actual failure, for example checking
the physical controller and serial cable when the gateway is reachable but the
controller is silent. Distinguish missing hardware, unknown facts and failure;
do not claim a precise root cause from a symptom alone. Link to the corresponding
configuration field or deployment overview when appropriate.

Reuse cached observations, effective configuration and shared component
identity from [deployment status](./general_idea_deployment-status-overview.md)
and [inline overrides](./web_idea_effective-config-overrides.md). Provide a
bounded manual refresh only if its effects are clear, and avoid per-browser
duplicated polling. Verify whether each existing check is passive or sends
read-only protocol requests before describing it as non-invasive. Never send
state-changing commands, restart a service or alter playback as a diagnostic.

An unresponsive web process cannot render its own page; document an external
fallback for that case rather than showing a misleading “web healthy” result.
Protect operational details and redact secrets under the overview's disclosure
policy. Actual recovery actions belong to the separately authorized
[restart idea](./general_idea_update-status-and-restarts.md), not diagnostics.

## Decisions and acceptance

- [ ] Define the first supported checks, source and freshness thresholds,
  actionable messages, supported deployments and manual-check permissions.
- [ ] Cover offline web fallback, missing configuration, unreachable gateway,
  silent controller, unavailable or stale agent, broken audio route and
  recovery; test partial failures independently and avoid false healthy states.
- [ ] Verify checks are bounded and non-mutating, shared across tabs, accessible
  on mobile and keyboard, and safe when sensitive details are disallowed.
- [ ] Confirm results against independent observations on Pi and supported
  alternative deployments, recording unavailable hardware as unverified.

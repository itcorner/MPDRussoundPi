# Idea: update status and controlled restarts

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Incorporate update information into the proposed
[deployment status overview](./general_idea_deployment-status-overview.md).
Show whether the Pi base system or deployed Docker images have updates
available, keep results in a backend-managed cache refreshed regularly, and
provide a manual **Check for updates** button. Also offer explicitly authorized
API actions to restart selected services or reboot the Raspberry Pi.

This idea covers **checking**, not installing updates. The separate
[user-triggered image updates idea](./general_idea_user-triggered-image-updates.md)
covers a possible future installation workflow; neither idea authorizes
automatic upgrades, pulls, deployments or reboots.

Use a shared component identity, release-channel policy and availability cache
with the image-installation idea; do not build a second registry checker.
Checking, installing and restarting are separate actions with separate consent.
The [deployment simplification plan](./general_open_simplify-deployment-configuration.md)
defines supported modes and image selection.

## Status and update checks

- Attach update results to the relevant host or deployment component in the
  overview. Show installed identity/version, selected release channel,
  available version or digest, last check, source, check result and age.
  Distinguish *update available*, *up to date*, *checking*, *unknown*,
  *unsupported*, *stale* and *check failed*. Include a safe, actionable error.
- **Pi system:** inspect package update availability for the actual configured
  repositories; report a count and, if approved, relevant package names and
  security updates. Separate package-index freshness from the check itself,
  and show whether reboot is recommended only when a reliable source reports
  it. A package index refresh has network and system-side effects: decide
  separately whether and how to authorize it. A stale local cache cannot prove
  there are no updates.
- **Docker images:** compare the running immutable image identity with the
  registry manifest for the selected channel/tag and target platform. Handle
  mutable tags, digest vs. image-ID differences, missing digest metadata,
  platform manifests, private registries, no stable release, and development
  or intentionally pinned versions. A newer `dev` build is not automatically
  a stable release or an update for a pinned deployment. Do not equate image
  build-time base packages with the Pi host OS.
- Manual checking requests a bounded refresh of the same backend cache as
  scheduled checks; return progress/last result without waiting indefinitely.
  Coalesce concurrent checks across tabs, rate-limit manual requests and use
  timeouts, bounded retries/backoff and sensible scheduling. Preserve the last
  successful result, mark it stale after failure, and never report a failed
  lookup as “up to date.”
- Decide whether results persist across web/host restarts; if they do, store
  only sanitized data with timestamps, provenance and an explicit schema.
  Reloaded cached results start as stale until freshness is established.
  Clients receive cached results rather than independently polling registries
  or the Pi. Browser requests remain same-origin.

## Restart and reboot controls

- Provide a small, explicitly configured list of supported actions and targets
  (for example a particular playback agent, MPD output, web container, serial
  gateway or Pi reboot), each with host, expected impact and dependencies.
  Do not accept arbitrary unit names, container IDs, commands, shell fragments
  or user-supplied host addresses. Multiple hosts/agents require explicit target
  identities; unsupported operations must be absent or clearly unavailable.
- Before submission, show a clear confirmation naming the target, likely
  interruption and recovery expectations. A reboot or restart of the web/proxy
  itself can interrupt the response; use durable operation IDs, idempotency,
  audit records, timeouts and reconnection/status retrieval across restarts.
  Track accepted, in-progress, succeeded, failed and **outcome unknown** states.
  Do not claim success merely because a restart was accepted or a host went
  offline; verify recovery, process identity/uptime and component health where
  possible. A missing controller must not be reported healthy because its
  gateway listens.
- Coordinate with existing MPD playback supervision and
  [watchdog restart limits](./mpd_done_playback-stall-watchdog.md). Preserve
  configured playback intentions and avoid overlapping operations, restart
  loops or escalation. Define dependencies and safe ordering for shared audio
  services; do not assume an individual restart is nondisruptive.
- This is a privileged management capability. The current web UI has no login;
  its LAN restriction, proxy source filter and CSRF controls do **not** provide
  administrator authorization. Before exposing any action, design authenticated
  user identity and authorization, CSRF/replay protection, explicit opt-in,
  audit attribution, rate limits and recovery controls. Default to disabled.
  Do not give the web container unrestricted Docker socket access, arbitrary
  sudo/systemctl, shell execution or reboot privileges. If needed, design an
  isolated least-privilege host-side management service with a fixed allowlist
  and authenticated transport. A service bearer token alone does not establish
  which human authorized an action.
- Never show credentials or raw registry authentication material. Treat
  hostnames, package inventories and operational details according to the
  disclosure decision in the deployment-overview idea.

## Existing boundaries

The [playback deployment guide](../documentation/playback-agent-deployment.md)
describes a native Pi agent behind a restricted TLS proxy and a web application
that may run on another host; user services, system services, containers and
physical hardware have different owners. The browser does not talk directly to
the agent. The existing [agent read API](../documentation/mpd-playback-read-api.md)
is not a general host-management API, and its optional playback commands do not
authorize service restarts or OS updates.

Deployment metadata and image identity sources must be established by the
overview design; an arbitrary container cannot reliably determine its own host
or running registry digest. A check must identify its actual source and target,
and report inaccessible data as unknown. Do not assume Docker Hub or apt are
always reachable; certificate verification must remain enabled.

## Decisions before implementation

1. Who may see detailed package/image update information and who may trigger
   checks, restarts or reboots? What administrator authentication and audit
   identity will be supported?
2. Which hosts, services and container layouts are initially in scope? Which
   restarts are safe and useful, and what are their operational prerequisites?
3. Which package sources, registry channels, tags and architectures count as
   updates? How are pinned images, mutable tags and unavailable stable releases
   represented?
4. How frequently should checks run, what is the minimum manual-check interval,
   how long are results fresh, and may the Pi refresh apt indexes?
5. What trusted component executes each check/action, where is the durable
   operation record kept, and how are self-restart, power loss and ambiguous
   outcomes recovered without duplicate execution?

## Proposed stages and acceptance

- [ ] Agree on disclosure, authorization, targets, update policies and supported
  deployment layouts; model host/image identity and action prerequisites.
- [ ] Define versioned, allowlisted APIs and cached result/operation state
  machines with mixed-version, offline and restart recovery behavior.
- [ ] Implement bounded, read-only update checks and the overview UI; do not
  introduce package or image installation.
- [ ] Design and implement separately gated, least-privilege restart/reboot
  operations only after administrator authentication and target safeguards
  exist; update deployment and operational documentation.
- [ ] Test stale package indexes, network/TLS errors, private registry failures,
  tag/digest/platform mismatches, unsupported layouts, concurrent manual checks
  and backend restarts. Confirm no false “up to date” result or unwanted upgrade.
- [ ] Test unauthenticated/unauthorized, CSRF/replay and arbitrary-target
  rejection; confirm idempotency, rate limits, audit attribution, timeouts and
  recovery after web/proxy/host restart. Validate no overlapping watchdog
  recovery, no lost playback intent and honest failure/unknown reporting.
- [ ] Perform separately approved, controlled Pi acceptance with an explicit
  maintenance window and recovery access. Verify service health after each
  authorized action; do not reboot production as part of routine automated
  tests.

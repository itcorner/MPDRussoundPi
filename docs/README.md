# Documentation index

| Folder | Contents | Maintained? |
|---|---|---|
| [documentation/](./documentation/) | How the system works and how to run it: guides, API contracts, architecture, references | Yes, kept current |
| [plans/](./plans/) | Ideas, implementation plans and backlogs, from proposal to done | File name and status line follow the work |

When a plan is implemented, keep it in `plans/`, rename it to `done` and update
its status line. Move lasting facts (behavior, contracts, procedures) into `documentation/`, and
record deployment acceptance in the plan it belongs to.

## Documentation

**Guides**

- [Docker image preparation and release](./documentation/docker-image-release.md): build-only artifacts, image boundaries and publication prerequisites.
- [Development and release workflow](./documentation/development-workflow.md): branch roles, feature PRs, release promotion and hotfix back-merges.
- [Playback deployment](./documentation/playback-agent-deployment.md): install or update the Pi playback stack and connect it to the web dashboard.
- [Headless MPD deployment and recovery](./documentation/mpd-headless-deployment.md), including [audio graph and hardware notes](./documentation/mpd-headless-deployment.md#audio-graph-and-hardware-notes).
- [Playback endurance and USB reconnect recorder](./documentation/mpd-playback-endurance-test.md)

**Architecture and API contracts**

- [Software architecture](./documentation/software-architecture.md): how the web application, serial gateway, playback agent, supervisor and MPD work together; runtime flows, state ownership and concurrency rules.
- [MPD integration with the Russound web interface](./documentation/mpd-web-integration-architecture.md)
- [Playback read API v1](./documentation/mpd-playback-read-api.md)
- [Manual playback operations API](./documentation/mpd-playback-operations-api.md)
- [Python MPD backend API](./documentation/mpd-backend-api.md)
- The web HTTP API is documented in [web/API.md](../web/API.md).

**Reference**

- [Russound RNET protocol knowledge base](./documentation/rnet-protocol-knowledge.md)
- [Test coverage review](./documentation/test-coverage.md)
- [Module pages](./documentation/modules/): one page per Python module in `web/` and `tool/`.
- [Screenshots](./documentation/screenshots/) used by the top-level README.

## Plans

File names follow `<subsystem>_<status>_<topic>.md`. Underscores separate the
three fields, hyphens join words inside the topic.

| Field | Values |
|---|---|
| Subsystem | `web`: web server and browser UI; `mpd`: MPD playback stack, agent and config on the Pi; `usb`: USB audio hardware and recovery; `general`: spans several subsystems |
| Status | `idea`: proposal without a decision; `new`: planned, not started; `open`: in progress or items remain; `done`: implemented, only follow-ups remain |

When the status changes, rename the file with `git mv` and update the links to it.

| Plan | Status |
|---|---|
| [MPD agent containerization](./plans/mpd_new_agent-containerization.md) | Planned; design required before adding an agent image |
| [CI/CD pipeline and Docker image release path](./plans/general_open_cicd-docker-release.md) | Stages 1–3 implemented; Stage 4 build-only simulations passed for web/ser2net; registry prerequisites remain; no image publication authorized yet |
| [MPD web integration](./plans/general_done_mpd-web-integration.md) | Manual integration complete with accepted limitations; holds the [deployment acceptance](./plans/general_done_mpd-web-integration.md#6-deployment-acceptance) record and Pi platform follow-ups |
| [Hardware-driven playback automation](./plans/general_new_hardware-playback-automation.md) | Lower priority; former S10 and automation-specific acceptance, not implemented |
| [Live stream level meter](./plans/general_idea_live-level-meter.md) | Idea, not scheduled |
| [Deployment and runtime configuration overview](./plans/general_idea_deployment-status-overview.md) | Idea, not scheduled; scope and disclosure decisions needed |
| [Effective configuration and environment overrides](./plans/web_idea_effective-config-overrides.md) | Idea, not scheduled; precedence and save semantics need design |
| [Update status and controlled restarts](./plans/general_idea_update-status-and-restarts.md) | Idea, not scheduled; authentication and privileged-action design required |
| [Simplify deployment and configuration](./plans/general_open_simplify-deployment-configuration.md) | Open; initial inventory started, deployment contract and staged implementation pending |
| [Guided first-time setup](./plans/general_idea_guided-first-time-setup.md) | Idea, not scheduled; setup and example-data migration need design |
| [Guided diagnostics](./plans/general_idea_guided-diagnostics.md) | Idea, not scheduled; reuse deployment health observations |
| [Safe configuration change handling](./plans/web_idea_safe-configuration-changes.md) | Idea, not scheduled; concurrency and save recovery need design |
| [User-triggered Docker image updates](./plans/general_idea_user-triggered-image-updates.md) | Idea, depends on versioned deployment images; not scheduled |
| [Playback stability and management follow-up](./plans/mpd_open_playback-stability-follow-up.md) | Open; USB and reachability observation, watchdog visibility and deferred recovery ideas |
| [MPD playback stall watchdog and USB audio stability](./plans/mpd_done_playback-stall-watchdog.md) | Watchdog implemented and live-validated 2026-11-02; remaining topics moved to follow-up |
| [Config split](./plans/mpd_done_config-split.md) | Rolled out 2026-10-17 |
| [Playback egress hardening](./plans/mpd_done_playback-egress-hardening.md) | Deployed 2026-10-17 |
| [USB audio recovery](./plans/usb_done_audio-recovery.md) | Deployed 2026-10-16 |
| [Web UI live-update rerender fix](./plans/web_done_live-update-rerender.md) | Implemented 2026-10-24 |
| [API payload cleanup](./plans/web_done_api-payload-cleanup.md) | Implemented and deployed 2026-11-02; deprecated `/api/state` removed the same day |
| [Long-uptime reliability backlog](./plans/web_open_reliability-backlog.md) | 7 of 12 items done |

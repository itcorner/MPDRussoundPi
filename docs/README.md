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
| [CI/CD pipeline and Docker image release path](./plans/general_open_cicd-docker-release.md) | Stages 1–2 accepted; Stage 3 branch/tag rules active, protected PR acceptance in progress; no image publication authorized yet |
| [MPD web integration](./plans/general_open_mpd-web-integration.md) | S01–S09 implemented; S10 automation and S11 acceptance open. Holds the [deployment acceptance](./plans/general_open_mpd-web-integration.md#6-deployment-acceptance) record and Pi platform follow-ups |
| [Live stream level meter](./plans/general_idea_live-level-meter.md) | Idea, not scheduled |
| [Config split](./plans/mpd_done_config-split.md) | Rolled out 2026-10-17 |
| [Playback egress hardening](./plans/mpd_done_playback-egress-hardening.md) | Deployed 2026-10-17 |
| [USB audio recovery](./plans/usb_done_audio-recovery.md) | Deployed 2026-10-16 |
| [Web UI live-update rerender fix](./plans/web_done_live-update-rerender.md) | Implemented 2026-10-24 |
| [API payload cleanup](./plans/web_new_api-payload-cleanup.md) | Planned, partly prepared by the live-update fix |
| [Long-uptime reliability backlog](./plans/web_open_reliability-backlog.md) | 7 of 12 items done |

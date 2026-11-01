# MPD agent containerization

Status: new; design and implementation not started.
Created: 2026-11-01.

## Goal

Evaluate a future `itcornernet/mpdrussoundpi-mpdagent` image. The current
[playback deployment](../documentation/playback-agent-deployment.md) is native:
MPD, PipeWire, supervisor, agent, Caddy and egress enforcement run on the Pi.
There is no agent Dockerfile today. An agent-only image is not a containerized
replacement for the complete audio stack.

## Decisions required before implementation

- Agent-only versus complete playback-stack container: define exactly what
  stays on the host and what moves into the container.
- Reachability of host-loopback MPD instances; host networking changes isolation.
- PipeWire/PulseAudio sockets, audio-user identity, mixer access, device ownership
  and USB reconnect handling.
- Shared configuration, playback state, operation SQLite database and lock paths:
  all writers must retain existing concurrency and atomic-write invariants.
- Supervisor placement and recovery ownership; avoid duplicate supervisors.
- Preservation of host cgroup-v2/nftables public-only stream enforcement.
- TLS proxy, agent tokens and secrets mounts without exposing unauthenticated
  control endpoints.
- Architecture support, hardware acceptance, deployment migration and rollback.

## Acceptance

Test status and durable commands, station/favorite selection, gain persistence,
route recovery, USB disconnect/reconnect, restart ambiguity and egress policy on
real Pi hardware. Do not assume successful image health certifies audio.

This is a separate prerequisite for adding an agent image to the
[CI/CD release path](./general_open_cicd-docker-release.md). No native service
changes or image publication are authorized by this plan.

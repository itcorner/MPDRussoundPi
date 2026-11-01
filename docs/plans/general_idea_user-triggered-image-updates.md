# User-triggered Docker image updates

Status: idea, not scheduled or authorized for implementation.
Recorded: 2026-10-31.

Once the required deployment components are available as versioned Docker
images, offer users an integrated update feature rather than requiring manual
image pulls and container recreation. This is a follow-up to the
[CI/CD and Docker release path](./general_open_cicd-docker-release.md),
not an additional approved stage or a change to its initial image scope.
The native Pi playback agent is not covered until its deployment model is
explicitly addressed.

- Show installed versions, available stable releases and release notes.
- Let an authorized administrator select a version and explicitly confirm
  the update, including expected downtime. No unattended updates by default.
- Pull the approved images for the host architecture and recreate only the
  relevant services, preserving persistent configuration and playback state.
- Use exact versions/digests and compatible component combinations, rather
  than blindly updating mutable tags or unrelated images.
- Check health after updating, report progress and failures explicitly, and
  provide rollback to the previous images with documented data-compatibility
  limits and backup requirements.
- Decide the execution mechanism later: prefer a narrowly scoped host-side
  updater over exposing unrestricted Docker socket access to the web app.

Before implementation, define supported deployment layouts, component scope,
authentication, update/rollback compatibility and acceptance tests for both
successful and failed updates.

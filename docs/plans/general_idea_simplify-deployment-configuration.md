# Idea: simplify deployment and configuration

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Reduce the number of ways to configure and deploy the solution, make the
remaining choices explicit, and provide one consistent, documented precedence
policy across components. Simplify the Ansible entry points so an operator can
understand which parts run where and which images or source revisions are used.
Preserve existing configuration and state through any migration.

## Requested deployment model

Provide two clear Docker Compose deployment files (final names to be decided):

| Mode | Default image source | Logging | Optional source build |
|---|---|---|---|
| Development | Docker Hub development images | Advanced diagnostic logging, with bounded retention and redaction | Yes, explicit opt-in to build locally from source |
| Production | Docker Hub stable release images | Minimal operational logging; warnings/errors and necessary health/security diagnostics remain visible | No implicit build |

Split Ansible deployment into understandable subparts or entry points for
**Docker release**, **Docker dev**, **Docker local build**, and **bare metal**.
Shared, narrowly scoped tasks may be reused rather than copied. A local build
is a development option, not an accidental fallback when a registry pull fails.
The images should be versioned and compatible across components, with image
source, revision/digest, architecture and pull policy explicit.

Stable Docker releases are not yet available. Production must fail clearly
until an approved compatible stable release exists; do not silently substitute
development images or build from source. Development defaults to published dev
images. Precisely define how local-build mode is selected, which machine builds,
and which source revision is used; “local” must not ambiguously mean both the
Ansible controller and Pi.

Define the supported component placement explicitly: web, serial gateway,
playback agent, proxy and MPD/audio services. The current agent/audio stack is
native on the Pi; “Docker deployment” must not imply all components are
containerized. Decide whether an external serial gateway and a separate web
host remain supported or receive a deliberate migration/deprecation path.
Preserve a hardware-free dummy backend for development/testing without
mistaking it for a physical gateway.

## Current complexity and important constraints

- [`docker-compose.yml`](../../docker-compose.yml) currently declares both
  local `build` and local `image` names. The Docker Ansible playbook defaults
  to building; an optional registry override and flags select published
  images. Native web deployment is a separate playbook. These mechanisms should
  be inventoried before deciding what to remove.
- Configuration is split among persisted application config/state, environment,
  CLI flags, Compose interpolation, Ansible variables, systemd units and
  agent/audio configuration. Some are deployment inputs or secret references,
  not interchangeable application settings. The
  [effective-configuration idea](./web_idea_effective-config-overrides.md)
  documents verified examples and depends on accurate source/precedence data.
- `RUSSOUND_DEBUG` currently changes more than logging: it selects Flask's
  development server instead of Waitress. Advanced logging must be decoupled
  from server selection, and development deployments must not inadvertently
  enable Flask's debugger on a network-facing service. Protocol-audit logging
  is separately configured; define its privacy and retention independently.
- Production “minimal logging” must not suppress actionable errors, security
  events or health failures. Define concrete log levels, destinations, size
  limits, rotation and retention for **each** component and for both modes.
  Avoid credentials, secret paths and raw stream URLs in logs.
- Pi deployments currently include existing config/state, private tokens,
  playback intentions, TLS trust, USB device mappings and audio services.
  Switching layouts can interrupt playback or hardware control; no design may
  overwrite those assets or stop unrelated services implicitly.

## Configuration and precedence

Inventory each actual setting across web, gateway, agent, audio/MPD, proxy and
Ansible/Compose. For each, record owner, type, default, inputs, current and
target precedence, validation, secret handling, restart requirements and
whether it is user-editable. Distinguish:

1. Application configuration and durable runtime state.
2. Deployment selection and host-specific wiring, including Compose
   interpolation versus environment actually delivered to a process.
3. Explicit CLI options, environment, config file and defaults for the **same**
   setting; a candidate common rule is explicit CLI > environment > config
   file > default, only where all those inputs actually exist.
4. Ansible variable precedence and generated files as a separate deployment
   layer; do not present Ansible precedence as process runtime precedence.
5. Secrets and certificates, which remain separate protected files and must
   not be copied into public config, logs or API payloads.

Adopt one explicit priority policy for comparable settings or document a
necessary exception with a reason. Validate absent, empty, malformed and
conflicting values consistently, and surface errors rather than silently
claiming a different effective value. Decide compatibility for existing
variables and CLI invocations before changing behavior. Expose provenance to
the configuration and deployment-status ideas without leaking secrets.
Decide the target policy first, then share one effective-value/source contract
with [inline overrides](./web_idea_effective-config-overrides.md) and the
[deployment overview](./general_idea_deployment-status-overview.md); do not
let those interfaces independently define precedence.

## Ansible and deployment design

- Separate entry points by operator intent, with a small shared core for
  packages, users, directories, configuration, device permissions, network
  setup and health verification. Avoid copy-pasted deployment logic and
  hidden cross-mode side effects. Keep inventory and host-specific settings
  easy to locate and document each entry point's prerequisites and impact.
- Published image modes use explicit approved Hub references and never build;
  build mode uses an explicit checkout/revision and never silently pulls a
  substitute application image. Define when normal base-image pulls occur.
  Explain digest pinning, dev updates, stable promotion and offline deployments.
- Validate inputs, architecture, disk space, serial hardware and required
  secrets **before** stopping a working service where feasible. Preserve dirty
  checkouts and avoid replacing native services unless the selected migration
  explicitly requests it. Make idempotence and failure/rollback boundaries
  visible; distinguish web HTTP readiness, gateway reachability, actual
  controller replies and playback-agent/audio readiness.
- Specify how mode changes preserve or migrate bind mounts, native paths,
  secrets, configuration, state, images and network access. Provide backup,
  verification and rollback procedures, including an explicit no-controller
  acceptance mode that reports incomplete hardware verification honestly.
- Update README, deployment guides, examples and compose commands together.
  Provide a concise decision table and exact commands for each mode; ensure
  existing links and scripts point at the authoritative paths.

## Decisions and staged work

- [ ] Audit existing settings, supported layouts, live deployment constraints
  and consumers; map old inputs/paths to the proposed supported modes.
- [ ] Agree on the precedence policy, logging profiles, supported mixed
  deployments, image pinning and local-build location; define deprecations
  and the stable-release prerequisite.
- [ ] Design the two Compose files and the four Ansible entry points with shared
  tasks, explicit image/source selection and migration/rollback procedures.
- [ ] Implement in independently verifiable stages; preserve compatibility or
  provide a documented migration with clear errors for removed behavior.
- [ ] Update user documentation, examples, API/source reporting and related
  ideas; validate the real deployment with explicit approval before cutover.

## Acceptance and verification

- Render Compose configurations for dev Hub, dev local build and production;
  assert expected images, logging, mounts, secrets, networking and no
  unintended builds. Missing production release must fail, not use dev.
- Test each Ansible entry point and repeated runs, shared task behavior,
  missing prerequisites and failure rollback; preserve unrelated services and
  dirty checkouts. Exercise native, Docker, external gateway and dummy modes
  according to the final support decision.
- For every comparable setting, test CLI/environment/config/default conflicts,
  blank and invalid values, source reporting, save/reload and existing
  configuration migration. Verify Ansible-generated values actually reach the
  intended container or service.
- Check development diagnostics and production minimum logging, rotation,
  redaction, server choice, exposed ports and resource limits. Ensure the dev
  debugger is not accidentally network-exposed.
- On the Pi and a representative non-Pi dev setup, verify web-to-gateway and
  web-to-agent routing, TLS, physical controller responses when available,
  MPD/audio readiness, preservation of state and a deliberate rollback.
  Report absent hardware or unavailable stable images as unverified, not passed.

Related: [deployment status](./general_idea_deployment-status-overview.md),
[update status and restarts](./general_idea_update-status-and-restarts.md),
[image release pipeline](./general_open_cicd-docker-release.md), and
[agent containerization](./mpd_new_agent-containerization.md).

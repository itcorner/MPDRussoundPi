# Idea: guided first-time setup

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Guide an operator through a working initial configuration without requiring
them to understand every deployment detail or use SSH for ordinary setup.
Make the distinction between example values, user choices and verified
hardware explicit. Provide a resumable, accessible flow rather than silently
assuming the example matches the installation.

## Existing behavior and scope

The [Docker deployment playbook](../../ansible/playbooks/deploy-russound-docker.yml)
seeds [`config_example.json`](../../web/config_example.json) when no config
exists. That example includes named rooms, controllers and a playback mapping;
these are illustrative, not discovered hardware. The
[configuration editor](../../web/static/config.html) already supports initial
setup, connection, controllers, zones and input mappings.

Propose a guided layer over those existing capabilities, not a second source
of configuration. It should work with a new deployment, and offer an explicit
opt-in revisit for an existing configuration without resetting it.

## Candidate flow

1. Explain the chosen deployment and whether a gateway, controller and
   playback agent are configured; identify which facts are unknown.
2. Select/confirm the effective connection, showing deployment-managed
   read-only fields and their source. Offer a read-only connectivity check and
   distinguish a TCP listener from a responding controller.
3. Configure controllers, zones, labels and enabled inputs. Treat hardware
   discovery only as a proposal if actually supported; never assert example
   names or zone counts are verified.
4. Optionally connect configured playback outputs to inputs, keeping stable
   agent/output identities. Allow skip and explicit offline/unavailable states.
5. Review the intended changes and impact, save explicitly, and show separate
   configured/connected/verified results. Link to
   [guided diagnostics](./general_idea_guided-diagnostics.md) for failures.

Preserve existing data and secrets; no automatic pairing, device probing,
audio playback, firmware changes, installation, service restarts or hardware
commands without an approved design and explicit consent. Reuse shared
effective-value metadata from
[inline overrides](./web_idea_effective-config-overrides.md) and safe-save
semantics from [configuration change handling](./web_idea_safe-configuration-changes.md).
The setup UI must not expose secret-file paths or credential contents.

## Decisions and acceptance

- [ ] Decide whether new installations stop seeding examples, mark them as
  examples, or preserve them with explicit confirmation; provide a migration
  for existing configs and do not infer first-run from names alone.
- [ ] Decide which layouts and offline cases are supported; distinguish a
  read-only test from any operation that talks to physical hardware and bound
  its load/timeouts.
- [ ] Define draft persistence, resume/cancel behavior, accessibility, mobile
  layout and server-side validation; never silently discard existing edits.
- [ ] Test missing/invalid config, sample config, existing config, env overrides,
  silent controller, absent gateway/agent, multiple agents, retries and later
  recovery. Confirm no unintended data overwrite or playback change.
- [ ] Validate with representative Docker and native deployments; report
  unavailable hardware as unverified, not successful.

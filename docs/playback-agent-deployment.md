# Raspberry Pi playback agent deployment

Status: agent-only deployment executed on 2026-10-15 with explicit approval
using a separate observation-test config. This is partial S06 acceptance,
not the completed production rollout. S04 production ID migration,
D06 secure transport and S05 web integration remain outstanding.

## Scope and prerequisites

Use [deploy-playback-agent.yml](../ansible/playbooks/deploy-playback-agent.yml),
not the full [music Pi setup](../ansible/playbooks/setup-musicpi.yml). The latter
regenerates audio/MPD and restarts playback; it is not an additive agent rollout.
The dedicated play copies the current controller checkout's agent entry point,
dependency manifest and Python helpers into a separate directory. It does not
pull/reset the Pi's repository or modify its shared playback virtual environment.
Uncommitted local agent sources are therefore included; review them before use.

Prerequisites for a later approved run:

- Ansible on the controller, the existing inventory/SSH account and sudo
  permission for `loginctl enable-linger` if lingering is not already enabled.
  Run as the existing non-root audio user, not with global `--become`.
- Existing Pi Python 3 with `venv`/pip support and `pactl`; existing MPD/audio
  services. The agent-only play deliberately performs no apt upgrades or audio
  package installation.
- A live user manager with the audio user's owned mode-0700 `/run/user/<uid>`
  and D-Bus socket. The play refuses to bootstrap it: starting a missing user
  manager could implicitly activate existing enabled playback services.
- An operational config whose stream entries already have the approved S04
  persisted IDs/labels. The play validates with the read-only collector but
  does not migrate, seed or overwrite configuration. Today's unmigrated Pi
  config is expected to fail this prerequisite.
- A controller-side private token file outside the repository, preferably in
  a private directory with mode 0600. Use a cryptographically random token
  of at least 32 non-whitespace ASCII characters; one trailing newline is fine.
  Supply only its path. Do not put the token in CLI arguments, inventory,
  source files or browser assets.
- Explicit deployment approval, normally a maintenance window after endurance testing. Capture config/
  intention checksums, daemon PIDs, queues, routes and gain before activation.
  These scripts do not replace that approval or the S06 acceptance procedure.

## Install and activate later

The following are operator examples, **not commands executed during preparation**.
From the repository root:

```sh
# Static controller-only validation; no target tasks.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml --syntax-check

# Later: install files/dependencies/unit without starting or enabling the agent.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  -e playback_agent_token_source=/private/path/playback-agent-token

# Later, only after migration and deployment approval: activate the agent.
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/deploy-playback-agent.yml \
  -e playback_agent_token_source=/private/path/playback-agent-token \
  -e playback_agent_start=true
```

Default locations:

| Resource | Location relative to the audio user's home |
| --- | --- |
| Agent entry point/helpers | `.local/share/mpdrussoundpi-agent/` |
| Isolated agent venv | `.local/share/mpdrussoundpi-agent/venv/` |
| Private token | `.config/mpdrussoundpi-agent/token` |
| User unit | `.config/systemd/user/mpdrussoundpi-playback-agent.service` |
| Existing config, read only | `.config/mpdrussoundpi/config.json` |

Override `playback_agent_id`, `playback_agent_port`, `playback_agent_config`
or installation path variables as necessary. Paths must be absolute and
systemd-safe (no spaces, traversal or `%` specifiers). The fixed listener is
**127.0.0.1:8081** by default. There is no remote-binding option in this play.
Do not expose it via an unprotected HTTP proxy: D06 must separately choose and
provision secure transport. No Docker registry, TLS or firewall is configured here.

The installation validates config without MPD/audio commands, preserves token
privacy in task output and enables lingering without restarting the user manager.
It reloads user unit definitions only. On explicit activation it enables/starts
only the agent, restarting it if its sources, unit, token or dependencies changed.
It checks authenticated health/identity and inventory, plus unauthenticated
401 rejection on the Pi's loopback interface.
Inventory HTTP 200 can contain failed or pending probes; these checks do not
prove successful MPD reads, fresh metadata or correct audio routes.

Install-only reruns do not stop/restart an already running agent, but can update
its files/token/unit. Use explicit activation to load those changes. Token
rotation is an explicit operator replacement of the private source file followed
by activation; tokens are never silently regenerated.
Check mode is incomplete for a new install: dependency installation, runtime
config validation, daemon reload and HTTP checks are skipped. Do not treat
`--check` as proof of target readiness; use syntax/static tests and subsequent
approved target acceptance.

The [user service template](../ansible/templates/playback-agent.service.j2)
has no `Wants`/`Requires` dependency on MPD/audio services. It supplies the
existing D-Bus/Pulse environment, a private umask and bounded on-failure
restarts. It creates no playback ledger/state files; future write support needs
its own reviewed writable-state provisioning. SIGTERM shutdown is allowed
90 seconds to finish bounded reads before systemd terminates its control group.
Journal output uses the agent's sanitized observation/status logging.

## Rollback later

```sh
ansible-playbook -i ansible/inventory.yaml \
  ansible/playbooks/remove-playback-agent.yml
```

[remove-playback-agent.yml](../ansible/playbooks/remove-playback-agent.yml)
stops/disables only the agent, removes only its unit and reloads user definitions.
It preserves code, venv, private token, lingering, operational config/intentions
and all MPD/audio services. Keeping files allows deliberate reinstallation;
credential deletion/revocation is a separate operator action. It requires the
same existing audio user manager; it is not a general Pi teardown.

## Preparation validation

[Static deployment tests](../tests/test_playback_agent_deployment.py) parse
the playbooks and render the unit, checking opt-in activation, agent-only service
operations, isolated dependencies, private credentials, loopback binding,
non-mutating config validation and limited rollback. They never execute Ansible
tasks or target commands. Ansible `--syntax-check` is controller-only.
Preparation validation passed: six static tests and syntax checks for both
playbooks. No target execution or systemd acceptance is claimed.

## Approved isolated target test (2026-10-15)

The user explicitly approved deploying during the endurance run using a
separate private config copy. The agent-only play completed with no failures
and started `mpdrussoundpi-playback-agent.service` at 01:02:29 CEST. The service
is enabled and remains running. No full Pi setup or rollback play was executed.

- Installed the isolated venv and current committed agent sources; existing
  playback checkout/venv were not updated.
- Copied the current operational config to
  `~/.config/mpdrussoundpi-agent/observation-test-config.json`, adding test
  IDs `front`, `centerlfe`, `rear` and test labels only in that copy.
  This is not the S04 production migration. The copy does not follow edits
  to the original config; use the approved operational config after migration.
- Generated a private random service credential outside the checkout; target
  token/config are mode 0600 under a mode-0700 directory. No credential values
  were logged or committed.
- Actual listener verified as **127.0.0.1:8081**, not a LAN listener. D06 and
  Docker/web integration remain unimplemented; SSH carried the test commands.
- Verified authenticated health/capabilities/inventory, per-output HEAD,
  unknown-output 404, unauthenticated 401 and unsupported POST 405.
  `write_operations` is empty.
- Three cached HTTP reads measured approximately 41.35, 3.19 and 6.44 ms on
  the Pi. These are a small smoke sample, not p95 LAN or metadata-update latency.
- At the successful verification around 02:13 CEST all streams were playing,
  with one queue entry, fresh title metadata, ready audio and correct routes.
  Measured stereo gain units were `[49905, 49905]`, `[65536, 65536]` and
  `[64044, 64044]`, matching -7.1, 0 and -0.6 dB respectively.
- Before/after SHA-256 checks confirmed byte-unchanged production config and
  intentions. PIDs, start times and restart counts were unchanged for all
  three MPDs, supervisor, recorder, PipeWire, PipeWire-Pulse and WirePlumber.
  The recorder remained sampling and its summary timestamp advanced.
- Radio 88.6's pre-existing faults continued: state problem samples rose
  from 9 to 17, route measurement problems from 11 to 17 and suspected-silence
  samples from 22 to 34 across the deployment/verification interval.
  FM4 counters remained 10 state/4 route; OE3 remained unflagged.
  This proves neither uninterrupted playback nor absence of polling/load
  effects. No playback repairs or fault injection were performed.
- One verification SSH connection reset; the read-only check was retried with
  keepalive and completed. No service restart was used to recover the test.
- At 02:14:50 CEST the agent still had zero restarts after approximately
  72 minutes, about 35 MiB resident memory and lifetime average CPU of 0.7%
  (one `ps` sample, not a load benchmark). Recorder and supervisor remained
  active with zero restarts; the recorder was still sampling before its deadline.

Private baseline evidence is retained on the Pi at
`~/.config/mpdrussoundpi-agent/deployment-baseline.json`. The separate config
and service remain in place for read-only tests; full S06 acceptance still
requires migrated production IDs, D06 transport, Docker reachability and final
endurance review.

## USB maintenance update (2026-10-16)

The user approved the coordinated USB recovery maintenance deployment described
in [the recovery record](./usb-audio-recovery-fix.md#approved-maintenance-deployment-2026-10-16).
The separate observation-test config now explicitly includes the same USB
model selector as the operational config; its test IDs/labels and every other
field were preserved. Relevant observer helpers were updated together, and the
agent was intentionally stopped during replacement and resumed at 20:46:03 CEST.

Post-maintenance authenticated inventory is fresh and reports playing streams,
ready downstream audio, correct routes and expected gains for all three outputs.
Unauthenticated inventory remains 401, write capabilities remain empty, and the
listener remains loopback-only. This does not migrate production stream IDs,
implement D06/web transport, or establish physical USB-reconnect acceptance.
The original Oct 15 uninterrupted agent-lifetime sample is historical; the
Oct 16 agent restart is an explicit maintenance event.

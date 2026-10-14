# Raspberry Pi playback agent deployment

Status: deployment files prepared, **not executed**. This is the agent-only
part of S06, not completed target acceptance. S04 production ID migration,
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
- An approved maintenance window after endurance testing. Capture config/
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

No target playbook, installer, pip command, service action or SSH deployment
has been executed for this request. Actual listeners, source metadata,
calibration/intentions preservation and Docker reachability remain S06 acceptance.

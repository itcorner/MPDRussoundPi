# Plan: package layout and single module names

Status: **open** (2026-11-20). Phases 1 and 2 are implemented and deployed to
the Pi at revision `5ed0e04` (see the [rollout record](#pi-rollout-2026-11-20)).
Phase 3 removes the transition bootstrap once the rollout is accepted after an
observation period.

## Progress

| Date | Change | Evidence |
|---|---|---|
| 2026-11-20 | Phase 1 implemented (steps 1-7). | Full test run loads no `lib.*` module and nothing twice (before: four). Full Python/Node suites, the web image smoke test and all 9 scripts run by file path pass. [Guard tests](../../tests/test_import_layout.py) build the egress install and preflight trees from the playbook and run them. |
| 2026-11-20 | Phase 2 implemented in code (see deviations below). | `pyproject.toml`; `mpdrussoundpi.common`; staging playbooks install the candidate editable and run its commands; generated units and the agent template use console commands. Editable install in a fresh venv runs all 10 commands from another directory and leaves the checkout clean; a regular wheel contains the static web files, Lua policies and seed config. CI runs the same check. |

**Pi rollout notes for Phase 1:** agent, supervisor, setup and audit scripts
keep their commands, paths and virtual environments; the new code works from
the existing checkout layout. The egress playbook changes the installed script
path to `{{ playback_egress_install }}/mpdrussoundpi/russound/playbackegress.py`,
rewrites the unit and MPD drop-ins, restarts the filter and MPD as before, and
removes the old flat files afterwards. Exercised on the Pi on 2026-11-20.

## Goal

Every Python module has exactly one import name, on every target and in tests.
Deployments install code as a package instead of hand-picked file lists, and
the web application depends only on an explicit shared subset of the Pi code.

## Starting point (2026-11-20, before Phase 1)

- Pi scripts run as `python …/mpdrussoundpi/russound/<script>.py`. Python puts
  the script directory on `sys.path`, so the shared library imports as a
  top-level package named `lib` (45 `from lib.x` imports in 12 library files,
  29 in 9 entry scripts).
- The web application imports the same files as
  `mpdrussoundpi.russound.lib.x`. That only works because the five files it uses
  happen to import each other relatively.
- In one `unittest discover` process, `publicnetwork`, `servicecredentials`,
  `sourceurl` and `tunein` are loaded **twice**, once under each name. Patching
  one name does not affect code that imported the other, and exception classes
  differ between the copies. Production processes are separate, so they are
  not affected.
- 17 test files temporarily put the scripts directory on `sys.path`; 25 patch
  targets are `lib.…` strings.
- Three hand-maintained lists say which library files a target needs:
  - [Dockerfile](../../Dockerfile) copies 5 named files.
  - The [egress playbook](../../ansible/playbooks/deploy-playback-egress.yml)
    copies `playbackegress.py`, `lib/publicnetwork.py` and `lib/sourceurl.py`.
  - [.dockerignore](../../.dockerignore) names 3 files, but its
    `!mpdrussoundpi/russound/` line re-includes the whole tree, so the list has
    no effect.
- `mpdrussoundpi/` and `mpdrussoundpi/russound/` have no `__init__.py`.

## Phase 1: one module name, no service changes

All Pi services keep their current commands, paths and virtual environments.
The only deployment-visible change is the egress install layout (step 4), which
must mirror the package so its script can use the canonical name.

1. Add `mpdrussoundpi/__init__.py` and `mpdrussoundpi/russound/__init__.py`.
2. Inside `lib/`, replace every `from lib.x import …` with `from .x import …`.
3. Entry scripts import `mpdrussoundpi.russound.lib.x`. When run as a file
   (`__package__` empty) they first add the repository root
   (`Path(__file__).resolve().parents[2]`) to `sys.path`. Imported as
   `mpdrussoundpi.russound.<script>`, they need nothing.
4. The egress playbook installs `mpdrussoundpi/__init__.py`,
   `mpdrussoundpi/russound/{__init__,playbackegress}.py` and
   `mpdrussoundpi/russound/lib/{__init__,publicnetwork,sourceurl}.py` under the
   existing install directory. Its unit and `ExecStartPre` point to the new
   script path.
5. Tests import library modules and entry scripts by their canonical names and
   stop editing `sys.path`; patch targets use canonical names.
6. A guard test fails on any `lib.` absolute import or `"lib.…"` patch target,
   imports every library module and entry script canonically in a clean
   interpreter, and runs one entry script by file path from another directory.
7. The Dockerfile copies the package `__init__.py` files; `.dockerignore` lists
   exactly what the image uses.

**Exit criteria:** no module is loaded under two names in a full test run;
full Python/Node suites and the web image smoke test pass; the egress playbook
tests cover the new layout. The next reviewed Pi deployment carries the change;
nothing is deployed by this phase itself.

## Phase 2: installable package (reviewed Pi rollout)

1. Add `pyproject.toml` with packages `web` and `mpdrussoundpi`, optional
   dependency groups for web, agent and audio, and console entry points
   (`playback-agent`, `mpd-supervisor`, `mpd-setup`, `audio-setup`,
   `audio-ready`, `playback-egress`, `mpdctrl`, `playback-audit`,
   `stream-levels`).
2. Move the modules the web application uses (`tunein`, `publicnetwork`,
   `sourceurl`, `servicecredentials`, `runtimelogging`, `atomicjson`) to
   `mpdrussoundpi.common`. The web application may import `common`, never the
   Pi playback internals; a guard test enforces the direction.
3. Ansible installs the candidate checkout with `pip install <source>` (not
   editable) into each candidate virtual environment, replacing the
   requirements-file install. Each candidate environment then holds a full copy
   of its code, which fits the existing stage/activate model.
4. systemd units, and the units `mpdsetup.py` and `playbackaudit.py` generate,
   run `{{ venv }}/bin/<command>`. Egress gets a root-owned virtual environment
   instead of copied files.
5. The Dockerfile runs `pip install .` and drops the copied file list; the entry
   scripts' `sys.path` bootstrap from Phase 1 is removed.

**Rollback:** activate the previous candidate revision, which keeps its own
virtual environment and units, as today.

**Exit criteria:** Pi services, egress and the web image run from installed
packages with no file lists or `sys.path` edits; deployment and profile tests
cover the new commands; a reviewed Pi rollout and observation pass.

### Phase 2 as implemented: deviations

- **Editable installs on the Pi.** Each candidate already has its own immutable
  checkout next to its venv, so `pip install -e "<source>[agent|audio]"` gives
  the same isolation as a copy. The code keeps running from the checkout, so
  `__file__`-relative resources (Lua policies, the audit's git revision probe)
  need no special handling. `*.egg-info/` and `/build/` are ignored so the
  staging "clean checkout" verification still holds.
- **Docker copies two directories instead of `pip install .`.** The web image
  contains `web/` and `mpdrussoundpi/common/` only; installing the whole
  package would ship the Pi code too. A guard test keeps `web` from importing
  anything but `mpdrussoundpi.common`, so the image needs no file list.
- **Egress keeps root-owned copied files instead of a root venv.** The filter
  uses only the standard library and `common/` (4 files). A venv would run pip
  and a build backend as root during deployment for no dependency benefit. The
  copy list stays in the playbook, and a guard test builds and runs that tree.
- **The `sys.path` bootstrap stays until the rollout is accepted.** Units
  installed before Phase 2 run the scripts by path; keeping the bootstrap lets
  them work until they are regenerated, and keeps rollback simple. Removing it
  is Phase 3.
- Dependency lists stay in the requirements files; `pyproject.toml` reads them
  as optional dependencies (`web`, `agent`, `audio`).

### Phase 2 rollout checklist

Agent and audio activation are not automated in this repository; the reviewed
rollout performs these steps on the Pi:

1. **Audio:** stage a candidate (`setup-musicpi.yml --tags stage`; it now
   verifies `mpd-setup`, `audio-setup`, `mpd-supervisor` and `audio-ready`).
   During activation run `<venv>/bin/mpd-setup --install <config>` from the
   activated environment so the MPD and supervisor units are regenerated with
   `audio-ready` and `mpd-supervisor`; then `mpd-supervisor --once` before
   enabling supervision. Existing units keep working until regenerated.
2. **Agent:** stage a candidate (it now verifies `playback-agent --help`);
   install the unit from the template, whose `ExecStart` is
   `{{ playback_agent_venv }}/bin/playback-agent`; restart and verify health
   through the proxy.
3. **Egress:** run `deploy-playback-egress.yml`. It installs
   `mpdrussoundpi/{russound/playbackegress.py,common/…}`, rewrites the unit and
   MPD drop-ins and removes the earlier layouts.
4. **Audit service**, if installed: rerun `playback-audit --install-service`.
5. **Web:** deploy an image built from this revision (Dockerfile changed).
6. Observe, then start Phase 3.

### Pi rollout (2026-11-20)

Revision `5ed0e045b2aaf713ab5aa5cf94d5d5980dca50c9` (develop after PRs #55-#59).
Baseline before and after: all three outputs playing the same stations, routes
and readiness ok, unchanged gains, all user and system units active.

| Step | Result |
|---|---|
| Backup | User units and drop-ins, MPD configs, egress files and root unit archived on the Pi (`~/MPDRussoundPi-deploy-backups/pre-package-layout-20261120T205227/`) and off-host, checksums verified. The previous checkout `~/MPDRussoundPi` (with uncommitted live changes, all of which are in `develop`) and the previous agent copy were left untouched for rollback. |
| Stage | `setup-musicpi.yml --tags stage` and `deploy-playback-agent.yml --tags stage`: editable installs, clean checkouts, commands start, live config validated (3 outputs). |
| Audio | `mpd-setup --install` first refused: `~/.config/mpd` was group-writable and the units, MPD configs and egress drop-ins predated the ownership markers. After the [documented migration](../documentation/mpd-headless-deployment.md#migrating-units-written-before-ownership-markers) (diff proved generator output; markers added, group write removed) it rewrote only the marker lines and command paths. Supervisor restarted from the candidate after `mpd-supervisor --once`. |
| Egress | `deploy-playback-egress.yml`: package-shaped install, unit and drop-ins rewritten, filter and MPD restarted (both start checks passed), old flat files removed. |
| Agent | Unit `ExecStart` switched to the candidate's `playback-agent` with unchanged arguments; health, capabilities and web playback health ok. |
| Web | Reviewed `deploy --update` with the handover's `compose_spec` and resolved `dev` images at `5ed0e04` (operation `d5edfa95-095c-4ee7-a03c-a988faacff53`): containers, HTTP, backend, playback and apply checks passed. Live: config revision and `428` without it, `no-store`, escaped Status page. |

Two playback interruptions occurred (MPD restart by the egress playbook,
supervisor restart); both recovered on their own. Left unchanged:
`playback_audit.service` (enabled, inactive, left over from the 72-hour run)
still runs the previous checkout. **Rollback:** restore the archives above,
`systemctl --user daemon-reload`, restart the affected units and the egress
service; for the web, the operator's retained backup and recovery actions.

## Phase 3: remove the transition bootstrap

After the Phase 2 rollout is accepted: remove the `if not __package__`
bootstrap from the entry scripts, run scripts only through console commands or
`python -m`, drop the guard test for running a script by file path, and remove
the "installed before the package layout change" notes from the guides.

## Out of scope

Changing service behavior, configuration formats, deployment state handling or
the playback agent API.

Related: [guided first-time setup](./general_open_guided-first-time-setup.md)
(the config/state store in I2 is independent of this layout change).

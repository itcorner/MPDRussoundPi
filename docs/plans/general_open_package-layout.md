# Plan: package layout and single module names

Status: **open** (2026-11-20). Phase 1 is implemented and verified locally; it
reaches the Pi with the next reviewed deployment. Phase 2 changes how the Pi
services are installed and needs a separately reviewed Pi rollout.

## Progress

| Date | Change | Evidence |
|---|---|---|
| 2026-11-20 | Phase 1 implemented (steps 1-7). | Full test run loads no `lib.*` module and nothing twice (before: four). Full Python/Node suites, the web image smoke test and all 9 scripts run by file path pass. [Guard tests](../../tests/test_import_layout.py) build the egress install and preflight trees from the playbook and run them. |

**Pi rollout notes for Phase 1:** agent, supervisor, setup and audit scripts
keep their commands, paths and virtual environments; the new code works from
the existing checkout layout. The egress playbook changes the installed script
path to `{{ playback_egress_install }}/mpdrussoundpi/russound/playbackegress.py`,
rewrites the unit and MPD drop-ins, restarts the filter and MPD as before, and
removes the old flat files afterwards. Not yet exercised on the Pi.

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

## Out of scope

Changing service behavior, configuration formats, deployment state handling or
the playback agent API.

Related: [guided first-time setup](./general_open_guided-first-time-setup.md)
(the config/state store in I2 is independent of this layout change).

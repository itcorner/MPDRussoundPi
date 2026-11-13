"""Controller-side verification of a controlled Ansible invocation.

The receipt is an internal prerequisite for finalization, not proof of target
health or permission to apply. Independent checks and protected inputs remain
the responsibility of the invoking deployment orchestrator.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from tool.deployment_files import checked_directory
from tool.deployment_inputs import read_file
from tool.deployment_state import StateError, identity, operation_id, requirements


MAX_EVENTS = 4 * 1024 * 1024
RUN_TIMEOUT_SECONDS = 600
PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1] / "ansible/callback_plugins"


def validate_events(events: list[dict[str, Any]], *, operation: str, host: str,
                    required: list[str], returncode: int) -> dict[str, str]:
    operation_id(operation)
    identity(host)
    requirements(required)
    if returncode != 0:
        raise StateError("Ansible did not return successfully; finalization is blocked")
    if len(events) < 2:
        raise StateError("Completion stream is absent or incomplete")
    started: set[str] = set()
    passed: set[str] = set()
    base = {"schema_version", "sequence", "operation", "host", "event"}
    for sequence, event in enumerate(events):
        if (type(event) is not dict or type(event.get("schema_version")) is not int
                or event["schema_version"] != 1 or type(event.get("sequence")) is not int
                or event["sequence"] != sequence or event.get("operation") != operation
                or event.get("host") != host):
            raise StateError("Completion stream sequence, schema or operation identity mismatch")
        kind = event.get("event")
        if sequence == 0:
            if kind != "start" or set(event) != base:
                raise StateError("Completion stream has no valid initial record")
            continue
        if sequence == len(events) - 1:
            if (kind != "end" or set(event) != base | {"recording_failed", "hosts", "totals"}
                    or event["recording_failed"] is not False or event["hosts"] != [host]
                    or event["totals"] != dict.fromkeys(("failures", "unreachable", "rescued", "ignored"), 0)):
                raise StateError("Completion terminal record is missing, failed or mismatched")
            if any(type(total) is not int for total in event["totals"].values()):
                raise StateError("Invalid completion statistics")
            continue
        if (set(event) != base | {"actual_host", "task", "unsupported"}
                or event["actual_host"] != host or event["unsupported"] is not False
                or kind not in ("task-start", "passed")):
            raise StateError("Failed, skipped, unsupported or unexpected task outcome blocks finalization")
        task = event["task"]
        if task is None:
            continue
        if not isinstance(task, str) or task not in required:
            raise StateError("Unexpected required task identifier")
        if kind == "task-start":
            if task in started:
                raise StateError("Duplicate required task start")
            started.add(task)
        else:
            if task not in started or task in passed:
                raise StateError("Missing start or duplicate required task terminal outcome")
            passed.add(task)
    if passed != set(required) or started != set(required):
        raise StateError("Missing required task completion")
    return dict.fromkeys(required, "passed")


def read_events(path: Path) -> list[dict[str, Any]]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise StateError("Completion evidence must be a private single-link regular file")
            raw = source.read(MAX_EVENTS + 1)
        if len(raw) > MAX_EVENTS or not raw.endswith(b"\n"):
            raise StateError("Completion evidence is oversized or truncated")
        return [json.loads(line) for line in raw.splitlines()]
    except (OSError, ValueError, UnicodeError) as error:
        if isinstance(error, StateError):
            raise
        raise StateError("Cannot read valid completion evidence; finalization is blocked") from None


def run_checked(*, ansible: Path, playbook: Path, inventory: Path, evidence: Path,
                operation: str, host: str, required: list[str],
                variables: Path | None = None) -> dict[str, str]:
    """Run a bounded play with controlled callbacks; never dispatch finalization.

    Only unconditional, non-looping, synchronous required tasks are supported.
    Required handlers must actually run. No retry of this invocation is made.
    The private evidence directory must already exist and is retained on error.
    Timeout kills the local Ansible process, not necessarily target-side work;
    its outcome is uncertain and must never trigger automatic recovery.
    """
    operation_id(operation)
    identity(host)
    requirements(required)
    directory = evidence.parent
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid() or evidence.exists() or evidence.is_symlink()):
        raise StateError("Completion evidence requires a private directory and a fresh file path")
    env = {
        **{key: value for key, value in os.environ.items()
           if not key.startswith(("ANSIBLE_", "RUSSOUND_COMPLETION_"))},
        "ANSIBLE_CONFIG": str(PLUGIN_DIRECTORY.parent / "deployment-runner.cfg"),
        "ANSIBLE_CALLBACK_PLUGINS": str(PLUGIN_DIRECTORY),
        "ANSIBLE_CALLBACKS_ENABLED": "deployment_completion",
        "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1",
        "ANSIBLE_NOCOLOR": "1",
        "RUSSOUND_COMPLETION_OPERATION": operation,
        "RUSSOUND_COMPLETION_HOST": host,
        "RUSSOUND_COMPLETION_STREAM": str(evidence),
    }
    reader, writer = os.pipe()
    try:
        env["RUSSOUND_COMPLETION_FD"] = str(writer)
        try:
            argv = [str(ansible), "-i", str(inventory), str(playbook)]
            if variables is not None:
                with checked_directory(variables.parent, os.getuid()) as source:
                    read_file(source, variables.name, os.getuid(), (0o400,))
                argv.extend(["--extra-vars", "@" + str(variables)])
            result = subprocess.run(argv,
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    pass_fds=(writer,), timeout=RUN_TIMEOUT_SECONDS)
        finally:
            os.close(writer)
        # A daemon retaining a descriptor must not make validation wait for EOF.
        os.set_blocking(reader, False)
        try:
            acknowledgement = os.read(reader, 2)
        except BlockingIOError:
            acknowledgement = b""
        if acknowledgement != b"1":
            raise StateError("Callback did not acknowledge durable completion evidence; finalization is blocked")
    except subprocess.TimeoutExpired:
        raise StateError("Controlled Ansible timed out; target work may continue; retain reservation and evidence") from None
    except OSError:
        raise StateError("Controlled Ansible execution or completion channel failed; inspect before retrying") from None
    finally:
        os.close(reader)
    return validate_events(read_events(evidence), operation=operation, host=host,
                           required=required, returncode=result.returncode)

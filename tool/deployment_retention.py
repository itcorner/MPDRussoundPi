"""Remove the large files of finished operations that no version still needs.

See docs/plans/general_open_operation-file-retention.md. Kept per deployment: the
committed operation and the verified-complete operation before it; always kept:
the pending or reserved operation, the updater's current operation and its restore
source, and anything the containers' compose labels or project name refer to.
Only directories in the large areas (``backups``, ``assets``, ``incoming``,
``restores`` and the updater's backup copies) are removed, and only when the
evidence names the operation they belong to. The small records (``evidence``,
``runtime``, ``staging``, the updater's JSON and log files) are never touched.
Nothing is removed while the state is blocked, reserved or pending.

Unlike the image cleanup, this reduces what can be restored: an older backup is the
only way back to that version's data. Manual use (dry run by default)::

    sudo .venv/bin/python -m tool.deployment_retention files
    sudo .venv/bin/python -m tool.deployment_retention files --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable

logger = logging.getLogger("mpdrussoundpi.retention")

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AREAS = ("backups", "assets", "incoming", "restores")
# An operation whose apply was attempted, backed up and completed, without a recovery afterwards.
FINISHED = (".apply-attempt.json", ".backup.json", ".completion.jsonl")
RECOVERED = (".recovery-attempt.json", ".recovery-complete.json")

Run = Callable[[list[str]], str]


class RetentionError(RuntimeError):
    """The retention could not decide safely; nothing was removed."""


def _uuid(value: object) -> str | None:
    return value if isinstance(value, str) and UUID.fullmatch(value) else None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ids_in(text: str) -> set[str]:
    """Operation or recovery IDs in compose labels: paths, and project names with the ID's hex form."""
    found = set(UUID.findall(text))
    for compact in re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", text):
        found.add(f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}")
    return found


class Evidence:
    """What the operations' small records say: known operations, recoveries and their order."""

    def __init__(self, evidence: Path, updater_evidence: Path | None) -> None:
        self.files: dict[str, set[str]] = {}
        for root in (evidence, updater_evidence):
            if root is None or not root.is_dir():
                continue
            for name in os.listdir(root):
                operation = _uuid(name[:36])
                if operation and len(name) > 36 and not (root / name).is_dir():
                    self.files.setdefault(operation, set()).add(name[36:])
        self.evidence = evidence
        # Recovery ID -> the operation it recovered (from the reviewed recovery attempt).
        self.recoveries: dict[str, str] = {}
        for operation, suffixes in self.files.items():
            if ".recovery-attempt.json" in suffixes:
                record = _read_json(evidence / (operation + ".recovery-attempt.json"))
                review = record.get("review") if isinstance(record, dict) else None
                recovery = _uuid(review.get("recovery")) if isinstance(review, dict) else None
                if recovery:
                    self.recoveries[recovery] = operation

    def owner(self, name: str) -> str | None:
        """The operation an ID belongs to, or None when the evidence does not know it."""
        if name in self.recoveries:
            return self.recoveries[name]
        return name if name in self.files else None

    def previous(self, deployment: str, committed: str) -> str | None:
        """The newest finished, never-recovered operation of this deployment other than the committed one."""
        best: tuple[float, str] | None = None
        for operation, suffixes in self.files.items():
            if operation == committed or not all(s in suffixes for s in FINISHED) or any(s in suffixes for s in RECOVERED):
                continue
            intent = _read_json(self.evidence / (operation + ".intent.json"))
            if not isinstance(intent, dict) or intent.get("deployment") != deployment:
                continue
            try:
                moment = (self.evidence / (operation + ".apply-attempt.json")).stat().st_mtime
            except OSError:
                continue
            if best is None or moment > best[0]:
                best = (moment, operation)
        return best[1] if best else None


def keep_set(status: dict[str, Any], evidence: Evidence, updater_state: dict[str, Any] | None,
             in_use: set[str]) -> dict[str, str]:
    """Operation -> reason; raises when it is not safe to decide."""
    if status.get("blocked") or status.get("reservation") is not None:
        raise RetentionError("The deployment state is blocked or reserved; nothing is removed.")
    deployments = status.get("deployments")
    if not isinstance(deployments, dict) or not deployments:
        raise RetentionError("No deployment is recorded; nothing is removed.")
    keep: dict[str, str] = {}
    for name, record in deployments.items():
        if not isinstance(record, dict) or record.get("pending") is not None:
            raise RetentionError(f"Deployment {name} has a pending operation; nothing is removed.")
        committed = record.get("committed")
        selection = committed.get("selection") if isinstance(committed, dict) else None
        current = _uuid(selection.get("operation")) if isinstance(selection, dict) else None
        if current is None:
            raise RetentionError(f"Deployment {name} has no committed operation; nothing is removed.")
        keep[current] = f"committed operation of {name}"
        backup = _uuid(selection.get("backup"))
        if backup:
            keep.setdefault(backup, f"backup of the committed version of {name}")
        previous = evidence.previous(name, current)
        if previous:
            keep.setdefault(previous, f"previous operation of {name}")
    if isinstance(updater_state, dict):
        restore = updater_state.get("restore")
        for value, reason in ((updater_state.get("operation"), "the updater's current operation"),
                              (restore.get("operation") if isinstance(restore, dict) else None,
                               "the updater's restore source")):
            operation = _uuid(value)
            if operation:
                keep.setdefault(evidence.owner(operation) or operation, reason)
    for value in sorted(in_use):
        keep.setdefault(evidence.owner(value) or value, "referred to by the running containers")
    return keep


def _size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        for name in files + directories:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                pass
    return total


def _entry_owner(area: str, path: Path, evidence: Evidence) -> str | None:
    owner = evidence.owner(path.name)
    if owner is None and area == "incoming":
        # A backup received for a recovery: a random parent holding exactly one operation directory.
        try:
            children = os.listdir(path)
        except OSError:
            return None
        if len(children) == 1 and (path / children[0]).is_dir():
            owner = evidence.owner(children[0])
    return owner


def plan(storage: dict[str, Path], updater_evidence: Path | None, evidence: Evidence,
         keep: dict[str, str]) -> dict[str, Any]:
    """Pure decision over the large areas: what to remove, what stays and what is unidentified."""
    remove, kept, unidentified = [], [], []
    areas = [(area, storage[area]) for area in AREAS]
    if updater_evidence is not None and updater_evidence.is_dir():
        areas.append(("updater", updater_evidence))
    for area, root in areas:
        for name in sorted(os.listdir(root)):
            path = root / name
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                if area != "updater":  # the updater's records are files and stay
                    unidentified.append(str(path))
                continue
            owner = _entry_owner(area, path, evidence) if _uuid(name) else None
            if owner is None:
                unidentified.append(str(path))
            elif owner in keep:
                kept.append({"path": str(path), "operation": owner, "reason": keep[owner]})
            else:
                remove.append({"path": str(path), "area": area, "operation": owner, "bytes": _size(path)})
    return {"remove": remove, "kept": kept, "unidentified": unidentified,
            "keep": [{"operation": key, "reason": value} for key, value in sorted(keep.items(), key=lambda i: i[1])]}


def _in_use(run: Run | None) -> set[str]:
    if run is None:
        return set()
    ids = run(["container", "ls", "--all", "--quiet", "--no-trunc"]).split()
    if not ids:
        return set()
    labels = run(["container", "inspect", "--format",
                  '{{index .Config.Labels "com.docker.compose.project"}} '
                  '{{index .Config.Labels "com.docker.compose.project.config_files"}} '
                  '{{index .Config.Labels "com.docker.compose.project.working_dir"}}', *ids])
    return _ids_in(labels)


def retention(storage: dict[str, Path], status: dict[str, Any], *, apply: bool, run: Run | None,
              updater_root: Path | None = None) -> dict[str, Any]:
    """Decide and, with ``apply``, remove. Raises RetentionError before removing anything when unsafe."""
    updater_evidence = updater_root / "evidence" if updater_root is not None else None
    updater_state = _read_json(updater_root / "state.json") if updater_root is not None else None
    if updater_root is not None and (updater_root / "state.json").exists() and not isinstance(updater_state, dict):
        raise RetentionError("The updater record is unreadable; nothing is removed.")
    evidence = Evidence(storage["evidence"], updater_evidence)
    try:
        in_use = _in_use(run)
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        raise RetentionError(f"Cannot read which files the containers use ({error}); nothing is removed.") from None
    decided = plan(storage, updater_evidence, evidence, keep_set(status, evidence, updater_state, in_use))
    report: dict[str, Any] = {**decided, "applied": apply, "removed": 0, "skipped": [],
                              "freed_bytes": 0, "would_free_bytes": sum(item["bytes"] for item in decided["remove"])}
    if not apply:
        return report
    for item in decided["remove"]:
        path = Path(item["path"])
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise OSError("no longer a directory")
            shutil.rmtree(path)
            report["removed"] += 1
            report["freed_bytes"] += item["bytes"]
        except OSError as error:
            report["skipped"].append({"path": item["path"], "error": str(error)[:200]})
    return report


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """The small result the operator and the updater pass on."""
    return {"removed": report.get("removed", 0), "skipped": len(report.get("skipped", [])),
            "freed_bytes": report.get("freed_bytes", 0), "error": report.get("error")}


def _megabytes(value: int) -> str:
    return f"{value / 1000 ** 2:.0f} MB"


def render(report: dict[str, Any]) -> str:
    applied = report["applied"]
    lines = [("Removed" if applied else "Would remove")
             + f" {report['removed'] if applied else len(report['remove'])} director(ies), "
             + _megabytes(report["freed_bytes"] if applied else report["would_free_bytes"])]
    lines += [f"  - {item['path']} ({item['operation'][:8]}, {_megabytes(item['bytes'])})" for item in report["remove"]]
    lines.append("Kept operations:")
    lines += [f"  - {item['operation'][:8]}: {item['reason']}" for item in report["keep"]]
    if report["unidentified"]:
        lines.append("Not identified by the evidence (never removed automatically; decide by hand):")
        lines += [f"  - {path}" for path in report["unidentified"]]
    for item in report["skipped"]:
        lines.append(f"Skipped {item['path']}: {item['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from tool.deployment_channel import ChannelError
    from tool.deployment_cleanup import CleanupError, docker_runner
    from tool.deployment_compose import ComposeError, local_engine, paths
    from tool.deployment_operator import STORAGE, operator_lock
    from tool.deployment_state import STATE_ROOT, StateError, StateStore
    from tool.deployment_updater import ROOT as UPDATER_ROOT

    parser = argparse.ArgumentParser(description="Remove the large files of finished operations no version needs.")
    sub = parser.add_subparsers(dest="command", required=True)
    files = sub.add_parser("files", help="dry run by default")
    files.add_argument("--apply", action="store_true", help="remove the listed directories")
    files.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run as root from the trusted tooling directory")
    storage = paths(STORAGE, state_root=STATE_ROOT)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "DOCKER_", "RUSSOUND_"))}
    try:
        with operator_lock(storage["evidence"]):
            report = retention(storage, StateStore(storage["state"], owner_uid=os.getuid()).status(),
                               apply=args.apply, run=docker_runner(["docker", "--context", local_engine()], env),
                               updater_root=UPDATER_ROOT if UPDATER_ROOT.is_dir() else None)
    except (RetentionError, CleanupError, ComposeError, ChannelError, StateError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

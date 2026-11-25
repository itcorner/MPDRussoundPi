"""Remove this project's Docker images that no version still needs.

See docs/plans/general_done_docker-image-cleanup.md. Kept: images used by any
container, every deployment's committed version and the previous version (the
images its committed operation backed up). Nothing is removed while the
deployment state is blocked, reserved or pending. Only the project's own
namespaces are considered, images are removed one reference at a time without
``--force``, and never with a global prune. A restore does not need these tags:
it loads the old images from the archives in the operation's backup.

Runs automatically on the target after a verified-complete update (best effort,
under the operator lock) and manually::

    sudo .venv/bin/python -m tool.deployment_cleanup images            # dry run
    sudo .venv/bin/python -m tool.deployment_cleanup images --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from mpdrussoundpi.common.imageregistry import REPOSITORIES

logger = logging.getLogger("mpdrussoundpi.cleanup")

COMPONENTS = ("web", "ser2net", "dummy")
PROJECT_REPOSITORIES = frozenset(
    [f"mpdrussoundpi-recovery/{name}" for name in COMPONENTS]
    + [f"mpdrussoundpi-local/{name}" for name in COMPONENTS]
    + list(REPOSITORIES.values()))
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kKMGTP]?)B")
UNITS = {"": 1, "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5}
NONE = "<none>"

Run = Callable[[list[str]], str]


class CleanupError(RuntimeError):
    """The cleanup could not decide safely; nothing was removed."""


def parse_size(text: object) -> int:
    """Docker's human sizes are decimal (``215MB``, ``1.486GB``, ``0B``)."""
    match = SIZE.match(text.strip()) if isinstance(text, str) else None
    return round(float(match.group(1)) * UNITS[match.group(2)]) if match else 0


def _digests(*values: object) -> set[str]:
    return {value for value in values if isinstance(value, str) and DIGEST.fullmatch(value)}


def _previous(assets: Path, operation: object) -> set[str]:
    """Image IDs the committed operation saved before replacing them (its backup's images.json)."""
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f-]{36}", operation):
        return set()
    try:
        images = json.loads((assets / operation / "images.json").read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(images, dict):
        return set()
    return set().union(*(_digests(image.get("reference"), image.get("config"))
                         for image in images.values() if isinstance(image, dict)))


def keep_set(status: dict[str, Any], assets: Path) -> dict[str, str]:
    """Image digest → reason, for every deployment in the state; raises when it is not safe to decide."""
    if status.get("blocked") or status.get("reservation") is not None:
        raise CleanupError("The deployment state is blocked or reserved; nothing is removed.")
    deployments = status.get("deployments")
    if not isinstance(deployments, dict) or not deployments:
        raise CleanupError("No deployment is recorded; nothing is removed.")
    keep: dict[str, str] = {}
    for name, record in deployments.items():
        if not isinstance(record, dict) or record.get("pending") is not None:
            raise CleanupError(f"Deployment {name} has a pending operation; nothing is removed.")
        committed = record.get("committed")
        selection = committed.get("selection") if isinstance(committed, dict) else None
        if not isinstance(selection, dict):
            raise CleanupError(f"Deployment {name} has no committed version; nothing is removed.")
        candidate = selection.get("candidate") if isinstance(selection.get("candidate"), dict) else {}
        current: set[str] = set()
        for image in (candidate.get("images") or {}).values():
            if isinstance(image, dict):
                current |= _digests(image.get("index_digest"), image.get("platform_digest"), image.get("config_digest"))
        loaded = selection.get("loaded_images")
        if isinstance(loaded, dict):
            current |= _digests(*loaded.values())
        if not current:
            raise CleanupError(f"The committed images of {name} are not recorded; nothing is removed.")
        for digest in sorted(_previous(assets, selection.get("backup")) - current):
            keep.setdefault(digest, f"previous version of {name}")
        for digest in sorted(current):
            keep[digest] = f"committed version of {name}"
    return keep


def plan(images: list[dict[str, Any]], used: set[str], keep: dict[str, str]) -> dict[str, Any]:
    """Pure decision from ``docker image ls`` entries and the image IDs containers use."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for entry in images:
        if isinstance(entry, dict) and isinstance(entry.get("ID"), str):
            by_id.setdefault(entry["ID"], []).append(entry)
    kept, remove, outside = [], [], []
    for image_id, entries in sorted(by_id.items()):
        project = [entry for entry in entries if entry.get("Repository") in PROJECT_REPOSITORIES]
        foreign = [entry for entry in entries if entry not in project]
        names = [_name(entry) for entry in project]
        outside += [_name(entry) for entry in foreign if entry.get("Repository") != NONE]
        if not project:
            continue
        reason = "used by a container" if image_id in used else keep.get(image_id)
        if reason:
            kept.append({"image": image_id, "references": names, "reason": reason})
            continue
        tags = [_name(entry) for entry in project if entry.get("Tag") not in (None, "", NONE)]
        # The ID itself is removed only when nothing outside the project refers to it.
        remove.append({"image": image_id, "references": names, "tags": tags,
                       "by_id": not foreign, "size": parse_size(project[0].get("Size"))})
    return {"kept": kept, "remove": remove, "outside": sorted(set(outside))}


def _name(entry: dict[str, Any]) -> str:
    tag = entry.get("Tag")
    return entry.get("Repository", NONE) + (":" + tag if tag not in (None, "", NONE) else "")


def _images(run: Run) -> list[dict[str, Any]]:
    lines = run(["image", "ls", "--no-trunc", "--format", "{{json .}}"]).splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _used(run: Run) -> set[str]:
    ids = run(["container", "ls", "--all", "--quiet", "--no-trunc"]).split()
    return set(run(["container", "inspect", "--format", "{{.Image}}", *ids]).split()) if ids else set()


def _images_usage(run: Run) -> tuple[int, int]:
    """Docker's total image size and its "reclaimable" part (every image no container uses)."""
    for line in run(["system", "df", "--format", "{{json .}}"]).splitlines():
        item = json.loads(line) if line.strip() else {}
        if item.get("Type") == "Images":
            return parse_size(item.get("Size")), parse_size(item.get("Reclaimable"))
    return 0, 0


def cleanup(run: Run, status: dict[str, Any], assets: Path, *, apply: bool) -> dict[str, Any]:
    """Decide and, with ``apply``, remove. Raises CleanupError before removing anything when unsafe."""
    keep = keep_set(status, assets)
    decided = plan(_images(run), _used(run), keep)
    # No byte estimate for the removal itself: images share layers, and Docker does not
    # say which layers only the removed images use. --apply measures what it frees.
    before, reclaimable = _images_usage(run)
    report: dict[str, Any] = {**decided, "applied": apply, "removed": 0, "skipped": [],
                              "docker_reclaimable_bytes": reclaimable, "freed_bytes": 0}
    if not apply:
        return report
    for item in decided["remove"]:
        references = item["tags"] + ([item["image"]] if item["by_id"] else [])
        removed_any = False
        for reference in references:
            try:
                run(["image", "rm", reference])
                removed_any = True
            except (CleanupError, subprocess.SubprocessError, OSError) as error:
                if reference == item["image"] and removed_any:
                    continue  # the last tag already removed the image
                report["skipped"].append({"reference": reference, "error": str(error)[:200]})
        report["removed"] += removed_any
    report["freed_bytes"] = max(0, before - _images_usage(run)[0])
    return report


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """The small result the operator and the updater pass on."""
    return {"removed": report.get("removed", 0), "skipped": len(report.get("skipped", [])),
            "freed_bytes": report.get("freed_bytes", 0), "error": report.get("error")}


def docker_runner(docker: list[str], env: dict[str, str]) -> Run:
    def run(args: list[str]) -> str:
        result = subprocess.run(docker + args, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            message = (result.stderr or "").strip().splitlines()
            raise CleanupError(message[-1] if message else f"docker {' '.join(args[:2])} failed")
        return result.stdout
    return run


def after_update(target: Any) -> dict[str, Any]:
    """Best effort after a verified-complete update; never raises."""
    try:
        report = cleanup(docker_runner(target.docker, target.env), target.store.status(),
                         target.storage["assets"], apply=True)
        logger.info("Removed %s old images, freed %s bytes", report["removed"], report["freed_bytes"])
        return summary(report)
    except Exception as error:  # noqa: BLE001 - a cleanup problem must never fail a committed update
        logger.warning("Image cleanup after the update did not run: %s", error)
        return {"removed": 0, "skipped": 0, "freed_bytes": 0, "error": str(error)[:200]}


def _megabytes(value: int) -> str:
    return f"{value / 1000 ** 2:.0f} MB"


def render(report: dict[str, Any]) -> str:
    lines = [("Removed" if report["applied"] else "Would remove")
             + f" {report['removed'] if report['applied'] else len(report['remove'])} image(s)"]
    for item in report["remove"]:
        lines.append(f"  - {item['image'][7:19]} {', '.join(item['references'])}"
                     + ("" if item["by_id"] else " (tags only; the image is also used outside the project)"))
    lines.append("Kept:")
    lines += [f"  - {item['image'][7:19]} {', '.join(item['references'])}: {item['reason']}" for item in report["kept"]]
    if report["outside"]:
        lines.append("Outside the project namespaces (never removed automatically; decide by hand):")
        lines += [f"  - {name}" for name in report["outside"]]
    for item in report["skipped"]:
        lines.append(f"Skipped {item['reference']}: {item['error']}")
    lines.append(f"Freed {_megabytes(report['freed_bytes'])}." if report["applied"]
                 else f"Docker reports {_megabytes(report['docker_reclaimable_bytes'])} reclaimable in total; that also counts "
                 "the kept previous version and the images outside the project, and the images share layers. "
                 "--apply removes the listed images and reports the space actually freed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from tool.deployment_channel import ChannelError
    from tool.deployment_compose import ComposeError, local_engine, paths
    from tool.deployment_operator import STORAGE, operator_lock
    from tool.deployment_state import STATE_ROOT, StateStore

    parser = argparse.ArgumentParser(description="Remove this project's Docker images that no version needs.")
    sub = parser.add_subparsers(dest="command", required=True)
    images = sub.add_parser("images", help="dry run by default")
    images.add_argument("--apply", action="store_true", help="remove the listed images")
    images.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run as root from the trusted tooling directory")
    storage = paths(STORAGE, state_root=STATE_ROOT)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "DOCKER_", "RUSSOUND_"))}
    try:
        with operator_lock(storage["evidence"]):
            report = cleanup(docker_runner(["docker", "--context", local_engine()], env),
                             StateStore(storage["state"], owner_uid=os.getuid()).status(),
                             storage["assets"], apply=args.apply)
    except (CleanupError, ComposeError, ChannelError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

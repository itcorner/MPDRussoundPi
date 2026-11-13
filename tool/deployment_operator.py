"""Interactive controller entry point for reviewed remote development deployments."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import threading
from typing import Any, Callable
from uuid import uuid4

from tool.deployment_backup import FileBackup
from tool.deployment_channel import Channel, ChannelError, receive_backup, send_backup
from tool.deployment_compose import ComposeDeployment, ComposeError, encoded, paths, private_read, private_write
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_inputs import read_file, write_file
from tool.deployment_load import bundle_streams, select_bundle
from tool.deployment_local import LOCAL_CANDIDATE
from tool.deployment_images import MAX_METADATA
from tool.deployment_recovery import recover, recovery_assets, resume_original
from tool.deployment_state import STATE_ROOT, StateError, StateStore, candidate, identity, operation_id
from tool.deployment_transfer import ssh_arguments


TOOLS = Path("/opt/mpdrussoundpi-deploy")
STORAGE = Path("/var/lib/mpdrussoundpi-operations")


def serve(channel: Channel, storage: dict[str, Path]) -> None:
    request = channel.receive()
    if request.get("action") == "resume-original":
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid original-runtime request")
        result = resume_original(storage, request["deployment"], request["operation"],
                                 lambda review: approve_remote(channel, review))
        channel.send(result)
        return
    if request.get("action") == "recover":
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid recovery request")
        identity(request["deployment"])
        operation_id(request["operation"])
        header = channel.receive()
        # Always use a new receive parent, never replace the target's old backup.
        incoming = storage["incoming"] / str(uuid4())
        incoming.mkdir(mode=0o700)
        backup, checksum = receive_backup(channel, incoming, header)
        if backup.name != request["operation"]:
            raise ChannelError("Recovery operation and backup differ")
        result = recover(storage, request["deployment"], request["operation"], backup, checksum,
                         lambda review: approve_remote(channel, review))
        channel.send(result)
        return
    if (set(request) != {"action", "spec", "selection", "operation", "update"}
            or request["action"] != "deploy" or type(request["update"]) is not bool):
        raise ChannelError("Invalid development deployment request")
    store = StateStore(storage["state"], owner_uid=os.getuid())
    store.initialize()
    identity(request["spec"]["deployment"])
    committed = store.status()["deployments"].get(request["spec"]["deployment"], {}).get("committed")
    selection = request["selection"]
    if selection is None:
        if committed is None:
            raise ChannelError("No committed pin exists; first deployment needs an explicit candidate")
        selection = committed["selection"]["candidate"]
    candidate(selection)
    if (committed is not None and selection != committed["selection"]["candidate"] and not request["update"]):
        raise ChannelError("Changing committed images requires explicit --update")
    operation_id(request["operation"])
    target = ComposeDeployment(request["spec"], selection, storage, operation=request["operation"])
    target.preflight()
    unchanged = target.unchanged_images()
    if unchanged is not None:
        plan: dict[str, Any] = {"operation": target.operation, "deployment": target.spec["deployment"],
                "selection": selection, "scope": target.scope(), "action": "verify-unchanged-pin"}
        digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
        if approve_remote(channel, {"digest": digest, "plan": plan}) != digest:
            raise ChannelError("Unchanged runtime check was not approved")
        checks = target.checks(unchanged)
        channel.send({"event": "unchanged", "operation": target.operation,
                      "deployment": target.spec["deployment"], "required": checks})
        return

    def receive_images(operation: str, intent: str) -> Path:
        target.store.start_image_transfer(target.spec["deployment"], operation, selection, intent)
        channel.send({"event": "images", "operation": operation, "intent_digest": intent})
        path = storage["incoming"] / operation
        path.mkdir(mode=0o700)
        channel.receive_file(path, "manifest.json", maximum=MAX_METADATA,
                             checksum=selection["manifest_sha256"].removeprefix("sha256:"))
        manifest = selection["manifest"]
        for component in sorted(manifest["images"]):
            image = manifest["images"][component]
            channel.receive_file(path, image["archive"], size=image["bytes"],
                                 checksum=image["sha256"].removeprefix("sha256:"))
        actual = select_bundle(path, selection["manifest_sha256"], manifest["gateway"], manifest["platform"])
        if actual != selection:
            raise ChannelError("Transferred image bytes differ from reviewed candidate")
        target.store.complete_image_transfer(target.spec["deployment"], operation, selection, intent)
        return path

    def offhost(backup: FileBackup) -> None:
        recovery_assets(backup.path, backup.operation, backup.manifest_sha256)
        send_backup(channel, backup.path, backup.operation, backup.manifest_sha256)
        if channel.receive() != {"event": "backup-verified", "operation": backup.operation,
                                 "manifest_sha256": backup.manifest_sha256}:
            raise ChannelError("Independent off-host backup verification was not acknowledged")
        target.quiescent()

    runner = target.runner(offhost, receive_images if selection["kind"] == LOCAL_CANDIDATE else None)
    result = runner.execute(lambda review: approve_remote(channel, review))
    channel.send({"event": "complete", **result,
                  "acceptance": {"controller": "reported-connected" if target.spec["require_controller"] else "not-requested",
                                 "playback": "reported-healthy" if target.spec["require_playback"] else "not-configured"}})


def approve_remote(channel: Channel, review: dict[str, Any]) -> str | None:
    channel.send({"event": "review", **review})
    answer = channel.receive()
    if set(answer) != {"event", "digest"} or answer["event"] != "approval":
        raise ChannelError("Expected explicit controller approval of the reviewed operation")
    return answer["digest"]


def controller(channel: Channel, request: dict[str, Any], evidence: Path,
               approve: Callable[[dict[str, Any]], str | None], *,
               bundle: Path | None = None, recovery: Path | None = None,
               recovery_checksum: str | None = None) -> dict[str, Any]:
    operation = request["operation"]
    evidence_id = operation + (".recovery" if request["action"] != "deploy" else "")
    channel.send(request)
    if request["action"] == "recover":
        if recovery is None or recovery_checksum is None:
            raise ChannelError("Recovery requires the private independently held backup and identity")
        recovery_assets(recovery, operation, recovery_checksum)
        send_backup(channel, recovery, operation, recovery_checksum)
    reviewed = None
    backup_received = False
    while True:
        event = channel.receive()
        if event.get("event") == "review":
            if reviewed is not None:
                raise ChannelError("Unexpected second approval request")
            reviewed = event
            if event["plan"]["operation"] != operation:
                raise ChannelError("Target reviewed a different operation")
            if request["action"] == "deploy" and (
                    (request["selection"] is not None and event["plan"]["selection"] != request["selection"])
                    or event["plan"]["deployment"] != request["spec"]["deployment"]
                    or event["plan"]["scope"]["project"] != request["spec"]["project"]):
                raise ChannelError("Target reviewed a different deployment scope")
            if (request["action"] == "deploy" and shutil.disk_usage(evidence).free
                    < event["plan"]["scope"]["offhost_free_bytes_required"]):
                channel.send({"event": "approval", "digest": None})
                raise ChannelError("Insufficient independent controller space for the retained recovery bundle")
            answer = approve(event)
            private_write(evidence / (evidence_id + ".review.json"), event)
            channel.send({"event": "approval", "digest": answer})
            if answer != event["digest"]:
                raise ChannelError("Operation was declined; target must not interrupt services")
        elif event.get("event") == "images":
            if (reviewed is None or request["action"] != "deploy" or bundle is None
                    or event != {"event": "images", "operation": operation, "intent_digest": reviewed["digest"]}):
                raise ChannelError("Unreviewed image transfer request")
            selection = request["selection"]
            if not is_mapping(selection) or selection.get("kind") != LOCAL_CANDIDATE:
                raise ChannelError("Registry or reused candidates must not request local archives")
            manifest = selection["manifest"]
            with bundle_streams(bundle, selection["manifest_sha256"], manifest["gateway"],
                                manifest["platform"]) as (actual, _, configs):
                if actual != manifest or configs != selection["configs"]:
                    raise ChannelError("Source image bytes changed after review")
                channel.send_file(bundle / "manifest.json")
                for component in sorted(manifest["images"]):
                    channel.send_file(bundle / manifest["images"][component]["archive"])
        elif event.get("event") == "backup":
            if reviewed is None or event["operation"] != operation or backup_received:
                raise ChannelError("Unreviewed or duplicate recovery transfer")
            backup, checksum = receive_backup(channel, evidence, event)
            recovery_assets(backup, operation, checksum)
            private_write(evidence / (operation + ".backup-identity.json"),
                          {"operation": operation, "manifest_sha256": checksum})
            backup_received = True
            channel.send({"event": "backup-verified", "operation": operation, "manifest_sha256": checksum})
        elif event.get("event") in ("complete", "recovered", "unchanged"):
            if reviewed is None or event["operation"] != operation:
                raise ChannelError("Target completion lacks matching reviewed approval")
            if request["action"] == "deploy" and event["event"] != "unchanged" and not backup_received:
                raise ChannelError("Target completed without verified off-host backup")
            private_write(evidence / (evidence_id + ".result.json"), event)
            return event
        elif event.get("event") == "error":
            raise ChannelError("Target operation failed; keep private evidence and inspect pending state before recovery")
        else:
            raise ChannelError("Unexpected deployment response; inspect target state without retrying")


def interactive(review: dict[str, Any]) -> str | None:
    print(json.dumps(review["plan"], indent=2))
    print("Approve this exact scope, including interruption and recovery retention.")
    expected = "approve " + review["plan"]["operation"]
    with open("/dev/tty", "r+", encoding="utf-8") as terminal:
        terminal.write("Type '" + expected + "' to proceed: ")
        terminal.flush()
        answer = terminal.readline().strip()
    return review["digest"] if answer == expected else None


def remote(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    evidence: Path = args.evidence
    with checked_directory(evidence, os.getuid()):
        pass
    trust_path: Path = args.known_hosts
    with checked_directory(trust_path.parent, os.getuid(), private=False) as parent:
        trust, trust_info = read_file(parent, trust_path.name, os.getuid(), (0o400, 0o600))
    # Snapshot host trust before connecting; it is never updated implicitly.
    evidence_id = request["operation"] + (".recovery" if request["action"] != "deploy" else "")
    snapshot = evidence / (evidence_id + ".known_hosts")
    with checked_directory(evidence, os.getuid()) as parent:
        write_file(parent, snapshot.name, trust, owner=os.getuid(), group=os.getgid(), mode=0o400)
    argv = ssh_arguments(args.host, args.user, args.port, snapshot, args.identity)
    argv += [args.host, f"cd {TOOLS} && exec sudo -n {TOOLS}/.venv/bin/python -m tool.deployment_operator serve"]
    with checked_directory(trust_path.parent, os.getuid(), private=False) as parent:
        current, current_info = read_file(parent, trust_path.name, os.getuid(), (0o400, 0o600))
    if current != trust or current_info != trust_info:
        raise ChannelError("SSH host trust changed before connection")
    log_path = evidence / (evidence_id + ".ssh.log")
    with log_path.open("xb") as log:
        os.fchmod(log.fileno(), 0o600)
        with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log) as process:
            assert process.stdin is not None and process.stdout is not None
            # Bounded controller lifetime. Killing SSH never proves target work ended.
            timer = threading.Timer(3600, process.kill)
            timer.start()
            try:
                def approved(review: dict[str, Any]) -> str | None:
                    print(f"SSH target: {args.user}@{args.host}:{args.port}; strict reviewed host trust")
                    return interactive(review)
                result = controller(Channel(process.stdout, process.stdin), request, evidence, approved,
                                    bundle=args.bundle, recovery=args.backup,
                                    recovery_checksum=args.backup_sha256)
                process.stdin.close()
                if process.wait(timeout=15):
                    raise ChannelError("SSH exit was unsuccessful; completion is uncertain")
                return result
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "recover", "resume-original", "serve"))
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--deployment")
    parser.add_argument("--operation")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    args = parser.parse_args()
    try:
        if args.action == "serve":
            if os.geteuid() != 0 or Path(__file__).resolve().parents[1] != TOOLS:
                raise ComposeError("Target tooling must be installed at the fixed trusted root-owned path")
            for path in [TOOLS, TOOLS / "tool", *list((TOOLS / "tool").glob("*.py"))]:
                info = path.lstat()
                if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ComposeError("Target tooling ownership or permissions are unsafe")
            with checked_directory(TOOLS, 0, private=False):
                pass
            storage = paths(STORAGE, state_root=STATE_ROOT)
            StateStore().initialize()
            channel = Channel(sys.stdin.buffer, sys.stdout.buffer)
            try:
                serve(channel, storage)
            except (OSError, ValueError, subprocess.SubprocessError):
                channel.send({"event": "error"})
                raise
        else:
            if not all((args.host, args.user, args.known_hosts, args.evidence)):
                parser.error("Select explicit --host, --user, --known-hosts and a private --evidence directory")
            if args.action == "deploy":
                if args.spec is None:
                    parser.error("deploy requires --spec")
                spec = json.loads(private_read(args.spec))
                if args.bundle is not None:
                    if args.selection is not None or not args.manifest_sha256:
                        parser.error("local deployment needs --bundle and --manifest-sha256, not --selection")
                    selection = select_bundle(args.bundle, args.manifest_sha256, spec["gateway"], spec["platform"])
                elif args.selection is not None:
                    selection = json.loads(private_read(args.selection))
                else:
                    selection = None
                request: dict[str, Any] = {
                    "action": "deploy", "spec": spec, "selection": selection,
                    "operation": str(uuid4()), "update": args.update,
                }
            elif args.action == "recover":
                if not all((args.deployment, args.operation, args.backup, args.backup_sha256)):
                    parser.error("recover requires --deployment, --operation, --backup and --backup-sha256")
                request = {"action": "recover", "deployment": args.deployment, "operation": args.operation}
            else:
                if not all((args.deployment, args.operation)):
                    parser.error("resume-original requires --deployment and --operation")
                request = {"action": "resume-original", "deployment": args.deployment, "operation": args.operation}
            print(json.dumps(remote(args, request)))
        return 0
    except (ComposeError, ChannelError, StateError) as error:
        print(str(error) + ". Retain private evidence and inspect target state before retrying.", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Deployment failed or is uncertain. Retain private evidence; inspect target state before any retry or recovery.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

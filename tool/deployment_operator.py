"""Interactive controller entry point for reviewed remote development deployments."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import shutil
import sys
import threading
from typing import Any, Callable, Generator
from uuid import uuid4

from tool.deployment_backup import FileBackup
from tool.deployment_channel import Channel, ChannelError, portable_backup, receive_backup, send_backup
from tool.deployment_compose import ComposeDeployment, ComposeError, encoded, paths, private_read, private_write
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_inputs import read_file, write_file
from tool.deployment_load import bundle_streams, select_bundle
from tool.deployment_local import LOCAL_CANDIDATE
from tool.deployment_images import MAX_METADATA
from tool.deployment_native_source import NativeError
from tool.deployment_recovery import recover, recovery_assets, resume_original
from tool.deployment_state import STATE_ROOT, StateError, StateStore, candidate, identity, operation_id
from tool.deployment_transfer import ssh_arguments


TOOLS = Path("/opt/mpdrussoundpi-deploy")
STORAGE = Path("/var/lib/mpdrussoundpi-operations")
DEPLOY_ACTIONS = ("deploy", "native-deploy", "handover-deploy")
DEFAULT_CONTROLLER_TIMEOUT = 3600
MAX_CONTROLLER_TIMEOUT = 5 * 3600
CLEANUP_NOTE = ("After a verified-complete update, this project's Docker images that neither the new "
                "nor the previous version uses are removed; backups keep their image archives.")


@contextmanager
def operator_lock(evidence: Path) -> Generator[None, None, None]:
    """Serialize reviewed target operations without releasing durable reservations."""
    with checked_directory(evidence, os.getuid()) as directory:
        fd = os.open("operator.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            info = os.fstat(fd)
            current = os.stat("operator.lock", dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ChannelError("Unsafe target operator lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ChannelError("Another target operator is active; inspect its outcome before recovery") from None
            yield
        finally:
            os.close(fd)


def serve(channel: Channel, storage: dict[str, Path]) -> None:
    request = channel.receive()
    if request.get("action") == "handover-deploy":
        from tool.deployment_handover_target import HandoverTarget
        from tool.deployment_handover_transaction import HandoverHooks, execute
        fields = {"action", "deployment", "operation", "native_spec", "committed", "selection"}
        if set(request) not in (fields, fields | {"playback_subnet"}):
            raise ChannelError("Invalid handover deployment request")
        store = StateStore(storage["state"], owner_uid=os.getuid())
        store.initialize()
        subnet = request.get("playback_subnet")
        if "playback_subnet" in request and not isinstance(subnet, str):
            raise ChannelError("Invalid handover playback subnet")
        target = HandoverTarget(store, storage, request["deployment"], request["operation"], request["selection"],
                                **({"playback_subnet": subnet} if subnet is not None else {}))
        if target.native_spec != request["native_spec"] or target.committed != request["committed"]:
            raise ChannelError("Target native baseline differs from independently supplied controller inputs")
        target.preflight()
        if target.backup_estimate is None:
            raise ChannelError("Handover backup capacity was not established")
        hooks = HandoverHooks(preflight=target.preflight, receive_images=lambda digest: target.receive_images(channel, digest),
                              stop_native=target.stop_native, capture_backup=lambda: target.capture_backup(channel),
                              load_images=target.load_images, apply=target.activate, checks=target.checks)
        result = execute(store, storage, request["deployment"], request["operation"],
                         committed=target.committed, native_spec=target.native_spec, selection=target.selection,
                         engine=target.reviewed_engine(), hooks=hooks,
                         approve=lambda review: approve_remote(channel, review),
                         offhost_free_bytes_required=target.backup_estimate, playback_subnet=subnet)
        channel.send(result)
        return
    if request.get("action") == "native-deploy":
        from tool.deployment_native import NativeDeployment, recovery_assets as native_assets
        if (set(request) != {"action", "spec", "selection", "operation", "update"}
                or type(request["update"]) is not bool):
            raise ChannelError("Invalid native deployment request")
        target_native = NativeDeployment(request["spec"], request["selection"], storage,
                                         operation=request["operation"], update=request["update"])
        def native_offhost(backup: FileBackup) -> None:
            native_assets(backup.path, backup.operation, backup.manifest_sha256)
            send_backup(channel, backup.path, backup.operation, backup.manifest_sha256)
            if channel.receive() != {"event": "backup-verified", "operation": backup.operation,
                                     "manifest_sha256": backup.manifest_sha256}:
                raise ChannelError("Independent native backup verification was not acknowledged")
            target_native.quiescent()
        channel.send(target_native.execute(lambda review: approve_remote(channel, review), native_offhost))
        return
    if request.get("action") == "native-resume-original":
        from tool.deployment_native import resume_original as native_resume
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid native original-runtime request")
        channel.send(native_resume(storage, request["deployment"], request["operation"],
                                   lambda review: approve_remote(channel, review)))
        return
    if request.get("action") == "handover-resume-original":
        from tool.deployment_pi_handover import resume_before_backup
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid handover original-runtime request")
        channel.send(resume_before_backup(storage, request["deployment"], request["operation"],
                                         lambda review: approve_remote(channel, review)))
        return
    if request.get("action") == "resume-original":
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid original-runtime request")
        result = resume_original(storage, request["deployment"], request["operation"],
                                 lambda review: approve_remote(channel, review))
        channel.send(result)
        return
    if request.get("action") in ("recover", "native-recover", "handover-recover-backup",
                                  "handover-recover-activated", "handover-recover-uncertain-load"):
        if set(request) != {"action", "deployment", "operation"}:
            raise ChannelError("Invalid recovery request")
        identity(request["deployment"])
        operation_id(request["operation"])
        header = channel.receive()
        if header.get("operation") != request["operation"]:
            raise ChannelError("Recovery operation and backup differ")
        # Always use a new receive parent, never replace the target's old backup.
        incoming = storage["incoming"] / str(uuid4())
        incoming.mkdir(mode=0o700)
        backup, checksum = receive_backup(channel, incoming, header, restore_root=storage["restores"])
        if backup.name != request["operation"]:
            raise ChannelError("Recovery operation and backup differ")
        if request["action"] in ("handover-recover-backup", "handover-recover-activated",
                                  "handover-recover-uncertain-load"):
            from tool.deployment_pi_handover import recover_before_activation
            result = recover_before_activation(storage, request["deployment"], request["operation"],
                                               backup_path=backup, checksum=checksum,
                                               approve=lambda review: approve_remote(channel, review),
                                               activated=request["action"] == "handover-recover-activated",
                                               uncertain_load=request["action"] == "handover-recover-uncertain-load")
            channel.send(result)
            return
        recovery_function = recover
        if request["action"] == "native-recover":
            from tool.deployment_native import recover as native_recover
            recovery_function = native_recover
        result = recovery_function(storage, request["deployment"], request["operation"], backup, checksum,
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
    # Committed and verified: remove images and older operations' large files no version needs
    # (best effort, still under the lock; neither changes the outcome).
    from tool.deployment_cleanup import after_update
    from tool.deployment_retention import after_update as retain_after_update
    cleanup = after_update(target)
    retention = retain_after_update(target)
    channel.send({"event": "complete", **result, "cleanup": cleanup, "retention": retention,
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
    deploying = request["action"] in DEPLOY_ACTIONS
    native = request["action"].startswith("native-")
    evidence_id = operation + (".recovery" if not deploying else "")
    backup_validator = recovery_assets
    if native:
        from tool.deployment_native import recovery_assets as native_assets
        backup_validator = native_assets
    channel.send(request)
    if request["action"] in ("recover", "native-recover", "handover-recover-backup",
                             "handover-recover-activated", "handover-recover-uncertain-load"):
        if recovery is None or recovery_checksum is None:
            raise ChannelError("Recovery requires the private independently held backup and identity")
        if request["action"] in ("handover-recover-backup", "handover-recover-activated",
                                  "handover-recover-uncertain-load"):
            portable_backup(recovery, operation, recovery_checksum)
        else:
            backup_validator(recovery, operation, recovery_checksum)
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
            if request["action"] == "native-deploy" and (
                    event["plan"]["kind"] != "native-deployment-review"
                    or event["plan"]["selection"] != request["selection"]
                    or event["plan"]["deployment"] != request["spec"]["deployment"]
                    or event["plan"]["scope"]["listener"] != {
                        "host": request["spec"]["publish_host"], "port": request["spec"]["publish_port"]}
                    or event["plan"]["scope"]["gateway_listener"] != {
                        "host": "127.0.0.1", "port": request["spec"]["backend_port"]}
                    or event["plan"]["scope"]["playback_agents"] != [
                        item["agent_id"] for item in request["spec"]["playback_agents"]]):
                raise ChannelError("Target reviewed a different native deployment scope")
            if request["action"] == "handover-deploy":
                from tool.deployment_pi_handover import prepare
                if not is_mapping(request["selection"]):
                    raise ChannelError("Handover requires a verified local image selection")
                project = "pi-handover-" + operation.replace("-", "")
                spec, config = prepare(request["native_spec"], request["committed"]["selection"]["candidate"],
                                       request["selection"], project=project,
                                       compose_path=str(STORAGE / "runtime" / operation / "compose.json"),
                                       playback_subnet=request.get("playback_subnet"))
                plan = event["plan"]
                expected: dict[str, Any] = {
                    "kind": "pi-handover-transaction", "deployment": request["deployment"], "operation": operation,
                    "committed": request["committed"], "selection": request["selection"],
                    "native_spec": request["native_spec"], "compose_spec": spec, "compose_config": config,
                    "engine": plan.get("engine"), "interrupt_native": True, "preserve_data_in_place": True,
                    "offhost_backup_required": True, "automatic_rollback": False,
                    "offhost_free_bytes_required": plan.get("offhost_free_bytes_required"),
                }
                if (plan != expected or not isinstance(plan["engine"], str) or not plan["engine"]
                        or type(plan["offhost_free_bytes_required"]) is not int
                        or plan["offhost_free_bytes_required"] <= 0
                        or event.get("digest") != "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()):
                    raise ChannelError("Target reviewed a different handover deployment scope")
            if (not deploying and event["plan"]["deployment"] != request["deployment"]):
                raise ChannelError("Target reviewed a different recovery deployment")
            if (request["action"] == "handover-resume-original"
                    and (event["plan"].get("kind") != "pi-handover-resume-original"
                         or event["plan"].get("restore_data") is not False
                         or event["plan"].get("preserve_operator_edits") is not True)):
                raise ChannelError("Target reviewed a different handover recovery action")
            if (request["action"] in ("handover-recover-backup", "handover-recover-activated",
                                       "handover-recover-uncertain-load")
                    and (event["plan"].get("kind") != "pi-handover-backup-recovery"
                         or event["plan"].get("backup_manifest_sha256") != recovery_checksum
                         or event["plan"].get("restore_matching_data") is not True
                         or event["plan"].get("preserve_post_backup_tree") is not True
                         or event["plan"].get("operator_confirmed_original_orchestration_ended") is not True
                         or event["plan"].get("activated") is not
                         (request["action"] == "handover-recover-activated"))):
                raise ChannelError("Target reviewed a different handover backup recovery")
            if (request["action"] == "handover-recover-uncertain-load"
                    and (event["plan"].get("uncertain_image_load") is not True
                         or event["plan"].get("operator_confirmed_daemon_load_ended") is not True
                         or event["plan"].get("partial_images_and_tags") != "retained"
                         or event["plan"].get("image_load_retry") is not False
                         or event.get("digest") != "sha256:" + hashlib.sha256(encoded(event["plan"])).hexdigest())):
                raise ChannelError("Target reviewed a different uncertain image-load resolution")
            required_space = (event["plan"]["offhost_free_bytes_required"] if request["action"] == "handover-deploy"
                              else event["plan"]["scope"]["offhost_free_bytes_required"] if deploying else 0)
            if deploying and shutil.disk_usage(evidence).free < required_space:
                channel.send({"event": "approval", "digest": None})
                raise ChannelError("Insufficient independent controller space for the retained recovery bundle")
            answer = approve(event)
            private_write(evidence / (evidence_id + ".review.json"), event)
            channel.send({"event": "approval", "digest": answer})
            if answer != event["digest"]:
                raise ChannelError("Operation was declined; target must not interrupt services")
        elif event.get("event") == "images":
            if (reviewed is None or request["action"] not in ("deploy", "handover-deploy") or bundle is None
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
            if request["action"] == "handover-deploy":
                from tool.deployment_pi_handover import receive_handover_backup
                if not is_mapping(request["selection"]):
                    raise ChannelError("Handover requires a verified local image selection")
                receive_handover_backup(
                    channel, evidence, event, operation=operation, committed=request["committed"],
                    native_spec=request["native_spec"], image_selection=request["selection"],
                    native_asset=STORAGE / "assets" / request["committed"]["selection"]["operation"])
            else:
                backup, checksum = receive_backup(channel, evidence, event)
                backup_validator(backup, operation, checksum)
                private_write(evidence / (operation + ".backup-identity.json"),
                              {"operation": operation, "manifest_sha256": checksum})
                channel.send({"event": "backup-verified", "operation": operation, "manifest_sha256": checksum})
            backup_received = True
        elif event.get("event") in ("complete", "recovered", "unchanged"):
            if reviewed is None or event["operation"] != operation:
                raise ChannelError("Target completion lacks matching reviewed approval")
            if deploying and event["event"] != "unchanged" and not backup_received:
                raise ChannelError("Target completed without verified off-host backup")
            if request["action"] == "handover-deploy":
                images = event.get("images")
                selection = request["selection"]
                if (event["event"] != "complete" or event.get("deployment") != request["deployment"]
                        or not is_mapping(images) or not is_mapping(selection)
                        or set(images) != set(selection["manifest"]["images"])
                        or any(images[name] not in (image["reference"], selection["configs"][name])
                               for name, image in selection["manifest"]["images"].items())
                        or event.get("required") != dict.fromkeys(
                            ("apply", "containers", "http", "backend", "playback"), "passed")):
                    raise ChannelError("Handover completion does not match the reviewed images and checks")
            private_write(evidence / (evidence_id + ".result.json"), event)
            return event
        elif event.get("event") == "error":
            raise ChannelError("Target operation failed; keep private evidence and inspect pending state before recovery")
        else:
            raise ChannelError("Unexpected deployment response; inspect target state without retrying")


def interactive(review: dict[str, Any]) -> str | None:
    print(json.dumps(review["plan"], indent=2))
    if "selection" in review["plan"] and review["plan"].get("action") != "verify-unchanged-pin":
        print(CLEANUP_NOTE)
    print("Approve this exact scope, including interruption and recovery retention.")
    expected = "approve " + review["plan"]["operation"]
    with open("/dev/tty", "r", encoding="utf-8") as source, \
            open("/dev/tty", "w", encoding="utf-8") as terminal:
        if review["plan"].get("uncertain_image_load") is True:
            confirmation = "confirm load-ended " + review["plan"]["operation"]
            terminal.write("Only if you independently established that the original orchestration and "
                           "daemon-side load request ended, type '" + confirmation + "': ")
            terminal.flush()
            if source.readline().strip() != confirmation:
                return None
        terminal.write("Type '" + expected + "' to proceed: ")
        terminal.flush()
        answer = source.readline().strip()
    return review["digest"] if answer == expected else None


def remote(args: argparse.Namespace, request: dict[str, Any], *,
           timeout_seconds: int = DEFAULT_CONTROLLER_TIMEOUT) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_CONTROLLER_TIMEOUT:
        raise ChannelError(f"Controller timeout must be between 1 and {MAX_CONTROLLER_TIMEOUT} seconds")
    evidence: Path = args.evidence
    with checked_directory(evidence, os.getuid()):
        pass
    trust_path: Path = args.known_hosts
    with checked_directory(trust_path.parent, os.getuid(), private=False) as parent:
        trust, trust_info = read_file(parent, trust_path.name, os.getuid(), (0o400, 0o600))
    # Snapshot host trust before connecting; it is never updated implicitly.
    evidence_id = request["operation"] + (".recovery" if request["action"] not in DEPLOY_ACTIONS else "")
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
            timer = threading.Timer(timeout_seconds, process.kill)
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
            except ChannelError as error:
                # The target's own reason (for example a held operator lock) is only in its stderr.
                raise ChannelError(f"{error} (target messages: {log_path})") from error
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "recover", "resume-original", "serve",
                                          "native-deploy", "native-recover", "native-resume-original",
                                          "handover-deploy",
                                          "handover-resume-original", "handover-recover-backup",
                                          "handover-recover-activated", "handover-recover-uncertain-load"))
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
    parser.add_argument("--playback-subnet", help="Dedicated RFC1918 /28 or /29 for the Pi handover bridge")
    parser.add_argument("--operation")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--controller-timeout-seconds", type=int,
                        help=f"Remote session limit (default {DEFAULT_CONTROLLER_TIMEOUT}, maximum "
                             f"{MAX_CONTROLLER_TIMEOUT}); does not extend an active session")
    args = parser.parse_args()
    if args.playback_subnet is not None and args.action != "handover-deploy":
        parser.error("--playback-subnet applies only to handover-deploy")
    timeout = args.controller_timeout_seconds
    if timeout is not None:
        if args.action == "serve":
            parser.error("--controller-timeout-seconds applies only to the controller")
        if not 1 <= timeout <= MAX_CONTROLLER_TIMEOUT:
            parser.error(f"--controller-timeout-seconds must be between 1 and {MAX_CONTROLLER_TIMEOUT}")
    else:
        timeout = DEFAULT_CONTROLLER_TIMEOUT
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
                with operator_lock(storage["evidence"]):
                    serve(channel, storage)
            except (OSError, ValueError, subprocess.SubprocessError):
                channel.send({"event": "error"})
                raise
        else:
            if not all((args.host, args.user, args.known_hosts, args.evidence)):
                parser.error("Select explicit --host, --user, --known-hosts and a private --evidence directory")
            if args.action == "handover-deploy":
                if not all((args.spec, args.selection, args.bundle, args.manifest_sha256, args.deployment)):
                    parser.error("handover-deploy requires --deployment, --spec, --selection, --bundle and --manifest-sha256")
                from tool.deployment_native import specification as native_specification
                spec = native_specification(json.loads(private_read(args.spec)))
                committed = json.loads(private_read(args.selection))
                if spec["deployment"] != args.deployment:
                    raise ChannelError("Controller native specification differs from deployment")
                image_selection = select_bundle(args.bundle, args.manifest_sha256, "managed",
                                                {"aarch64": "linux/arm64", "x86_64": "linux/amd64"}[
                                                    committed["selection"]["candidate"]["runtime"]["machine"]])
                request = {"action": args.action, "deployment": args.deployment, "operation": str(uuid4()),
                           "native_spec": spec, "committed": committed, "selection": image_selection}
                if args.playback_subnet is not None:
                    request["playback_subnet"] = args.playback_subnet
            elif args.action == "native-deploy":
                if args.spec is None or args.selection is None or args.bundle is not None:
                    parser.error("native-deploy requires --spec and --selection, not image archives")
                from tool.deployment_native import specification as native_specification
                from tool.deployment_native_source import validate_candidate as native_candidate
                spec = native_specification(json.loads(private_read(args.spec)))
                selection = json.loads(private_read(args.selection))
                native_candidate(selection)
                request = {"action": args.action, "spec": spec, "selection": selection,
                           "operation": str(uuid4()), "update": args.update}
            elif args.action == "deploy":
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
            elif args.action in ("recover", "native-recover", "handover-recover-backup",
                                 "handover-recover-activated", "handover-recover-uncertain-load"):
                if not all((args.deployment, args.operation, args.backup, args.backup_sha256)):
                    parser.error("recover requires --deployment, --operation, --backup and --backup-sha256")
                request = {"action": args.action, "deployment": args.deployment, "operation": args.operation}
            else:
                if not all((args.deployment, args.operation)):
                    parser.error("resume-original requires --deployment and --operation")
                request = {"action": args.action, "deployment": args.deployment, "operation": args.operation}
            print(json.dumps(remote(args, request, timeout_seconds=timeout)))
        return 0
    except (ComposeError, ChannelError, StateError, NativeError) as error:
        print(str(error) + ". Retain private evidence and inspect target state before retrying.", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Deployment failed or is uncertain. Retain private evidence; inspect target state before any retry or recovery.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

import copy
import getpass
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool import deployment_build as builds
from tool import deployment_transfer as transfers
from tool.deployment_load import load_reserved_bundle, select_bundle
from tool.deployment_state import StateError, StateStore
from test_deployment_build import fixture_bundle


ROOT = Path(__file__).resolve().parents[1]
INTENT = "sha256:" + "d" * 64


class ImageTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-transfer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.bundle, self.checksum, _ = fixture_bundle(self.root, "managed")
        self.selection = select_bundle(self.bundle, self.checksum, "managed", "linux/arm64")
        self.operation = str(uuid4())
        self.store = StateStore(self.root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.received = self.root / "received"
        self.received.mkdir(mode=0o700)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.trust = self.root / "known_hosts"
        self.trust.write_text("fixture-host ssh-ed25519 synthetic-fixture-public-key\n")
        self.trust.chmod(0o600)
        self.transfer = self.prepare()
        self.calls = []

    def prepare(self, **overrides):
        args = {
            "deployment": "demo", "operation": self.operation, "intent_digest": INTENT,
            "host": "fixture-host", "user": "fixture", "port": 22, "known_hosts": self.trust,
            "remote_tools": Path("/opt/russound-tools"), "remote_parent": Path("/private/received"),
        }
        return transfers.ImageTransfer(self.bundle, self.checksum, "managed", "linux/arm64",
                                       **{**args, **overrides})

    def begin(self):
        self.store.begin("demo", self.operation, self.selection, INTENT, ["apply"])

    def wire(self, request=None):
        value = request or self.transfer._request
        header = transfers.encoded(value)
        raw = struct.pack("!Q", len(header)) + header + (self.bundle / "manifest.json").read_bytes()
        for _, image in sorted(value["selection"]["manifest"]["images"].items()):
            raw += (self.bundle / image["archive"]).read_bytes()
        return raw

    def fake_ssh(self, argv, **kwargs):
        expected = self.transfer._argv.copy()
        expected[expected.index("UserKnownHostsFile=" + str(self.trust))] = (
            "UserKnownHostsFile=" + str(self.evidence / (self.operation + ".known_hosts")))
        self.assertEqual(argv, expected)
        self.assertEqual((self.evidence / (self.operation + ".known_hosts")).read_bytes(), self.trust.read_bytes())
        self.calls.append(argv)
        result = transfers.receive(self.store, self.received, kwargs["stdin"])
        kwargs["stdout"].write(transfers.encoded(result))
        return subprocess.CompletedProcess(argv, 0)

    def send(self, transfer=None, **kwargs):
        transfer = transfer or self.transfer
        return transfer.send(kwargs.get("approved_digest", transfer.review()["digest"]),
                             kwargs.get("evidence", self.evidence))

    def test_acknowledged_pair_transfers_verifies_and_keeps_state_pending(self):
        self.begin()
        with patch.object(transfers.subprocess, "run", side_effect=self.fake_ssh):
            result = self.send()
        self.assertEqual(result, transfers.receipt(self.transfer._request))
        target = self.received / self.operation
        self.assertEqual(select_bundle(target, self.checksum, "managed", "linux/arm64"), self.selection)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        for path in target.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        state = self.store.status()
        self.assertTrue(state["image_transfer_started"])
        self.assertTrue(state["image_transfer_verified"])
        self.assertFalse(state["image_load_started"])
        self.assertEqual(state["deployments"]["demo"]["pending"]["phase"], "prepared")
        self.assertIsNone(state["deployments"]["demo"]["pending"]["loaded_images"])
        self.assertTrue(state["blocked"])
        log = self.evidence / (self.operation + ".transfer.log")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.evidence / (self.operation + ".transfer.json")).is_file())
        with patch.object(transfers.subprocess, "run", side_effect=self.fake_ssh):
            with self.assertRaises(transfers.TransferError):
                self.send()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_acknowledgement_plan_or_trust_drift_never_invokes_ssh(self):
        self.begin()
        with patch.object(transfers.subprocess, "run") as run:
            with self.assertRaises(transfers.TransferError):
                self.send(approved_digest="sha256:" + "e" * 64)
            self.trust.write_text("new public host trust\n")
            with self.assertRaises(transfers.TransferError):
                self.send()
            run.assert_not_called()
        self.assertFalse(self.store.status()["image_transfer_started"])
        self.assertEqual(list(self.evidence.iterdir()), [])
        self.transfer = self.prepare()
        self.transfer._argv[-2] = "different-host"
        with patch.object(transfers.subprocess, "run") as run:
            with self.assertRaises(transfers.TransferError):
                self.send()
            run.assert_not_called()

    def test_source_drift_and_mixed_candidate_never_start_transport(self):
        self.begin()
        self.transfer._request["selection"]["configs"]["web"] = "sha256:" + "e" * 64
        with patch.object(transfers.subprocess, "run") as run:
            with self.assertRaises(transfers.TransferError):
                self.send()
            run.assert_not_called()
        self.transfer = self.prepare()
        path = self.bundle / "web.tar"
        path.chmod(0o600)
        with patch.object(transfers.subprocess, "run") as run:
            with self.assertRaises(transfers.TransferError):
                self.send()
            run.assert_not_called()
        self.assertFalse(self.store.status()["image_transfer_started"])

    def test_ssh_arguments_pin_trust_disable_ambient_configuration_and_quote_remote_paths(self):
        transfer = self.prepare(remote_tools=Path("/opt/tools'; false; '"),
                                remote_parent=Path("/private/artifacts with spaces"))
        args = transfer.review()["plan"]["ssh"]
        self.assertEqual(args[:4], ["ssh", "-F", "/dev/null", "-T"])
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no",
                       "ClearAllForwardings=yes", "GlobalKnownHostsFile=/dev/null",
                       "UpdateHostKeys=no", "VerifyHostKeyDNS=no",
                       "UserKnownHostsFile=" + str(self.trust)):
            self.assertIn(option, args)
        self.assertNotIn("sudo", args[-1])
        self.assertIn("exec /usr/bin/python3 -m tool.deployment_transfer receive", args[-1])
        self.assertIn("'/private/artifacts with spaces'", args[-1])
        for values in ({"host": "-oProxyCommand=bad"}, {"user": "root;bad"}, {"port": True},
                       {"remote_parent": Path("/")}, {"remote_tools": Path("../tools")},
                       {"known_hosts": self.root / "%h"}, {"known_hosts": Path("relative")}):
            with self.subTest(values=values), self.assertRaises(transfers.TransferError):
                self.prepare(**values)

    def test_receiver_rejects_missing_or_mismatched_reservation_before_artifact_creation(self):
        wire = self.wire()
        with self.assertRaises(StateError):
            transfers.receive(self.store, self.received, io.BytesIO(wire))
        self.begin()
        for overrides in ({"intent_digest": "sha256:" + "e" * 64}, {"operation": str(uuid4())},
                          {"deployment": "other"}):
            value = {**self.transfer._request, **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(StateError):
                transfers.receive(self.store, self.received, io.BytesIO(self.wire(value)))
        self.assertEqual(list(self.received.iterdir()), [])
        self.assertFalse(self.store.status()["image_transfer_started"])
        self.store.advance("demo", self.operation, "inputs-verified")
        self.store.advance("demo", self.operation, "backup-complete")
        with self.assertRaises(StateError):
            transfers.receive(self.store, self.received, io.BytesIO(wire))

    def test_truncation_corruption_and_trailing_bytes_keep_marker_no_success(self):
        original = self.wire()
        for fault in ("truncated", "corrupt", "trailing"):
            self.store = StateStore(self.root / ("state-" + fault), owner_uid=os.getuid())
            self.store.initialize()
            self.received = self.root / ("received-" + fault)
            self.received.mkdir(mode=0o700)
            self.begin()
            raw = original[:-10] if fault == "truncated" else (
                original[:-1] + bytes([original[-1] ^ 1]) if fault == "corrupt" else original + b"extra")
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                transfers.receive(self.store, self.received, io.BytesIO(raw))
            self.assertTrue(self.store.status()["image_transfer_started"])
            self.assertFalse(self.store.status()["image_transfer_verified"])
            self.assertFalse((self.received / self.operation / "transfer.json").exists())
            with self.assertRaises(StateError):
                transfers.receive(self.store, self.received, io.BytesIO(original))
            self.assertTrue(self.store.status()["blocked"])
            self.store.advance("demo", self.operation, "inputs-verified")
            self.store.advance("demo", self.operation, "backup-complete")
            with self.assertRaises(StateError):
                self.store.start_image_load("demo", self.operation, self.selection, INTENT)
            self.assertFalse(self.store.status()["image_load_started"])

    def test_malformed_headers_never_create_target_files_or_markers(self):
        self.begin()
        valid = copy.deepcopy(self.transfer._request)
        bad = {**valid, "manifest_bytes": True}
        malformed = transfers.encoded(bad)
        for raw in (b"", b"\0" * 4, struct.pack("!Q", transfers.MAX_METADATA + 1),
                    struct.pack("!Q", 1) + b"{", struct.pack("!Q", len(malformed)) + malformed):
            with self.subTest(raw=raw[:8]), self.assertRaises(ValueError):
                transfers.receive(self.store, self.received, io.BytesIO(raw))
        self.assertFalse(self.store.status()["image_transfer_started"])
        self.assertEqual(list(self.received.iterdir()), [])

    def test_remote_failure_missing_receipt_and_timeout_never_record_controller_success(self):
        self.begin()
        for fault in ("exit", "receipt", "timeout", "lost-receipt"):
            evidence = self.root / ("evidence-" + fault)
            evidence.mkdir(mode=0o700)
            def failing(argv, **kwargs):
                self.calls.append(argv)
                if fault == "timeout":
                    raise subprocess.TimeoutExpired(argv, 1800)
                if fault == "lost-receipt":
                    transfers.receive(self.store, self.received, kwargs["stdin"])
                kwargs["stdout"].write(b"{}")
                return subprocess.CompletedProcess(argv, 1 if fault in ("exit", "lost-receipt") else 0)
            with self.subTest(fault=fault), patch.object(transfers.subprocess, "run", side_effect=failing):
                with self.assertRaises(transfers.TransferError):
                    self.send(evidence=evidence)
                self.assertFalse((evidence / (self.operation + ".transfer.json")).exists())
                with self.assertRaises(transfers.TransferError):
                    self.send(evidence=evidence)
            self.assertEqual(len(self.calls), ("exit", "receipt", "timeout", "lost-receipt").index(fault) + 1)
        self.assertTrue(self.store.status()["image_transfer_verified"])
        self.assertTrue(self.store.status()["blocked"])

    def test_successful_receiver_process_exit_does_not_release_transfer_marker(self):
        self.begin()
        code = """
import json, os, sys
from pathlib import Path
from tool.deployment_state import StateStore
from tool.deployment_transfer import receive
result = receive(StateStore(Path(sys.argv[1]), owner_uid=os.getuid()), Path(sys.argv[2]), sys.stdin.buffer)
print(json.dumps(result))
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.store.root), str(self.received)],
                               cwd=ROOT, input=self.wire(), capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), transfers.receipt(self.transfer._request))
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_transfer_started"])
        with self.assertRaises(StateError):
            transfers.receive(self.store, self.received, io.BytesIO(self.wire()))

    def test_receiver_process_loss_after_marker_never_allows_automatic_resume(self):
        self.begin()
        code = """
import os, sys
from pathlib import Path
from tool.deployment_state import StateStore
store = StateStore(Path(sys.argv[1]), owner_uid=os.getuid())
pending = store.status()['deployments']['demo']['pending']
store.start_image_transfer('demo', pending['operation'], pending['candidate'], pending['intent_digest'])
os._exit(23)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.store.root)], cwd=ROOT, timeout=10)
        self.assertEqual(child.returncode, 23)
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse(self.store.status()["image_transfer_verified"])
        with self.assertRaises(StateError):
            transfers.receive(self.store, self.received, io.BytesIO(self.wire()))
        self.assertEqual(list(self.received.iterdir()), [])

    def test_receiver_cli_rejects_bad_stream_without_success_output(self):
        child = subprocess.run(
            [sys.executable, "-m", "tool.deployment_transfer", "receive", "--parent", str(self.received)],
            cwd=ROOT, input=b"invalid", capture_output=True, timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(child.stdout, b"")
        self.assertIn(b"blocked or uncertain", child.stderr)
        self.assertNotIn(b"Traceback", child.stderr)
        self.assertEqual(list(self.received.iterdir()), [])

    def test_existing_destination_or_unsafe_parent_never_overwrites(self):
        self.begin()
        unsafe = self.root / "shared-received"
        unsafe.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            transfers.receive(self.store, unsafe, io.BytesIO(self.wire()))
        linked = self.root / "linked-received"
        linked.symlink_to(self.received, target_is_directory=True)
        with self.assertRaises(OSError):
            transfers.receive(self.store, linked, io.BytesIO(self.wire()))
        self.assertFalse(self.store.status()["image_transfer_started"])
        self.assertEqual(list(self.received.iterdir()), [])
        destination = self.received / self.operation
        destination.mkdir(mode=0o700)
        sentinel = destination / "keep"
        sentinel.write_text("preserve")
        with self.assertRaises(FileExistsError):
            transfers.receive(self.store, self.received, io.BytesIO(self.wire()))
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertTrue(self.store.status()["image_transfer_started"])

    def test_source_replacement_during_successful_transport_blocks_controller_receipt(self):
        self.begin()
        def mutate(argv, **kwargs):
            result = self.fake_ssh(argv, **kwargs)
            archive = self.bundle / "web.tar"
            original = archive.read_bytes()
            archive.unlink()
            archive.write_bytes(original)
            archive.chmod(0o400)
            return result
        with patch.object(transfers.subprocess, "run", side_effect=mutate):
            with self.assertRaises(transfers.TransferError):
                self.send()
        self.assertTrue((self.received / self.operation / "transfer.json").is_file())
        self.assertFalse((self.evidence / (self.operation + ".transfer.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_receive_verification_persistence_failure_blocks_load_and_retry(self):
        self.begin()
        write = self.store._write
        def fail_completion(directory, name, value, **kwargs):
            if name == "image-transfer.json" and kwargs.get("replace"):
                raise OSError("synthetic transfer completion write failure")
            return write(directory, name, value, **kwargs)
        with patch.object(self.store, "_write", side_effect=fail_completion):
            with self.assertRaises(StateError):
                transfers.receive(self.store, self.received, io.BytesIO(self.wire()))
        self.assertTrue((self.received / self.operation / "transfer.json").is_file())
        self.assertFalse(self.store.status()["image_transfer_verified"])
        with self.assertRaises(StateError):
            transfers.receive(self.store, self.received, io.BytesIO(self.wire()))

    def test_receive_marker_persistence_failure_precedes_artifact_mutation(self):
        self.begin()
        write = self.store._write
        def fail_marker(directory, name, value, **kwargs):
            write(directory, name, value, **kwargs)
            if name == "image-transfer.json":
                raise OSError("synthetic marker sync uncertainty")
        with patch.object(self.store, "_write", side_effect=fail_marker):
            with self.assertRaises(StateError):
                transfers.receive(self.store, self.received, io.BytesIO(self.wire()))
        self.assertTrue(self.store.status()["image_transfer_started"])
        self.assertFalse(self.store.status()["image_transfer_verified"])
        self.assertEqual(list(self.received.iterdir()), [])


@unittest.skipUnless(os.environ.get("RUSSOUND_RUN_SSH_TRANSFER_TESTS") == "1",
                     "Opt in to isolated loopback sshd/SSH transfer")
class ActualSSHTransferTests(unittest.TestCase):
    def test_isolated_ssh_verifies_host_key_and_delivers_reserved_pair(self):
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        self.assertTrue(Path(sshd).is_file())
        with tempfile.TemporaryDirectory(prefix="russound-ssh-") as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            host_key, client_key = root / "hostkey", root / "clientkey"
            for key in (host_key, client_key):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            authorized = root / "authorized_keys"
            authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
            authorized.chmod(0o600)
            real_images = os.environ.get("RUSSOUND_RUN_LOCAL_BUILD_TESTS") == "1"
            platform = "linux/arm64"
            if real_images:
                docker = ["docker", "--context", builds.local_docker()]
                info = json.loads(builds.command(docker + ["info", "--format", "{{json .}}"]))
                architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(
                    info["Architecture"], info["Architecture"])
                platform = info["OSType"] + "/" + architecture
                source = root / "source"
                source.mkdir()
                for args in (["init", "--quiet"], ["config", "user.email", "fixture@example.invalid"],
                             ["config", "user.name", "Fixture"]):
                    builds.command(["git", "-C", str(source), *args])
                (source / "marker").write_text("ssh transfer fixture " + str(uuid4()))
                for name in ("Dockerfile", "Dockerfile.ser2net"):
                    (source / name).write_text("FROM scratch\nCOPY marker /marker\n")
                builds.command(["git", "-C", str(source), "add", "."])
                builds.command(["git", "-C", str(source), "commit", "--quiet", "-m", "Fixture"])
                revision = builds.command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
                artifacts = root / "builds"
                artifacts.mkdir(mode=0o700)
                bundle, checksum = builds.build(source, revision, artifacts, "managed", platform)
            else:
                bundle, checksum, _ = fixture_bundle(root, "managed", platform)
            selection = select_bundle(bundle, checksum, "managed", platform)
            if real_images:
                owned_tags = {image["tag"] for image in selection["manifest"]["images"].values()}
                def cleanup():
                    present = builds.command(docker + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
                    for tag in owned_tags & set(present.decode().splitlines()):
                        builds.command(docker + ["image", "rm", tag])
                self.addCleanup(cleanup)
                for tag in owned_tags:
                    builds.command(docker + ["image", "rm", tag])
            operation = str(uuid4())
            store = StateStore(root / "state", owner_uid=os.getuid())
            store.initialize()
            store.begin("fixture", operation, selection, INTENT, ["apply"])
            received = root / "received"
            received.mkdir(mode=0o700)
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            # The isolated server forces a fixture receiver with non-root test
            # state; production uses the fixed root-owned state directory.
            wrapper = root / "receiver.py"
            wrapper.write_text(
                "import json, os, sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from tool.deployment_state import StateStore\n"
                "from tool.deployment_transfer import receive\n"
                f"store=StateStore(Path({str(store.root)!r}), owner_uid=os.getuid())\n"
                f"print(json.dumps(receive(store, Path({str(received)!r}), sys.stdin.buffer)))\n")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            config = root / "sshd_config"
            config.write_text(
                f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
                f"PidFile {root / 'sshd.pid'}\nAuthorizedKeysFile {authorized}\n"
                "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
                "PubkeyAuthentication yes\nStrictModes yes\nAllowTcpForwarding no\n"
                "PermitTunnel no\nX11Forwarding no\nPermitTTY no\n"
                f"AllowUsers {getpass.getuser()}\n"
                f"ForceCommand {shlex.join([sys.executable, str(wrapper)])}\n")
            trust = root / "known_hosts"
            trust.write_text(f"[127.0.0.1]:{port} " + host_key.with_suffix(".pub").read_text())
            trust.chmod(0o600)
            with (root / "sshd.log").open("wb") as log:
                server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if server.poll() is not None:
                            self.fail((root / "sshd.log").read_text())
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail("Isolated sshd did not become ready")
                            time.sleep(0.05)
                    transfer = transfers.ImageTransfer(
                        bundle, checksum, "managed", platform, deployment="fixture",
                        operation=operation, intent_digest=INTENT, host="127.0.0.1",
                        user=getpass.getuser(), port=port, known_hosts=trust, identity_file=client_key,
                        remote_tools=ROOT, remote_parent=received)
                    result = transfer.send(transfer.review()["digest"], evidence)
                    self.assertEqual(result["manifest_sha256"], checksum)
                    self.assertEqual(select_bundle(received / operation, checksum, "managed", platform),
                                     selection)
                    self.assertTrue(store.status()["image_transfer_started"])
                    self.assertFalse(store.status()["image_load_started"])
                    # A different valid host key must fail SSH, not touch target
                    # operation state or create another incoming directory.
                    wrong_trust = root / "wrong_hosts"
                    wrong_trust.write_text(f"[127.0.0.1]:{port} " + client_key.with_suffix(".pub").read_text())
                    wrong_trust.chmod(0o600)
                    wrong = transfers.ImageTransfer(
                        bundle, checksum, "managed", platform, deployment="fixture",
                        operation=str(uuid4()), intent_digest=INTENT, host="127.0.0.1",
                        user=getpass.getuser(), port=port, known_hosts=wrong_trust, identity_file=client_key,
                        remote_tools=ROOT, remote_parent=received)
                    with self.assertRaises(transfers.TransferError):
                        wrong.send(wrong.review()["digest"], evidence)
                    self.assertEqual([path.name for path in received.iterdir()], [operation])
                    if real_images:
                        store.advance("fixture", operation, "inputs-verified")
                        store.advance("fixture", operation, "backup-complete")
                        loaded = load_reserved_bundle(store, "fixture", operation, INTENT,
                                                      selection, received / operation, evidence)
                        self.assertEqual(set(loaded), {"web", "ser2net"})
                        self.assertTrue(store.status()["image_transfer_started"])
                        self.assertTrue(store.status()["image_load_started"])
                        for name, reference in loaded.items():
                            actual = json.loads(builds.command(
                                docker + ["image", "inspect", selection["manifest"]["images"][name]["tag"]]))[0]
                            builds.image_identity(actual, reference, platform, revision)
                        self.assertEqual(store.status()["deployments"]["fixture"]["pending"]["phase"],
                                         "backup-complete")
                finally:
                    server.terminate()
                    server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

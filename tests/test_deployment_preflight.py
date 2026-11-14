import errno
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tool import deployment_preflight as check


class TargetPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-preflight-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "deployment"
        self.data.mkdir(mode=0o700)
        for name in ("config", "data", "logs", "secrets", "dummy-data"):
            (self.data / name).mkdir(mode=0o700)
        self.request = {
            "gateway": "external", "project": "test-preflight", "platform": "linux/arm64",
            "data_dir": str(self.data), "uid": os.getuid(), "gid": os.getgid(),
            "publish_host": "127.0.0.1", "publish_port": 8000,
            "serial_device": "/dev/ttyUSB0", "serial_gid": None,
        }

    def docker(self, arguments):
        if arguments == ["context", "inspect"]:
            return [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
        if arguments[0] == "info":
            return {"Architecture": "aarch64", "OSType": "linux", "SecurityOptions": ["name=seccomp"]}
        if arguments[0] == "compose":
            return {"version": "v5.6.1"}
        if arguments[:2] == ["container", "ls"]:
            return []
        self.fail(f"Unexpected Docker inspection: {arguments}")

    def run_preflight(self):
        def xattr(path, name):
            raise OSError(errno.ENODATA, "no ACL")

        with patch.object(check.sys, "platform", "linux"), patch.dict(os.environ, {}, clear=True):
            with patch.object(check, "docker_json", side_effect=self.docker):
                with patch.object(check.os, "getxattr", side_effect=xattr, create=True):
                    with patch.object(check, "check_port"):
                        return check.preflight(self.request)

    def test_external_and_dummy_are_readonly_and_do_not_claim_routing_or_apply_safety(self):
        before = {path.relative_to(self.data): path.stat().st_mtime_ns for path in self.data.rglob("*")}
        for gateway in ("external", "dummy"):
            self.request["gateway"] = gateway
            result = self.run_preflight()
            self.assertFalse(result["safe_to_apply"])
            self.assertEqual(result["container_routing"], "not-tested")
            self.assertEqual(result["data"]["runtime_write_access"], "not-tested")
            self.assertEqual(result["serial"], "not-managed")
            self.assertEqual(result["project"], "absent")
            self.assertGreater(result["data"]["free_bytes"], 0)
        after = {path.relative_to(self.data): path.stat().st_mtime_ns for path in self.data.rglob("*")}
        self.assertEqual(before, after)

    def test_invalid_requests_fail_before_docker_or_target_inspection(self):
        for key, value in (("gateway", "unknown"), ("platform", "linux/arm/v7"),
                           ("data_dir", "/"), ("data_dir", "/tmp/../data"),
                           ("uid", True), ("gid", -1), ("publish_port", 0),
                           ("publish_port", 65536), ("publish_host", "gateway.example"),
                           ("publish_host", "127.0.0.1\n"), ("project", "../other")):
            with self.subTest(key=key, value=value):
                with patch.object(check, "docker_json") as docker:
                    with self.assertRaises(check.PreflightError):
                        check.preflight({**self.request, key: value})
                    docker.assert_not_called()

    def test_bind_directory_missing_symlink_unsafe_permissions_and_service_access_fail(self):
        path = self.data / "logs"
        path.rmdir()
        with self.assertRaises(OSError):
            self.run_preflight()
        path.symlink_to(self.data / "data", target_is_directory=True)
        with self.assertRaises(check.PreflightError):
            self.run_preflight()
        path.unlink()
        path.mkdir(mode=0o777)
        path.chmod(0o777)
        with self.assertRaises(check.PreflightError):
            self.run_preflight()
        path.chmod(0o700)
        self.request["uid"] = os.getuid() + 1
        with self.assertRaises(check.PreflightError):
            self.run_preflight()

    def test_secrets_and_acls_require_explicit_private_access(self):
        path = self.data / "secrets"
        path.chmod(0o755)
        with self.assertRaisesRegex(check.PreflightError, "Secrets"):
            self.run_preflight()
        path.chmod(0o700)
        with patch.object(check.os, "getxattr", return_value=b"acl", create=True):
            with self.assertRaisesRegex(check.PreflightError, "ACL"):
                check.check_data(self.request)

    def test_existing_project_mount_and_serial_ownership_require_adoption(self):
        for container in (
            {"Config": {"Labels": {"com.docker.compose.project": self.request["project"]}}},
            {"Mounts": [{"Type": "bind", "Source": str(self.data)}]},
            {"Mounts": [{"Type": "bind", "Source": str(self.data / "config")}]},
            {"Mounts": [{"Type": "bind", "Source": str(self.data.parent)}]},
        ):
            with self.subTest(container=container), self.assertRaises(check.PreflightError):
                check.check_containers(self.request, [container])
        check.check_containers(self.request, [{"Mounts": [{"Type": "bind", "Source": "/unrelated"}]}])
        self.request["gateway"] = "managed"
        with self.assertRaisesRegex(check.PreflightError, "Serial adapter"):
            check.check_containers(self.request, [{"HostConfig": {
                "Devices": [{"PathOnHost": "/dev/ttyUSB0"}],
            }}])

    def test_occupied_port_fails_without_stopping_or_reusing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(check.PreflightError, "listener is unavailable"):
                check.check_port("127.0.0.1", port)
            self.assertGreater(listener.fileno(), -1)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        check.check_port("127.0.0.1", port)

    def test_remote_daemon_wrong_platform_rootless_and_bad_metadata_fail(self):
        for fault in ("remote", "platform", "rootless", "invalid"):
            def docker(arguments):
                result = self.docker(arguments)
                if fault == "remote" and arguments[0] == "context":
                    result[0]["Endpoints"]["docker"]["Host"] = "ssh://other"
                elif fault == "platform" and arguments[0] == "info":
                    result["Architecture"] = "amd64"
                elif fault == "rootless" and arguments[0] == "info":
                    result["SecurityOptions"] = ["name=rootless"]
                elif fault == "invalid" and arguments[:2] == ["container", "ls"]:
                    result = ["--unsafe"]
                return result
            with self.subTest(fault=fault), patch.object(check.sys, "platform", "linux"):
                with patch.dict(os.environ, {}, clear=True), patch.object(check, "docker_json", side_effect=docker):
                    with self.assertRaises(check.PreflightError):
                        check.preflight(self.request)
        with patch.object(check.sys, "platform", "linux"):
            with patch.dict(os.environ, {"DOCKER_HOST": "ssh://other"}):
                with self.assertRaisesRegex(check.PreflightError, "endpoints"):
                    check.preflight(self.request)

    def test_serial_requires_root_character_device_permissions_and_no_holders(self):
        self.request.update(gateway="managed", serial_gid=os.getgid())
        with patch.object(check.os, "geteuid", return_value=1234):
            with self.assertRaisesRegex(check.PreflightError, "requires root"):
                check.check_serial(self.request)
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(),
                               st_gid=os.getgid(), st_rdev=12345)
        with patch.object(check.os, "geteuid", return_value=0):
            with patch.object(check, "safe_path", return_value=info):
                with self.assertRaisesRegex(check.PreflightError, "character"):
                    check.check_serial(self.request)
                info.st_mode = stat.S_IFCHR | 0o600
                with patch.object(check.Path, "iterdir", return_value=iter([])):
                    check.check_serial(self.request)
                process_path = self.root / "123"
                with patch.object(check.Path, "iterdir", side_effect=[[process_path], [self.root / "fd"]]):
                    with patch.object(check.Path, "stat", return_value=info):
                        with self.assertRaisesRegex(check.PreflightError, "existing process"):
                            check.check_serial(self.request)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "root Linux serial inspection")
    def test_real_open_pseudoterminal_with_group_write_is_detected_without_hardware(self):
        import pty

        master, slave = pty.openpty()
        try:
            path = Path(os.ttyname(slave))
            info = path.stat()
            check.safe_path(path, device=True)
            self.request.update(gateway="managed", serial_device=str(path), serial_gid=info.st_gid)
            with self.assertRaisesRegex(check.PreflightError, "existing process"):
                check.check_serial(self.request)
        finally:
            os.close(slave)
            os.close(master)

    def test_docker_json_handles_actual_json_lines_not_an_assumed_array(self):
        identifiers = ['a' * 64, 'b' * 64]
        result = SimpleNamespace(returncode=0, stdout=b"".join(json.dumps(item).encode() + b"\n"
                                                            for item in identifiers))
        with patch.object(check.subprocess, "run", return_value=result) as run:
            self.assertEqual(check.docker_json(["container", "ls", "--format", "{{json .ID}}"]), identifiers)
            self.assertEqual(run.call_args.args[0][:3], ["docker", "container", "ls"])
        for failure in (OSError("sensitive"), subprocess.TimeoutExpired("sensitive", 30)):
            with patch.object(check.subprocess, "run", side_effect=failure):
                with self.assertRaises(check.PreflightError) as caught:
                    check.docker_json(["info"])
                self.assertNotIn("sensitive", str(caught.exception))

    def test_cli_redacts_unexpected_private_details_and_reports_failure(self):
        with patch.object(check.sys, "stdin", SimpleNamespace(buffer=BytesIO(json.dumps(self.request).encode()))):
            with patch.object(check.sys, "stderr", new_callable=StringIO) as errors:
                with patch.object(check, "preflight", side_effect=KeyError("private-path-secret")):
                    self.assertEqual(check.main(), 1)
                self.assertNotIn("private-path-secret", errors.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "standalone probe needs Linux")
    def test_standalone_linux_cli_with_fixture_docker_and_real_paths_and_listener(self):
        binary = self.root / "docker"
        binary.write_text(
            "#!" + sys.executable + "\n"
            "import json,sys\n"
            "args=sys.argv[1:]\n"
            "if args[0]=='context': print(json.dumps([{'Endpoints':{'docker':{'Host':'unix:///fixture'}}}]))\n"
            "elif args[0]=='info': print(json.dumps({'Architecture':'aarch64','OSType':'linux'}))\n"
            "elif args[0]=='compose': print(json.dumps({'version':'v5.6.1'}))\n"
            "elif args[:2]==['container','ls']: pass\n"
            "else: raise SystemExit(1)\n"
        )
        binary.chmod(0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.request["publish_port"] = listener.getsockname()[1]
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
        environment["PATH"] = str(self.root) + os.pathsep + os.environ.get("PATH", "")
        before = set(self.data.rglob("*"))
        result = subprocess.run([sys.executable, "-c", Path(check.__file__).read_text()],
                                input=json.dumps(self.request), capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["safe_to_apply"])
        self.assertEqual(set(self.data.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()

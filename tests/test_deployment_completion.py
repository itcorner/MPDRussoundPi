import copy
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import yaml

from tool.deployment_completion import read_events, run_checked, validate_events
from tool.deployment_state import PHASES, StateError, StateStore
from tool.deployment_images import resolve
from test_deployment_images import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"


class CompletionValidationTests(unittest.TestCase):
    def setUp(self):
        self.operation = str(uuid4())
        self.events = []
        for kind, fields in (
            ("start", {}),
            ("task-start", {"actual_host": "fixture", "task": "apply", "unsupported": False}),
            ("passed", {"actual_host": "fixture", "task": "apply", "unsupported": False}),
            ("end", {"recording_failed": False, "hosts": ["fixture"],
                     "totals": dict.fromkeys(("failures", "unreachable", "rescued", "ignored"), 0)}),
        ):
            self.events.append({"schema_version": 1, "sequence": len(self.events),
                                "operation": self.operation, "host": "fixture", "event": kind, **fields})

    def validate(self, events=None, returncode=0):
        return validate_events(self.events if events is None else events, operation=self.operation,
                               host="fixture", required=["apply"], returncode=returncode)

    def test_exact_complete_stream_passes(self):
        self.assertEqual(self.validate(), {"apply": "passed"})

    def test_missing_duplicate_truncated_and_contradictory_outcomes_fail(self):
        variants = [[], self.events[:-1], self.events[1:], self.events[:1] + self.events[2:],
                    self.events[:-1] + self.events[2:],
                    self.events[:2] + [{**self.events[2], "event": "failed"}] + self.events[2:]]
        for events in variants:
            with self.subTest(events=events), self.assertRaises(StateError):
                self.validate(events)
        duplicate = [copy.deepcopy(event) for event in self.events[:-1] + self.events[2:]]
        for sequence, event in enumerate(duplicate):
            event["sequence"] = sequence
        with self.assertRaisesRegex(StateError, "duplicate required task terminal"):
            self.validate(duplicate)
        with self.assertRaises(StateError):
            self.validate(returncode=1)
        for index, overrides in (
            (0, {"operation": str(uuid4())}), (1, {"host": "other"}),
            (1, {"actual_host": "other"}), (1, {"unsupported": True}),
            (2, {"event": "skipped"}), (2, {"task": "unexpected"}),
            (3, {"recording_failed": True}), (3, {"hosts": []}),
            (3, {"totals": {"failures": 0}}),
            (3, {"totals": dict.fromkeys(("failures", "unreachable", "rescued", "ignored"), False)}),
        ):
            events = copy.deepcopy(self.events)
            events[index].update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(StateError):
                self.validate(events)

    def test_private_regular_complete_file_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("".join(json.dumps(value) + "\n" for value in self.events))
            path.chmod(0o600)
            self.assertEqual(read_events(path), self.events)
            path.chmod(0o644)
            with self.assertRaises(StateError):
                read_events(path)
            path.chmod(0o600)
            path.write_text(json.dumps(self.events[0]))
            with self.assertRaises(StateError):
                read_events(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(StateError):
                read_events(link)

    def test_callback_write_failure_is_sticky_and_never_logs_result_values(self):
        spec = importlib.util.spec_from_file_location(
            "completion_callback_fixture", ROOT / "ansible/callback_plugins/deployment_completion.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            env = {"RUSSOUND_COMPLETION_OPERATION": self.operation,
                   "RUSSOUND_COMPLETION_HOST": "fixture", "RUSSOUND_COMPLETION_STREAM": str(path)}
            with patch.dict(os.environ, env):
                callback = module.CallbackModule()
            callback.stream.close()
            with patch.object(callback._display, "warning") as warning:
                callback.emit("task-start", task="apply")
                self.assertTrue(callback.failed)
                self.assertTrue(warning.called)
            callback.stream = StringIO()
            with patch("os.fsync"), patch.object(callback._display, "warning"):
                callback.emit("end", recording_failed=callback.failed)
            terminal = json.loads(callback.stream.getvalue())
            self.assertTrue(terminal["recording_failed"])

    def test_terminal_fsync_failure_with_complete_bytes_sends_failure_acknowledgement(self):
        spec = importlib.util.spec_from_file_location(
            "completion_callback_fixture", ROOT / "ansible/callback_plugins/deployment_completion.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reader, writer = os.pipe()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "events.jsonl"
                env = {"RUSSOUND_COMPLETION_OPERATION": self.operation,
                       "RUSSOUND_COMPLETION_HOST": "fixture",
                       "RUSSOUND_COMPLETION_STREAM": str(path),
                       "RUSSOUND_COMPLETION_FD": str(writer)}
                with patch.dict(os.environ, env):
                    callback = module.CallbackModule()
                    with patch("os.fsync", side_effect=OSError("test final fsync failure")):
                        with patch.object(callback._display, "warning"):
                            callback.v2_playbook_on_stats(SimpleNamespace(processed={}))
                self.assertEqual(os.read(reader, 1), b"0")
                terminal = json.loads(path.read_text().splitlines()[-1])
                self.assertFalse(terminal["recording_failed"])
                self.assertTrue(callback.failed)
                callback.stream.close()
        finally:
            os.close(reader)
            os.close(writer)

    def test_runner_requires_success_acknowledgement_even_with_valid_file_and_zero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            evidence = root / "events.jsonl"

            acknowledgement = b""

            def no_ack(argv, **kwargs):
                evidence.write_text("".join(json.dumps(value) + "\n" for value in self.events))
                evidence.chmod(0o600)
                if acknowledgement:
                    os.write(int(kwargs["env"]["RUSSOUND_COMPLETION_FD"]), acknowledgement)
                return subprocess.CompletedProcess(argv, 0)

            for acknowledgement in (b"", b"0", b"11"):
                with self.subTest(acknowledgement=acknowledgement):
                    if evidence.exists():
                        evidence.unlink()
                    with patch("tool.deployment_completion.subprocess.run", side_effect=no_ack):
                        with self.assertRaisesRegex(StateError, "did not acknowledge"):
                            run_checked(ansible=ANSIBLE, playbook=root / "play.yml", inventory=root / "inventory",
                                        evidence=evidence, operation=self.operation, host="fixture", required=["apply"])


@unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
class ActualAnsibleCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-completion-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.operation = str(uuid4())
        self.inventory = self.root / "inventory.yml"
        self.inventory.write_text(yaml.safe_dump({"all": {"hosts": {
            "fixture": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                        "ansible_async_dir": str(self.root / "async")},
        }}}))
        self.path = self.root / "events.jsonl"

    def invoke(self, tasks, *, handlers=None, required=None):
        playbook = self.root / "play.yml"
        play = {"hosts": "all", "gather_facts": False, "tasks": tasks}
        if handlers is not None:
            play["handlers"] = handlers
        playbook.write_text(yaml.safe_dump([play]))
        return run_checked(ansible=ANSIBLE, playbook=playbook, inventory=self.inventory,
                           evidence=self.path, operation=self.operation, host="fixture",
                           required=required or ["apply"])

    def test_simple_local_play_has_complete_private_nonsecret_evidence(self):
        self.assertEqual(self.invoke([{
            "name": "sensitive-task-name-must-not-be-recorded",
            "ansible.builtin.debug": {"msg": "secret-value-must-not-be-recorded"},
            "tags": ["deployment_required_apply"],
        }]), {"apply": "passed"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        content = self.path.read_text()
        self.assertNotIn("sensitive-task-name", content)
        self.assertNotIn("secret-value", content)
        self.assertEqual(read_events(self.path)[-1]["event"], "end")

    def test_failed_ignored_and_rescued_tasks_never_become_success(self):
        for mode in ("failed", "ignored", "rescued"):
            with self.subTest(mode=mode):
                if self.path.exists():
                    self.path.unlink()
                fail = {"ansible.builtin.fail": {"msg": "secret-failure"},
                        "tags": ["deployment_required_apply"]}
                tasks = [fail]
                if mode == "ignored":
                    fail["ignore_errors"] = True
                elif mode == "rescued":
                    tasks = [{"block": [fail], "rescue": [{"ansible.builtin.debug": {"msg": "rescued"}}]}]
                with self.assertRaises(StateError):
                    self.invoke(tasks)
                self.assertNotIn("secret-failure", self.path.read_text())

    def test_retry_loop_condition_and_async_patterns_are_rejected(self):
        for pattern in (
            {"until": "true", "retries": 1},
            {"loop": ["secret-item-one", "secret-item-two"]},
            {"when": "false"},
            {"async": 10, "poll": 1},
        ):
            with self.subTest(pattern=pattern):
                if self.path.exists():
                    self.path.unlink()
                task = {"ansible.builtin.command": {"argv": ["/usr/bin/true"]},
                        "tags": ["deployment_required_apply"], **pattern}
                with self.assertRaises(StateError):
                    self.invoke([task])
                self.assertNotIn("secret-item", self.path.read_text())

    def test_missing_required_handler_is_not_success(self):
        tasks = [{"ansible.builtin.debug": {"msg": "unchanged"},
                  "tags": ["deployment_required_apply"]}]
        with self.assertRaisesRegex(StateError, "Missing required task"):
            self.invoke(tasks, handlers=[{
                "name": "reload-fixture", "ansible.builtin.debug": {"msg": "handler"},
                "tags": ["deployment_required_reload"],
            }], required=["apply", "reload"])

    def test_notified_handler_must_finish_before_receipt(self):
        result = self.invoke([{
            "ansible.builtin.debug": {"msg": "changed"}, "changed_when": True,
            "notify": "reload-fixture", "tags": ["deployment_required_apply"],
        }], handlers=[{
            "name": "reload-fixture", "ansible.builtin.debug": {"msg": "handler"},
            "tags": ["deployment_required_reload"],
        }], required=["apply", "reload"])
        self.assertEqual(result, {"apply": "passed", "reload": "passed"})
        events = read_events(self.path)
        self.assertEqual(events[-2]["task"], "reload")
        self.assertEqual(events[-1]["event"], "end")

    def test_unreachable_host_never_produces_receipt(self):
        self.inventory.write_text(yaml.safe_dump({"all": {"hosts": {"fixture": {
            "ansible_connection": "ssh", "ansible_host": "127.0.0.1", "ansible_port": 1,
            "ansible_ssh_common_args": "-o ConnectTimeout=1 -o BatchMode=yes",
        }}}}))
        with self.assertRaises(StateError):
            self.invoke([{"ansible.builtin.command": {"argv": ["/usr/bin/true"]},
                          "tags": ["deployment_required_apply"]}])

    def test_failed_handler_blocks_receipt_after_successful_apply(self):
        with self.assertRaises(StateError):
            self.invoke([{
                "ansible.builtin.debug": {"msg": "changed"}, "changed_when": True,
                "notify": "reload-fixture", "tags": ["deployment_required_apply"],
            }], handlers=[{
                "name": "reload-fixture", "ansible.builtin.fail": {"msg": "handler-failure"},
                "tags": ["deployment_required_reload"],
            }], required=["apply", "reload"])
        events = read_events(self.path)
        self.assertTrue(any(event.get("event") == "passed" and event.get("task") == "apply"
                            for event in events))
        self.assertTrue(any(event.get("event") == "failed" and event.get("task") == "reload"
                            for event in events))

    def test_real_completion_can_be_consumed_by_separate_state_finalization(self):
        store = StateStore(self.root / "state", owner_uid=os.getuid())
        store.initialize()
        intent = "sha256:" + "d" * 64
        selection = resolve("external", "linux/arm64", FixtureRegistry())
        store.begin("demo", self.operation, selection, intent, ["apply"])
        for phase in PHASES[1:3]:
            store.advance("demo", self.operation, phase)
        result = self.invoke([{"ansible.builtin.debug": {"msg": "fixture-only"},
                               "tags": ["deployment_required_apply"]}])
        self.assertTrue(store.status()["blocked"])
        store.advance("demo", self.operation, "apply-complete")
        store.advance("demo", self.operation, "checks-complete")
        store.finalize("demo", self.operation, {
            "operation": self.operation, "intent_digest": intent,
            "invocation": "verified-complete", "required": result,
        })
        self.assertFalse(store.status()["blocked"])
        self.assertEqual(store.status()["deployments"]["demo"]["committed"]["selection"]["candidate"],
                         selection)

    def test_callback_creation_failure_does_not_allow_zero_exit_success(self):
        self.path.mkdir(mode=0o700)
        # A fresh-looking path that is occupied just as the callback is loaded.
        with patch("tool.deployment_completion.Path.exists", return_value=False):
            with self.assertRaises(StateError):
                self.invoke([{"ansible.builtin.debug": {"msg": "ok"},
                              "tags": ["deployment_required_apply"]}])


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from tool import deployment_images
from test_deployment_images import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"
WEB = "itcornernet/mpdrussoundpi-web@sha256:" + "a" * 64
GATEWAY = "itcornernet/mpdrussoundpi-ser2net@sha256:" + "b" * 64
DUMMY = "sha256:" + "c" * 64


def compose_environment():
    return {
        **{key: value for key, value in os.environ.items()
           if not key.startswith(("RUSSOUND_", "COMPOSE_"))},
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "RUSSOUND_WEB_IMAGE": WEB,
        "RUSSOUND_PLATFORM": "linux/arm64",
        "RUSSOUND_DATA_DIR": "/tmp/russound-profile-test",
    }


@unittest.skipUnless(DOCKER, "Docker Compose is required")
class ContainerProfileTests(unittest.TestCase):
    def render(self, profile="dev", overlay=None, **variables):
        argv = [DOCKER, "compose", "--env-file", "/dev/null", "-p", "profile-test",
                "-f", str(ROOT / f"compose.{profile}.yml")]
        if overlay:
            argv += ["-f", str(ROOT / f"compose.{overlay}.yml")]
        result = subprocess.run(argv + ["config", "--format", "json"], check=True,
                                capture_output=True, text=True,
                                env={**compose_environment(), **variables})
        return json.loads(result.stdout)

    def test_external_web_needs_no_gateway_image_device_or_build(self):
        for profile in ("dev", "prod"):
            with self.subTest(profile=profile):
                config = self.render(profile)
                self.assertEqual(set(config["services"]), {"russound-web"})
                web = config["services"]["russound-web"]
                self.assertEqual(web["image"], WEB)
                self.assertNotIn("build", web)
                self.assertNotIn("depends_on", web)
                self.assertNotIn("container_name", web)
                self.assertEqual(web["environment"]["RUSSOUND_DEBUG"], str(profile == "dev").lower())
                self.assertEqual(web["environment"]["RUSSOUND_PLAYBACK_AGENTS"], "")
                self.assertEqual(web["logging"]["options"], {"max-size": "10m", "max-file": "3"})
                self.assertEqual(web["stop_grace_period"], "30s")
                self.assertEqual({mount["target"] for mount in web["volumes"]},
                                 {"/config", "/data", "/logs", "/run/secrets"})
                for mount in web["volumes"]:
                    self.assertFalse(mount["bind"].get("create_host_path", False))
                    self.assertTrue(mount["source"].startswith("/tmp/russound-profile-test/"))

    def test_managed_gateway_is_internal_and_scopes_device_access(self):
        config = self.render(overlay="gateway", RUSSOUND_SER2NET_IMAGE=GATEWAY, RUSSOUND_SERIAL_GID="20")
        self.assertEqual(set(config["services"]), {"russound-web", "russound-ser2net"})
        gateway = config["services"]["russound-ser2net"]
        self.assertEqual(gateway["image"], GATEWAY)
        self.assertNotIn("ports", gateway)
        self.assertNotIn("build", gateway)
        self.assertNotIn("privileged", gateway)
        self.assertNotIn("devices", config["services"]["russound-web"])
        self.assertEqual(config["services"]["russound-web"]["environment"]["RUSSOUND_BACKEND_HOST"],
                         "russound-ser2net")

    def test_dummy_preserves_web_mounts_and_does_not_require_hardware(self):
        config = self.render(overlay="dummy", RUSSOUND_DUMMY_IMAGE=DUMMY)
        web = config["services"]["russound-web"]
        self.assertEqual(web["volumes"], self.render()["services"]["russound-web"]["volumes"])
        self.assertEqual(web["environment"]["RUSSOUND_BACKEND_HOST"], "russound-dummy")
        dummy = config["services"]["russound-dummy"]
        self.assertNotIn("ports", dummy)
        self.assertNotIn("devices", dummy)
        self.assertNotIn("build", dummy)
        self.assertEqual(dummy["environment"]["RUSSOUND_DUMMY_TUI"], "false")

    def test_selected_missing_images_fail_instead_of_building(self):
        for overlay in ("gateway", "dummy"):
            with self.subTest(overlay=overlay):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.render(overlay=overlay)

    @unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
    def test_preview_respects_inventory_and_never_uses_target_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.yml"
            variables = {
                "ansible_connection": "ssh", "ansible_host": "invalid.example",
                "russound_gateway": "external", "russound_backend_host": "gateway.example",
                "russound_data_dir": "/tmp/preview-only", "russound_web_image": WEB,
                "russound_publish_port": 8765, "russound_project": "inventory-choice",
            }
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"preview": variables}}}))
            argv = [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev.yml")]
            env = {**compose_environment(), "RUSSOUND_WEB_IMAGE": "ambient-wrong:latest",
                   "RUSSOUND_PLAYBACK_AGENTS": "not-json"}
            result = subprocess.run(argv, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("inventory-choice", result.stdout)
            self.assertIn("changed=0", result.stdout)
            self.assertIn("blocked-until-safety-runner", result.stdout)
            self.assertNotIn("ambient-wrong", result.stdout)
            rejected = subprocess.run(argv + ["-e", "russound_action=apply"],
                                      capture_output=True, text=True, env=env)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Only russound_action=preview", rejected.stdout)
            for overrides in (
                {"russound_web_image": "itcornernet/mpdrussoundpi-web:dev"},
                {"russound_backend_host": ""},
                {"russound_ser2net_image": GATEWAY},
                {"russound_publish_port": True},
                {"russound_publish_port": 65536},
                {"russound_playback_agents": "[]"},
                {"russound_data_dir": "relative"},
                {"russound_use_published_images": True},
            ):
                with self.subTest(overrides=overrides):
                    result = subprocess.run(argv + ["-e", json.dumps(overrides)],
                                            capture_output=True, text=True, env=env)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
            for mode, overrides in (
                ("managed", {"russound_ser2net_image": GATEWAY, "russound_serial_gid": 20}),
                ("dummy", {"russound_dummy_image": DUMMY}),
            ):
                result = subprocess.run(argv + ["-e", json.dumps({
                    "russound_gateway": mode, "russound_backend_host": "", **overrides,
                })], capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("changed=0", result.stdout)

    @unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
    def test_preview_reuses_candidate_offline_and_rejects_conflicting_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.yml"
            variables = {
                "ansible_connection": "ssh", "ansible_host": "invalid.example",
                "russound_data_dir": "/tmp/candidate-preview",
            }
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"preview": variables}}}))
            argv = [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev.yml")]
            for mode in ("external", "managed", "dummy"):
                with self.subTest(mode=mode):
                    candidate = deployment_images.resolve(mode, "linux/arm64", FixtureRegistry())
                    path = Path(directory) / f"{mode}.json"
                    deployment_images.save_selection(path, candidate)
                    original = path.read_bytes()
                    overrides = {
                        "russound_gateway": mode, "russound_image_selection_file": str(path),
                        **({"russound_backend_host": "gateway.example"} if mode == "external" else
                           {"russound_serial_gid": 20} if mode == "managed" else
                           {"russound_dummy_image": DUMMY}),
                    }
                    result = subprocess.run(argv + ["--check", "-e", json.dumps(overrides)],
                                            capture_output=True, text=True, env=compose_environment())
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("candidate-schema-only-offline", result.stdout)
                    self.assertIn(candidate["images"]["web"]["reference"], result.stdout)
                    if mode == "managed":
                        self.assertIn(candidate["images"]["ser2net"]["reference"], result.stdout)
                    self.assertIn("changed=0", result.stdout)
                    self.assertEqual(path.read_bytes(), original)
                    for invalid in (
                        {"russound_web_image": WEB},
                        {"russound_platform": "linux/amd64"},
                        {"russound_image_selection_file": str(path) + ".missing"},
                    ):
                        rejected = subprocess.run(argv + ["-e", json.dumps({**overrides, **invalid})],
                                                  capture_output=True, text=True, env=compose_environment())
                        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
                        self.assertNotIn("Show the non-mutating development preview", rejected.stdout)
                        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
    def test_explicit_target_preflight_executes_over_local_ansible_and_surfaces_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.yml"
            variables = {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "russound_action": "preflight",
                "russound_gateway": "external", "russound_backend_host": "gateway.example",
                "russound_data_dir": str(Path(directory).resolve() / "not-provisioned"),
                "russound_web_image": WEB,
            }
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"preflight": variables}}}))
            result = subprocess.run(
                [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev.yml")],
                capture_output=True, text=True, env=compose_environment(),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Require a successful first-install target preflight", result.stdout)
            self.assertIn("Target preflight failed:", result.stdout)
            self.assertFalse(Path(variables["russound_data_dir"]).exists())
            self.assertNotIn("Traceback", result.stdout + result.stderr)

    @unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
    def test_preflight_validates_controller_inputs_before_target_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.yml"
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"unreachable": {
                "ansible_connection": "ssh", "ansible_host": "invalid.example",
                "russound_action": "preflight", "russound_gateway": "external",
                "russound_backend_host": "gateway.example", "russound_data_dir": "relative",
                "russound_web_image": WEB,
            }}}}))
            result = subprocess.run(
                [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev.yml")],
                capture_output=True, text=True, env=compose_environment(),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Validate layout and image selection inputs", result.stdout)
            self.assertNotIn("Inspect first-install prerequisites", result.stdout)
            self.assertNotIn("UNREACHABLE", result.stdout)

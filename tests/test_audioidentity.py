import copy
import importlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from typing import cast
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    identity = importlib.import_module("lib.audioidentity")
    ready = importlib.import_module("lib.audioready")


class GraphFixture:
    def setUp(self):
        self.selector = identity.HardwareSelector.parse({"vendor": "0d8c", "product": "0102"})
        self.card: dict[str, object] = {
            "device.api": "alsa", "device.bus": "usb",
            "device.vendor.id": "0x0d8c", "device.product.id": "0x0102",
            "device.name": "alsa_card.usb-0d8c_USB_Sound_Device-00.3",
        }
        self.graph: list[dict[str, object]] = []
        self.add(10, "Device", self.card)
        self.add(20, "Node", {
            "device.id": 10, "media.class": "Audio/Sink", "audio.channels": 6,
            "device.profile.name": "analog-surround-51", "api.alsa.pcm.stream": "playback",
        })
        for node, name, channels in (
            (30, "stereo_front", ("FL", "FR")),
            (40, "stereo_centerlfe", ("FC", "LFE")),
            (50, "stereo_rear", ("RL", "RR")),
        ):
            self.add(node, "Node", {"node.name": f"playback.{name}"})
            for index, channel in enumerate(channels):
                self.add(node * 10 + index, "Port", {
                    "node.id": node, "audio.channel": channel, "port.direction": "out",
                })
                self.add(node * 10 + index + 2, "Port", {
                    "node.id": 20, "audio.channel": channel, "port.direction": "in",
                })
                self.graph.append({
                    "id": node * 10 + index + 4, "type": "PipeWire:Interface:Link",
                    "info": {"output-node-id": node, "input-node-id": 20,
                             "output-port-id": node * 10 + index,
                             "input-port-id": node * 10 + index + 2, "state": "active"},
                })
        self.mappings = {"stereo_front": ("FL", "FR"), "stereo_centerlfe": ("FC", "LFE"),
                         "stereo_rear": ("RL", "RR")}

    def add(self, number: int, kind: str, props: dict[str, object]) -> None:
        self.graph.append({"id": number, "type": f"PipeWire:Interface:{kind}", "info": {"props": props}})

    def details(self, item: dict[str, object]) -> dict[str, object]:
        value = item["info"]
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def properties(self, item: dict[str, object]) -> dict[str, object]:
        value = self.details(item)["props"]
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def validate(self):
        return identity.validate_downstream(self.graph, self.selector, self.mappings)


class AudioIdentityTests(GraphFixture, unittest.TestCase):
    def test_hex_formats_and_invalid_selector(self):
        for value in ("0d8c", "0x0D8C", 3468):
            self.assertEqual(identity.usb_id(value), 3468)
        for value in (True, None, -1, 65536, "garbage", "0x", "12345"):
            with self.assertRaises(ValueError):
                identity.usb_id(value)
        with self.assertRaises(ValueError):
            identity.HardwareSelector.parse({"vendor": "0d8c", "product": "0102", "port": "1"})

    def test_suffix_index_and_port_do_not_define_identity(self):
        for suffix in ("", ".2", ".3", ".99"):
            card = dict(self.card, **{"device.name": "alsa_card.example" + suffix,
                                    "api.alsa.card": "7", "device.bus_path": "another-port"})
            self.assertIs(identity.unique_card([card], self.selector), card)

    def test_missing_ambiguous_and_other_model_rejected(self):
        for cards in ([], [self.card, dict(self.card)],
                      [dict(self.card, **{"device.product.id": "0103"})],
                      [dict(self.card, **{"device.api": "bluez5"})]):
            with self.assertRaises(identity.HardwareUnavailable):
                identity.unique_card(cards, self.selector)

    def test_complete_graph_is_validated_without_mutation(self):
        before = copy.deepcopy(self.graph)
        self.assertEqual(self.validate(), 20)
        self.assertEqual(before, self.graph)

    def test_missing_duplicate_and_stereo_outputs_rejected(self):
        for field, value in (("audio.channels", 2), ("device.profile.name", "analog-stereo"),
                             ("device.id", 999)):
            original = self.properties(self.graph[1]).copy()
            self.properties(self.graph[1])[field] = value
            with self.assertRaises(identity.HardwareUnavailable):
                self.validate()
            self.details(self.graph[1])["props"] = original
        duplicate = copy.deepcopy(self.graph[1])
        duplicate["id"] = 999
        self.graph.append(duplicate)
        with self.assertRaises(identity.HardwareUnavailable):
            self.validate()

    def test_wrong_target_channel_and_inactive_links_rejected(self):
        link = next(item for item in self.graph if str(item["type"]).endswith(":Link"))
        original = self.details(link).copy()
        for field, value in (("input-node-id", 999), ("input-port-id", 999), ("state", "error")):
            link["info"] = dict(original, **{field: value})
            with self.assertRaises(identity.HardwareUnavailable):
                self.validate()
        link["info"] = original
        port = next(item for item in self.graph if item["id"] == original["input-port-id"])
        self.properties(port)["audio.channel"] = "LFE"
        with self.assertRaises(identity.HardwareUnavailable):
            self.validate()

    def test_missing_links_and_duplicate_identity_rejected(self):
        link = next(item for item in self.graph if str(item["type"]).endswith(":Link"))
        self.graph.remove(link)
        with self.assertRaises(identity.HardwareUnavailable):
            self.validate()
        self.graph.append(copy.deepcopy(self.graph[0]))
        with self.assertRaises(ValueError):
            self.validate()

    def test_invalid_or_vacuous_channel_mapping_rejected(self):
        for mappings in ({}, {"front": ("FL", "LFE")}, {"": ("FL", "FR")},
                         {"front": ("FL", "FR"), "other": ("FL", "FR")}):
            with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                identity.validate_downstream(self.graph, self.selector, mappings)

    def test_paused_links_are_valid_but_incomplete_ports_are_not(self):
        for item in self.graph:
            if str(item["type"]).endswith(":Link"):
                self.details(item)["state"] = "paused"
        self.assertEqual(self.validate(), 20)
        self.graph = [item for item in self.graph if item["id"] != 300]
        with self.assertRaises(identity.HardwareUnavailable):
            self.validate()

    def test_malformed_link_identity_and_info_raise_explicit_errors(self):
        link = next(item for item in self.graph if str(item["type"]).endswith(":Link"))
        self.details(link)["input-port-id"] = True
        with self.assertRaises(ValueError):
            self.validate()
        link["info"] = None
        with self.assertRaises(ValueError):
            self.validate()


class USBReadinessTests(GraphFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        os.chmod(runtime.name, 0o700)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = json.loads((SCRIPTS / "config.json").read_text())
        self.device = self.config["outputmappers"]["audio_devices"][0]
        self.device["hardware-selector"] = {"vendor": "0d8c", "product": "0102"}
        self.pulse_properties = dict(self.card, **{"object.id": "10"})
        self.pulse_card: dict[str, object] = {
            "index": 1234, "name": self.card["device.name"],
            "active_profile": "output:analog-surround-51",
            "properties": self.pulse_properties,
        }
        self.details(self.graph[0])["params"] = {
            "Profile": [{"index": 15, "name": "output:analog-surround-51"}],
        }
        self.sinks = [{"name": f"sink.{name}"} for name in self.mappings]
        self.graph_patch = patch.object(ready, "pipewire_graph", return_value=self.graph)
        self.read_graph = self.graph_patch.start()
        self.addCleanup(self.graph_patch.stop)

    def check(self, enforce: bool = False):
        return ready.audio_ready_config(self.config, enforce_profile=enforce)

    def inventory(self):
        def pactl(*arguments):
            if arguments == ("--format=json", "list", "cards"):
                return json.dumps([self.pulse_card])
            if arguments == ("--format=json", "list", "sinks"):
                return json.dumps(self.sinks)
            return ""
        return patch.object(ready, "_pactl", side_effect=pactl)

    def test_realistic_suffixed_graph_is_ready_and_read_only(self):
        original = copy.deepcopy(self.config)
        with self.inventory() as pactl:
            self.check()
        self.assertEqual(pactl.call_count, 2)
        self.assertEqual(original, self.config)

    def test_profile_recovery_targets_unique_card_index_then_requires_fresh_check(self):
        self.pulse_card["active_profile"] = "output:analog-stereo+input:analog-stereo"
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "not ready"):
                self.check(enforce=True)
        self.assertEqual(pactl.call_args.args,
                         ("set-card-profile", "1234", "output:analog-surround-51"))
        self.assertEqual(self.read_graph.call_count, 2)
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "not ready"):
                self.check()
        self.assertEqual(pactl.call_count, 2)

    def test_ambiguous_graph_never_mutates_even_if_duplicate_has_no_node(self):
        duplicate = copy.deepcopy(self.graph[0])
        duplicate["id"] = 11
        self.graph.append(duplicate)
        self.pulse_card["active_profile"] = "off"
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "ambiguous"):
                self.check(enforce=True)
        self.assertEqual(pactl.call_args_list, [
            unittest.mock.call("--format=json", "list", "cards"),
        ])

    def test_ambiguity_appearing_immediately_before_profile_change_blocks_it(self):
        duplicate = copy.deepcopy(self.graph[0])
        duplicate["id"] = 11
        self.read_graph.side_effect = [self.graph, [*self.graph, duplicate]]
        self.pulse_card["active_profile"] = "off"
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "ambiguous"):
                self.check(enforce=True)
        self.assertEqual(pactl.call_count, 2)

    def test_disagreeing_pulse_inventory_rejected_without_mutation(self):
        self.pulse_properties["object.id"] = "999"
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "disagree"):
                self.check(enforce=True)
        self.assertEqual(pactl.call_args_list, [
            unittest.mock.call("--format=json", "list", "cards"),
        ])

    def test_reused_card_id_with_new_generation_blocks_profile_mutation(self):
        self.properties(self.graph[0])["object.serial"] = 1234
        changed = copy.deepcopy(self.graph)
        self.properties(changed[0])["object.serial"] = 5678
        self.read_graph.side_effect = [self.graph, changed]
        self.pulse_card["active_profile"] = "off"
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "changed during recovery"):
                self.check(enforce=True)
        self.assertEqual(pactl.call_count, 2)

    def test_wrong_downstream_target_and_missing_virtual_sink_rejected(self):
        self.sinks.pop()
        with self.inventory(), self.assertRaisesRegex(identity.HardwareUnavailable, "Virtual sink"):
            self.check()
        self.sinks = [{"name": f"sink.{name}"} for name in self.mappings]
        link = next(item for item in self.graph if str(item["type"]).endswith(":Link"))
        self.details(link)["input-node-id"] = 999
        with self.inventory(), self.assertRaisesRegex(identity.HardwareUnavailable, "downstream target"):
            self.check()

    def test_invalid_selectors_and_overlapping_configurations_fail_before_io(self):
        variants: tuple[object, ...] = (None, {}, {"vendor": True, "product": "0102"},
                                       {"vendor": "0d8c", "product": "0102", "port": "1"})
        for selector in variants:
            with self.subTest(selector=selector):
                self.device["hardware-selector"] = selector
                with patch.object(ready, "_pactl") as pactl, self.assertRaises(ValueError):
                    self.check()
                pactl.assert_not_called()
        self.device["hardware-selector"] = {"vendor": "0d8c", "product": "0102"}
        duplicate = copy.deepcopy(self.device)
        duplicate["name"] = "other"
        for sink in duplicate["sinks"]:
            sink["name"] += "_other"
        self.config["outputmappers"]["audio_devices"].append(duplicate)
        with patch.object(ready, "_pactl") as pactl, self.assertRaisesRegex(ValueError, "Duplicate USB"):
            self.check()
        pactl.assert_not_called()

    def test_invalid_profile_sink_names_and_channel_pairs_rejected(self):
        cases: tuple[tuple[str, object], ...] = (("card-profile", "analog-stereo"), ("sinks", []))
        for field, value in cases:
            original = self.device[field]
            self.device[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                identity.hardware_configuration(self.device)
            self.device[field] = original
        sink = self.device["sinks"][0]
        for field, value in (("name", 'bad"\nname'), ("master_channel_map", "[ FL LFE ]"),
                             ("channel_map", "[ RL RR ]"), ("master_channel_map", "FL FR")):
            original = sink.get(field)
            sink[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                identity.hardware_configuration(self.device)
            sink[field] = original

    def test_mixed_legacy_device_cannot_duplicate_managed_sink_names(self):
        duplicate = copy.deepcopy(self.device)
        del duplicate["hardware-selector"]
        duplicate["name"] = "legacy"
        self.config["outputmappers"]["audio_devices"].append(duplicate)
        with patch.object(ready, "_pactl") as pactl, self.assertRaisesRegex(ValueError, "Sink names"):
            self.check()
        pactl.assert_not_called()

    def test_preflight_uses_same_model_gate_and_does_not_require_existing_links(self):
        self.read_graph.return_value = [self.graph[0]]
        ready.hardware_preflight([self.selector])
        self.read_graph.return_value = []
        with self.assertRaisesRegex(identity.HardwareUnavailable, "missing"):
            ready.hardware_preflight([self.selector])

    def stale_profile(self):
        self.pulse_card["active_profile"] = "off"
        self.properties(self.graph[0])["object.serial"] = 100
        self.details(self.graph[0])["params"] = {"Profile": [{"index": 0, "name": "off"}]}
        self.graph.append({"id": 0, "type": "PipeWire:Interface:Core", "info": {"cookie": 123}})
        for item in self.graph:
            if item["type"] == "PipeWire:Interface:Node":
                self.details(item)["state"] = "idle"

    def observe_failure(self, when):
        with patch.object(ready.time, "monotonic", return_value=when):
            with self.assertRaises(identity.HardwareUnavailable):
                self.check(enforce=True)

    def test_persistent_stale_profile_gets_one_reset_after_grace_and_fresh_verification(self):
        self.stale_profile()
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.observe_failure(14.99)
            self.assertNotIn(("set-card-profile", "1234", "off"),
                             [call.args for call in pactl.call_args_list])
            self.observe_failure(15)
            self.observe_failure(16)
        mutations = [call.args for call in pactl.call_args_list if call.args[0] == "set-card-profile"]
        self.assertEqual(mutations, [
            ("set-card-profile", "1234", "output:analog-surround-51"),
            ("set-card-profile", "1234", "output:analog-surround-51"),
            ("set-card-profile", "1234", "off"),
            ("set-card-profile", "1234", "output:analog-surround-51"),
        ])
        self.pulse_card["active_profile"] = "output:analog-surround-51"
        self.details(self.graph[0])["params"] = {
            "Profile": [{"index": 15, "name": "output:analog-surround-51"}],
        }
        with self.inventory() as pactl:
            self.check(enforce=True)
        self.assertEqual(pactl.call_count, 2)

    def test_transient_profile_restoration_cancels_the_grace_observation(self):
        self.stale_profile()
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.pulse_card["active_profile"] = "output:analog-surround-51"
            self.details(self.graph[0])["params"] = {
                "Profile": [{"index": 15, "name": "output:analog-surround-51"}],
            }
            self.check(enforce=True)
            self.pulse_card["active_profile"] = "off"
            self.details(self.graph[0])["params"] = {"Profile": [{"index": 0, "name": "off"}]}
            self.observe_failure(100)
        self.assertNotIn(("set-card-profile", "1234", "off"),
                         [call.args for call in pactl.call_args_list])

    def test_healthy_guards_and_read_only_checks_never_reset(self):
        with self.inventory() as pactl:
            for _ in range(3):
                self.check(enforce=True)
        self.assertEqual(pactl.call_count, 6)
        self.stale_profile()
        with self.inventory() as pactl:
            with self.assertRaises(identity.HardwareUnavailable):
                self.check()
        self.assertEqual(pactl.call_count, 2)

    def test_native_and_pulse_profiles_must_agree_before_ready(self):
        self.details(self.graph[0])["params"] = {"Profile": [{"index": 0, "name": "off"}]}
        with self.inventory() as pactl:
            with self.assertRaisesRegex(identity.HardwareUnavailable, "not ready"):
                self.check()
        self.assertEqual(pactl.call_count, 2)

    def test_running_audio_is_not_reset(self):
        for media_class in ("Audio/Sink", "Stream/Output/Audio"):
            self.stale_profile()
            self.add(999, "Node", {"media.class": media_class})
            self.details(self.graph[-1])["state"] = "running"
            with self.inventory() as pactl:
                self.observe_failure(10)
                self.observe_failure(20)
            self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))
            self.graph[:] = [item for item in self.graph if item["id"] not in (0, 999)]

    def test_generation_change_restarts_grace_not_an_immediate_reset(self):
        self.stale_profile()
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.properties(self.graph[0])["object.serial"] = 101
            self.observe_failure(20)
        self.assertNotIn(("set-card-profile", "1234", "off"),
                         [call.args for call in pactl.call_args_list])

    def test_device_replacement_after_off_never_selects_replacement(self):
        self.stale_profile()
        with self.inventory():
            self.observe_failure(10)
        changed = copy.deepcopy(self.graph)
        self.properties(changed[0])["object.serial"] = 101
        self.read_graph.side_effect = [self.graph, self.graph, changed]
        with self.inventory() as pactl:
            self.observe_failure(15)
        self.assertEqual([call.args for call in pactl.call_args_list
                          if call.args[0] == "set-card-profile"],
                         [("set-card-profile", "1234", "off")])

    def test_pipewire_restart_after_off_never_selects_new_server(self):
        self.stale_profile()
        with self.inventory():
            self.observe_failure(10)
        changed = copy.deepcopy(self.graph)
        self.details(changed[-1])["cookie"] = 456
        self.read_graph.side_effect = [self.graph, self.graph, changed]
        with self.inventory() as pactl:
            self.observe_failure(15)
        self.assertEqual([call.args for call in pactl.call_args_list
                          if call.args[0] == "set-card-profile"],
                         [("set-card-profile", "1234", "off")])

    def test_new_core_generation_gets_its_own_bounded_recovery(self):
        self.stale_profile()
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.observe_failure(15)
            self.observe_failure(16)
            self.details(self.graph[-1])["cookie"] = 456
            self.observe_failure(20)
            self.observe_failure(25)
        resets = [call for call in pactl.call_args_list
                  if call.args == ("set-card-profile", "1234", "off")]
        self.assertEqual(len(resets), 2)

    def test_reused_pulse_index_is_rejected_before_profile_mutation(self):
        self.stale_profile()
        changed = dict(self.pulse_card, index=999)
        with patch.object(ready, "_pactl", side_effect=[
            json.dumps([self.pulse_card]), json.dumps(self.sinks), json.dumps([changed]),
        ]) as pactl:
            self.observe_failure(10)
        self.assertEqual(pactl.call_count, 3)

    def test_failed_reset_consumes_budget_across_new_guard_invocations(self):
        self.stale_profile()
        with self.inventory():
            self.observe_failure(10)
        with self.inventory() as pactl:
            inventory = pactl.side_effect

            def failing(*arguments):
                if arguments == ("set-card-profile", "1234", "off"):
                    raise RuntimeError("reset failed")
                return inventory(*arguments)

            pactl.side_effect = failing
            with patch.object(ready.time, "monotonic", return_value=15):
                with self.assertRaisesRegex(RuntimeError, "reset failed"):
                    self.check(enforce=True)
        with self.inventory() as pactl:
            self.observe_failure(100)
        self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))

    def test_missing_generation_or_incomplete_topology_never_resets(self):
        self.stale_profile()
        del self.properties(self.graph[0])["object.serial"]
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.observe_failure(20)
        self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))
        self.properties(self.graph[0])["object.serial"] = 100
        self.graph[:] = [item for item in self.graph if item["id"] != 300]
        with self.inventory() as pactl:
            self.observe_failure(10)
            self.observe_failure(20)
        self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))

    def test_profile_lock_excludes_other_guards_and_preserves_its_inode(self):
        with ready._profile_lock() as owner:
            inode = os.fstat(owner.fileno()).st_ino
            with self.inventory() as pactl:
                with self.assertRaisesRegex(RuntimeError, "in progress"):
                    self.check(enforce=True)
            pactl.assert_not_called()
        with ready._profile_lock() as next_owner:
            self.assertEqual(os.fstat(next_owner.fileno()).st_ino, inode)

    def test_corrupt_state_and_symlink_are_explicit_failures_without_mutation(self):
        self.stale_profile()
        with ready._profile_lock() as state:
            state.write("invalid json")
        with self.inventory() as pactl:
            with self.assertRaises(ValueError):
                self.check(enforce=True)
        self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "mpdrussoundpi-audio-profile.lock"
        path.unlink()
        path.symlink_to("/dev/null")
        with self.inventory() as pactl:
            with self.assertRaises(OSError):
                self.check(enforce=True)
        pactl.assert_not_called()

    def test_missing_profile_catalogue_restarts_session_once_per_boot_not_per_generation(self):
        self.stale_profile()
        self.details(self.graph[0])["params"]["EnumProfile"] = []
        self.pulse_card["profiles"] = {}
        self.pulse_card["active_profile"] = None
        boot = "11111111-1111-1111-1111-111111111111"
        with patch.object(ready, "_boot_id", return_value=boot):
            with self.inventory() as pactl, patch.object(ready.subprocess, "run") as run:
                self.observe_failure(10)
                run.assert_not_called()
                self.observe_failure(15)
                run.assert_called_once_with(
                    ["systemctl", "--user", "--no-block", "restart", "wireplumber.service"],
                    check=True, capture_output=True, text=True, timeout=2,
                )
                self.details(self.graph[-1])["cookie"] = 456
                self.properties(self.graph[0])["object.serial"] = 101
                self.observe_failure(30)
                self.observe_failure(40)
                self.assertEqual(run.call_count, 1)
            self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))
            self.details(self.graph[0])["params"] = {
                "Profile": [{"index": 15, "name": "output:analog-surround-51"}],
            }
            self.pulse_card["profiles"] = {"output:analog-surround-51": {}}
            self.pulse_card["active_profile"] = "output:analog-surround-51"
            with self.inventory(), patch.object(ready.subprocess, "run") as run:
                self.check(enforce=True)
            run.assert_not_called()

    def test_empty_native_profile_is_recoverable_but_conflicting_profiles_are_not(self):
        self.stale_profile()
        parameters = self.details(self.graph[0])["params"]
        parameters["Profile"] = []
        with patch.object(ready, "_boot_id", return_value="boot"):
            with self.inventory(), patch.object(ready.subprocess, "run") as run:
                self.observe_failure(10)
                self.observe_failure(15)
                self.assertEqual(run.call_count, 1)
        parameters["Profile"] = [{"name": "off"}, {"name": "output:analog-surround-51"}]
        with self.inventory() as pactl, patch.object(ready.subprocess, "run") as run:
            self.observe_failure(100)
        run.assert_not_called()
        self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))

    def test_session_recovery_refuses_running_and_unknown_audio(self):
        self.stale_profile()
        self.pulse_card["profiles"] = {}
        for state in ("running", None):
            self.details(self.graph[1])["state"] = state
            with self.inventory() as pactl, patch.object(ready.subprocess, "run") as run:
                self.observe_failure(10)
                self.observe_failure(100)
            run.assert_not_called()
            self.assertFalse(any(call.args[0] == "set-card-profile" for call in pactl.call_args_list))

    def test_transient_missing_inventory_and_unrelated_healthy_card_do_not_force_restart(self):
        self.stale_profile()
        self.pulse_card["profiles"] = {}
        with patch.object(ready, "_boot_id", return_value="boot"):
            with self.inventory(), patch.object(ready.subprocess, "run") as run:
                self.observe_failure(10)
                self.pulse_card["profiles"] = {"off": {}}
                self.pulse_card["active_profile"] = "output:analog-surround-51"
                self.details(self.graph[0])["params"]["Profile"] = [
                    {"name": "output:analog-surround-51"},
                ]
                self.check(enforce=True)
                self.pulse_card["profiles"] = {}
                self.observe_failure(100)
                run.assert_not_called()
                with ready._profile_lock() as owner:
                    ready._clear_session_observation(identity.HardwareSelector(1, 2), owner)
                self.observe_failure(105)
                self.assertEqual(run.call_count, 1)

    def test_failed_session_restart_consumes_budget_and_surfaces_error(self):
        self.stale_profile()
        self.pulse_card["profiles"] = {}
        for failure in (subprocess.CalledProcessError(1, "systemctl", stderr="denied"),
                        subprocess.TimeoutExpired("systemctl", 2)):
            with ready._profile_lock() as owner:
                ready._write_recovery_state(owner, {})
            with patch.object(ready, "_boot_id", return_value="boot"), self.inventory():
                self.observe_failure(10)
                with patch.object(ready.subprocess, "run", side_effect=failure) as run:
                    with patch.object(ready.time, "monotonic", return_value=15):
                        with self.assertRaisesRegex(RuntimeError, "WirePlumber recovery"):
                            self.check(enforce=True)
                    self.observe_failure(100)
                    self.assertEqual(run.call_count, 1)

    def test_missing_catalogue_never_lists_sinks_or_mutates_from_read_only_probe(self):
        self.stale_profile()
        self.pulse_card["profiles"] = {}
        with self.inventory() as pactl, patch.object(ready.subprocess, "run") as run:
            with patch.object(ready, "_boot_id", return_value="boot"):
                self.observe_failure(10)
        self.assertNotIn(("--format=json", "list", "sinks"),
                         [call.args for call in pactl.call_args_list])
        run.assert_not_called()
        with self.inventory() as pactl, patch.object(ready.subprocess, "run") as run:
            with self.assertRaises(identity.HardwareUnavailable):
                self.check()
        self.assertEqual(pactl.call_count, 2)
        run.assert_not_called()


class PipeWireInventoryTests(unittest.TestCase):
    def test_inventory_failures_are_explicit_and_bounded(self):
        for failure in (subprocess.TimeoutExpired("pw-dump", 2),
                        subprocess.CalledProcessError(1, "pw-dump", stderr="offline")):
            with patch.object(ready.subprocess, "run", side_effect=failure) as run:
                with self.assertRaises(RuntimeError):
                    ready.pipewire_graph()
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertEqual(run.call_args.args[0], ["pw-dump"])
        for raw in ("{}", "[null]", "invalid"):
            with patch.object(ready.subprocess, "run", return_value=Mock(stdout=raw)):
                with self.assertRaises(ValueError):
                    ready.pipewire_graph()


if __name__ == "__main__":
    unittest.main()

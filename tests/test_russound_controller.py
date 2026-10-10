import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from web.russound_controller import (
    apply_shortcut,
    build_config_editor_payload,
    build_view_payload,
    get_controller,
    handle_zone_setting_change,
    load_state,
    persist_state,
    RussoundBackend,
    RussoundController,
    RussoundState,
    set_shared_source,
    update_config_zones,
    update_system_power,
    update_zone_setting,
    Zone,
)


class RussoundControllerTests(unittest.TestCase):
    def test_system_off_write_failure_does_not_change_state(self):
        zone = Zone(name="Kitchen", controller=1, zone_number=1, power=True)
        state = RussoundState(system_power=True, zones=[zone])
        backend = Mock()
        backend.turn_all_zones_off.return_value = False
        with self.assertRaises(RuntimeError):
            state.set_system_power(False, backend=backend)
        self.assertTrue(state.system_power)
        self.assertTrue(zone.power)

    def test_shortcut_editor_creates_updates_deletes_and_rejects_invalid_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"controllers": [{"id": 1, "zone_count": 6}], "zones": [], "inputs": []}))
            controller = RussoundController(config_path, Path(directory) / "state.json")
            shortcut = {"id": "party", "name": "Party", "source": 1, "zone_addresses": [{"controller": 1, "zone": 1}]}
            payload = {
                "zone_slots": [{"controller": 1, "zone": 1, "name": "Kitchen", "enabled": True, "visible": True}],
                "source_slots": [{"id": 1, "name": "Radio", "enabled": True}], "shortcuts": [shortcut],
            }
            result = controller.update_config_zones(payload=payload)
            self.assertEqual(result["config"]["shortcuts"], [shortcut])
            payload["shortcuts"] = [{**shortcut, "name": "Evening"}]
            controller.update_config_zones(payload=payload)
            self.assertEqual(controller.load_config()["shortcuts"][0]["name"], "Evening")
            saved = config_path.read_text()
            for invalid in (
                [{**shortcut, "source": 2}], [{**shortcut, "name": ""}],
                [shortcut, shortcut], [{**shortcut, "zone_addresses": []}],
                [{**shortcut, "zone_addresses": [{"controller": 1, "zone": 2}]}],
                [{**shortcut, "id": ""}],
            ):
                with self.subTest(shortcuts=invalid), self.assertRaises(ValueError):
                    controller.update_config_zones(payload={**payload, "shortcuts": invalid})
                self.assertEqual(config_path.read_text(), saved)
            controller.update_config_zones(payload={**payload, "shortcuts": []})
            self.assertEqual(controller.load_config()["shortcuts"], [])

    def test_setup_recovers_invalid_config_and_saves_without_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            state_path = Path(directory) / "state.json"
            controller = RussoundController(config_path, state_path)
            for text in ("{broken", "[]", "{}", '{"backend":"bad"}'):
                config_path.write_text(text)
                self.assertIsNone(controller.load_config())
                payload = controller.build_config_editor_payload()
                self.assertTrue(payload["config_required"])
                self.assertEqual(len(payload["zone_slots"]), 6)
                self.assertEqual(len(payload["source_slots"]), 6)
            with patch.object(RussoundBackend, "connect", side_effect=AssertionError("Must not contact hardware")) as connect:
                payload = controller.update_config_zones(payload={
                    "backend": {"host": "gateway", "port": 7777},
                    "controllers": [{"id": 1, "zone_count": 6}],
                    "zone_slots": [{"controller": 1, "zone": 1, "name": "Kitchen", "enabled": True, "visible": True}],
                    "source_slots": [{"id": 1, "name": "Radio", "enabled": True}],
                })
                connect.assert_not_called()
            self.assertFalse(payload["config_required"])
            self.assertEqual(json.loads(config_path.read_text())["backend"]["port"], 7777)
            self.assertEqual(next(Path(directory).glob("*.bak")).read_text(), '{"backend":"bad"}')
            self.assertTrue(state_path.exists())

    def test_setup_rejects_invalid_endpoint_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text("{broken")
            controller = RussoundController(config_path, Path(directory) / "state.json")
            with self.assertRaises(ValueError):
                controller.update_config_zones(payload={"backend": {"host": "gateway", "port": 70000}, "controllers": [{"id": 1, "zone_count": 6}], "zone_slots": []})
            self.assertEqual(config_path.read_text(), "{broken")
            self.assertEqual(list(Path(directory).glob("*.bak")), [])

    def test_setup_state_write_failure_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text("{broken")
            controller = RussoundController(config_path, Path(directory) / "state.json")
            with patch.object(controller, "persist_state", side_effect=PermissionError("read-only data volume")):
                with self.assertRaises(PermissionError):
                    controller.update_config_zones(payload={
                        "backend": {"host": "gateway", "port": 6666},
                        "controllers": [{"id": 1, "zone_count": 6}], "zone_slots": [],
                    })
            self.assertEqual(config_path.read_text(), "{broken")
            self.assertEqual(list(Path(directory).glob("*.bak")), [])

    def test_setup_handles_unreadable_and_malformed_nested_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            controller = RussoundController(config_path, Path(directory) / "state.json")
            for config in (
                {"backend": {"host": "gateway", "port": True}},
                {"backend": {"poll_interval_seconds": "slow"}},
                {"controllers": [{"id": "one", "zone_count": 6}]},
                {"controllers": [{"id": 1, "zone_count": 7}]},
                {"zones": [{"controller": 1, "zone": "one"}]},
                {"inputs": [{"id": True}]},
                {"shortcuts": [{"zone_addresses": "bad"}]},
            ):
                with self.subTest(config=config):
                    config_path.write_text(json.dumps(config))
                    self.assertTrue(controller.build_config_editor_payload()["config_required"])
                    self.assertTrue(controller.build_view_payload(refresh_backend=False)["config_required"])
            with patch.object(Path, "open", side_effect=PermissionError("unreadable config")):
                payload = controller.build_config_editor_payload()
                self.assertTrue(payload["config_required"])
                self.assertIn("unreadable config", payload["message"])

    def test_controller_is_a_singleton_and_loads_state_as_russoundstate(self):
        controller = get_controller()

        self.assertIsInstance(controller, RussoundController)
        self.assertIs(controller, get_controller())

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            state = controller.load_state(config_path, state_path, refresh_backend=False)

            self.assertIsInstance(state, RussoundState)
            self.assertEqual(state.zones[0].name, "Living Room")
            self.assertEqual(state.inputs[0]["name"], "Radio")

    def test_initial_state_uses_configured_zones_and_inputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [
                            {"id": "living", "name": "Living Room"},
                            {"id": "patio", "name": "Patio"},
                        ],
                        "inputs": [
                            {"id": 1, "name": "Radio"},
                            {"id": 2, "name": "TV"},
                            {"id": 3, "name": "Bluetooth"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path)

            self.assertFalse(state["system_power"])
            self.assertEqual(state["zones"][0].name, "Living Room")
            self.assertEqual(state["zones"][0].source, 1)
            self.assertEqual(state["zones"][0].volume, 20)
            self.assertEqual(state["zones"][0].bass, 0)
            self.assertEqual(state["zones"][0].treble, 0)
            self.assertFalse(state["zones"][0].loudness)
            self.assertEqual(state["zones"][0].balance, 0)
            self.assertEqual(state["inputs"][0]["name"], "Radio")

    def test_load_state_preserves_values_from_legacy_dict_based_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "system_power": True,
                        "zones": [
                            {
                                "name": "Living Room",
                                "power": True,
                                "source": 2,
                                "volume": 33,
                                "bass": 5,
                                "treble": -3,
                                "loudness": True,
                                "balance": 2,
                                "controller": 1,
                                "zone": 1,
                            }
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path, refresh_backend=False)

            self.assertTrue(state["system_power"])
            self.assertTrue(state["zones"][0].power)
            self.assertEqual(state["zones"][0].source, 2)
            self.assertEqual(state["zones"][0].volume, 33)
            self.assertEqual(state["zones"][0].bass, 5)
            self.assertEqual(state["zones"][0].treble, -3)
            self.assertTrue(state["zones"][0].loudness)
            self.assertEqual(state["zones"][0].balance, 2)

    def test_handle_zone_setting_change_persists_updated_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            payload = handle_zone_setting_change(config_path, state_path, controller_id=1, zone_number=1, setting="volume", value=42)

            self.assertEqual(payload["state"]["zones"][0]["volume"], 42)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["zones"][0]["volume"], 42)
            self.assertNotIn("inputs", persisted)

    def test_update_zone_setting_raises_when_power_update_fails(self):
        state = RussoundState(zones=[Zone(name="Living Room", controller=1, zone_number=1)], inputs=[{"id": 1, "name": "Radio"}])
        backend = Mock()
        backend.set_zone_power.return_value = False

        with self.assertRaises(RuntimeError):
            state.update_zone_setting(1, 1, "power", True, backend=backend)

    def test_update_zone_setting_raises_when_source_update_fails(self):
        state = RussoundState(zones=[Zone(name="Living Room", controller=1, zone_number=1)], inputs=[{"id": 1, "name": "Radio"}])
        backend = Mock()
        backend.set_zone_source.return_value = False

        with self.assertRaises(RuntimeError):
            state.update_zone_setting(1, 1, "source", 1, backend=backend)

    def test_update_zone_setting_raises_when_volume_update_fails(self):
        state = RussoundState(zones=[Zone(name="Living Room", controller=1, zone_number=1)], inputs=[{"id": 1, "name": "Radio"}])
        backend = Mock()
        backend.set_zone_volume.return_value = False

        with self.assertRaises(RuntimeError):
            state.update_zone_setting(1, 1, "volume", 42, backend=backend)

    def test_update_zone_setting_keeps_advanced_settings_local_when_backend_is_unavailable(self):
        state = RussoundState(zones=[Zone(name="Living Room", controller=1, zone_number=1)], inputs=[{"id": 1, "name": "Radio"}])

        state.update_zone_setting(1, 1, "treble", 4, backend=None)

        self.assertEqual(state.zones[0].treble, 4)

    def test_build_view_payload_reports_backend_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(RussoundBackend, "_connect", return_value=None):
                payload = build_view_payload(config_path, state_path, refresh_backend=False)

            self.assertFalse(payload["backend_status"]["connected"])
            self.assertIn("currently unavailable", payload["backend_status"]["message"])

    def test_backend_status_flags_a_reachable_gateway_with_unresponsive_hardware(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "backend": {"host": "127.0.0.1", "port": 6666},
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )
            controller = RussoundController(config_path=config_path, state_path=state_path)

            # The zone poll connects but the hardware never answers.
            with patch.object(RussoundBackend, "connect", return_value=True), patch.object(
                RussoundBackend, "read_zone", return_value=None
            ):
                controller.load_state(refresh_backend=True)
                status = controller._backend_status()

            self.assertFalse(status["connected"])
            self.assertIn("not responding", status["message"])

    def test_backend_status_with_open_shared_connection_reports_silent_hardware(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "backend": {"host": "127.0.0.1", "port": 6666},
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )
            controller = RussoundController(config_path=config_path, state_path=state_path)
            shared_backend = Mock()
            shared_backend.connect.return_value = True
            shared_backend.is_connected.return_value = True
            shared_backend.read_zone.return_value = None
            controller.set_shared_backend(shared_backend)

            controller.load_state(refresh_backend=True, backend=shared_backend)
            status = controller._backend_status()

            self.assertFalse(status["connected"])
            self.assertIn("not responding", status["message"])

    def test_backend_status_is_connected_when_a_zone_poll_answers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "backend": {"host": "127.0.0.1", "port": 6666},
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )
            controller = RussoundController(config_path=config_path, state_path=state_path)
            zone_reply = {"power": True, "source": 1, "volume": 30, "bass": 0, "treble": 0, "loudness": False, "balance": 0}

            with patch.object(RussoundBackend, "connect", return_value=True), patch.object(
                RussoundBackend, "read_zone", return_value=zone_reply
            ):
                controller.load_state(refresh_backend=True)
                status = controller._backend_status()

            self.assertTrue(status["connected"])
            self.assertEqual(status["message"], "")

    def test_backend_status_reports_unavailable_when_the_gateway_cannot_be_reached(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "backend": {"host": "127.0.0.1", "port": 6666},
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )
            controller = RussoundController(config_path=config_path, state_path=state_path)

            with patch.object(RussoundBackend, "connect", return_value=False), patch.object(
                RussoundBackend, "read_zone", return_value=None
            ):
                controller.load_state(refresh_backend=True)
                status = controller._backend_status()

            self.assertFalse(status["connected"])
            self.assertIn("currently unavailable", status["message"])

    def test_build_view_payload_requires_a_config_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"

            payload = build_view_payload(None, state_path)

            self.assertTrue(payload["config_required"])
            self.assertIsNone(payload["config"])
            self.assertIn("config file is required", payload["message"])
            self.assertEqual(payload["state"], {"system_power": False, "zones": []})

    def test_build_view_payload_hides_zones_excluded_from_overview(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 2}],
                        "zones": [
                            {"id": "living", "name": "Living Room", "controller": 1, "zone": 1, "visible": True},
                            {"id": "garage", "name": "Garage", "controller": 1, "zone": 2, "visible": False},
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_view_payload(config_path, state_path, refresh_backend=False)

            self.assertEqual([(zone["controller"], zone["zone"]) for zone in payload["config"]["zones"]], [(1, 1)])
            self.assertEqual([(zone["controller"], zone["zone"]) for zone in payload["state"]["zones"]], [(1, 1)])

    def test_build_view_payload_keeps_metadata_in_config_and_frontend_zone_payloads_include_name(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 1}],
                        "zones": [
                            {"id": "living", "name": "Living Room", "controller": 1, "zone": 1, "visible": True, "enabled": True},
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_view_payload(config_path, state_path, refresh_backend=False)
            state_zone = payload["state"]["zones"][0]

            self.assertEqual(payload["config"]["zones"][0]["name"], "Living Room")
            self.assertTrue(payload["config"]["zones"][0]["visible"])
            self.assertTrue(payload["config"]["zones"][0]["enabled"])
            self.assertEqual(state_zone["controller"], 1)
            self.assertEqual(state_zone["zone"], 1)
            self.assertEqual(state_zone["name"], "Living Room")
            self.assertNotIn("visible", state_zone)
            self.assertNotIn("enabled", state_zone)

    def test_update_config_zones_rebuilds_slots_and_prunes_removed_zones_from_shortcuts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 2}],
                        "zones": [
                            {"name": "Living Room", "controller": 1, "zone": 1, "visible": True},
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}],
                        "shortcuts": [{"id": "party", "name": "Party", "zone_addresses": [{"controller": 1, "zone": 1}, {"controller": 2, "zone": 1}], "source": 1}],
                    }
                ),
                encoding="utf-8",
            )

            response = update_config_zones(
                config_path,
                state_path,
                {
                    "zone_slots": [
                        {"controller": 1, "zone": 1, "keypad_id": 2, "enabled": True, "visible": True, "name": "Lounge"},
                        {"controller": 1, "zone": 2, "keypad_id": 1, "enabled": True, "visible": False, "name": "Patio"},
                    ],
                    "source_slots": [
                        {"id": 1, "name": "Tuner", "enabled": False},
                    ],
                },
            )

            persisted_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual([zone["name"] for zone in persisted_config["zones"]], ["Lounge", "Patio"])
            self.assertEqual([zone["keypad_id"] for zone in persisted_config["zones"]], [2, 1])
            self.assertEqual(persisted_config["zones"][1]["visible"], False)
            self.assertEqual(response["zone_slots"][1]["enabled"], True)
            self.assertEqual(
                persisted_config["shortcuts"][0]["zone_addresses"],
                [{"controller": 1, "zone": 1}],
            )
            self.assertNotIn("zone_ids", persisted_config["shortcuts"][0])
            self.assertEqual(persisted_config["inputs"], [])

    def test_build_config_editor_payload_returns_slots_per_controller_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 3}],
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_config_editor_payload(config_path)

            self.assertEqual(len(payload["zone_slots"]), 3)
            self.assertTrue(payload["zone_slots"][0]["enabled"])
            self.assertFalse(payload["zone_slots"][1]["enabled"])
            self.assertEqual([slot["keypad_id"] for slot in payload["zone_slots"]], [1, 1, 1])
            self.assertEqual(
                payload["source_slots"],
                [
                    {"id": 1, "name": "Radio", "enabled": True},
                    {"id": 2, "name": "Source 2", "enabled": False},
                    {"id": 3, "name": "Source 3", "enabled": False},
                    {"id": 4, "name": "Source 4", "enabled": False},
                    {"id": 5, "name": "Source 5", "enabled": False},
                    {"id": 6, "name": "Source 6", "enabled": False},
                ],
            )

    def test_disabled_sources_remain_editable_but_are_excluded_from_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 1}],
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [
                            {"id": 1, "name": "Radio", "enabled": True},
                            {"id": 2, "name": "TV", "enabled": False},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            editor_payload = build_config_editor_payload(config_path)
            state = load_state(config_path, refresh_backend=False)

            self.assertEqual(editor_payload["source_slots"][1], {"id": 2, "name": "TV", "enabled": False})
            self.assertEqual(state.inputs, [{"id": 1, "name": "Radio"}])

    def test_enabling_an_unconfigured_source_creates_its_config_entry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 1}],
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            update_config_zones(
                config_path,
                None,
                {
                    "zone_slots": [{"controller": 1, "zone": 1, "enabled": True, "visible": True, "name": "Living Room"}],
                    "source_slots": [
                        {"id": 1, "name": "Radio", "enabled": True},
                        {"id": 4, "name": "Game Console", "enabled": True},
                    ],
                },
            )

            persisted_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted_config["inputs"], [{"id": 1, "name": "Radio"}, {"id": 4, "name": "Game Console"}])

    def test_update_config_zones_rejects_unknown_source_slot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "controllers": [{"id": 1, "zone_count": 1}],
                        "zones": [{"id": "living", "name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "source id must be from 1 to 6"):
                update_config_zones(
                    config_path,
                    state_path,
                    {
                        "zone_slots": [
                            {"controller": 1, "zone": 1, "enabled": True, "visible": True, "name": "Living Room"},
                        ],
                        "source_slots": [
                            {"id": 7, "name": "TV", "enabled": True},
                        ],
                    },
                )

    def test_zone_object_capsules_zone_properties_and_actions(self):
        zone = Zone(name="Living Room", controller=2, zone_number=3)

        self.assertEqual(zone.name, "Living Room")
        self.assertEqual(zone.address, (2, 3))
        self.assertFalse(zone.power)

        zone.power = True
        zone.source = 1
        zone.volume = 45
        zone.bass = -4
        zone.treble = 3
        zone.loudness = True
        zone.balance = -2

        self.assertTrue(zone.power)
        self.assertEqual(zone.source, 1)
        self.assertEqual(zone.volume, 45)
        self.assertEqual(zone.bass, -4)
        self.assertEqual(zone.treble, 3)
        self.assertTrue(zone.loudness)
        self.assertEqual(zone.balance, -2)
        self.assertEqual(zone.to_state_payload(), {
            "power": True,
            "source": 1,
            "volume": 45,
            "bass": -4,
            "treble": 3,
            "loudness": True,
            "balance": -2,
            "turn_on_volume": 0,
            "controller": 2,
            "zone": 3,
        })

    def test_zone_updates_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path)
            update_zone_setting(state, 1, 1, "power", True)
            set_shared_source(state, 1)
            update_zone_setting(state, 1, 1, "volume", 45)
            with patch.object(RussoundBackend, "set_zone_bass", return_value=True), patch.object(RussoundBackend, "set_zone_treble", return_value=True), patch.object(RussoundBackend, "set_zone_loudness", return_value=True), patch.object(RussoundBackend, "set_zone_balance", return_value=True):
                update_zone_setting(state, 1, 1, "bass", -5)
                update_zone_setting(state, 1, 1, "treble", 4)
                update_zone_setting(state, 1, 1, "loudness", True)
                update_zone_setting(state, 1, 1, "balance", -3)
            persist_state(state_path, state)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["system_power"])
            self.assertTrue(persisted["zones"][0]["power"])
            self.assertEqual(persisted["zones"][0]["source"], 1)
            self.assertEqual(persisted["zones"][0]["volume"], 45)
            self.assertEqual(persisted["zones"][0]["bass"], -5)
            self.assertEqual(persisted["zones"][0]["treble"], 4)
            self.assertTrue(persisted["zones"][0]["loudness"])
            self.assertEqual(persisted["zones"][0]["balance"], -3)

    def test_zone_sound_parameter_updates_only_the_target_zone(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [
                            {"name": "Living Room", "controller": 1, "zone": 1},
                            {"name": "Kitchen", "controller": 1, "zone": 2},
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path)
            with patch.object(RussoundBackend, "set_zone_bass", return_value=True), patch.object(RussoundBackend, "set_zone_treble", return_value=True), patch.object(RussoundBackend, "set_zone_loudness", return_value=True), patch.object(RussoundBackend, "set_zone_balance", return_value=True):
                update_zone_setting(state, 1, 2, "bass", 6)
                update_zone_setting(state, 1, 2, "treble", 5)
                update_zone_setting(state, 1, 2, "loudness", True)
                update_zone_setting(state, 1, 2, "balance", -4)

            self.assertEqual(state["zones"][0].bass, 0)
            self.assertEqual(state["zones"][0].treble, 0)
            self.assertFalse(state["zones"][0].loudness)
            self.assertEqual(state["zones"][0].balance, 0)
            self.assertEqual(state["zones"][1].bass, 6)
            self.assertEqual(state["zones"][1].treble, 5)
            self.assertTrue(state["zones"][1].loudness)
            self.assertEqual(state["zones"][1].balance, -4)

    def test_zone_sound_parameter_update_keeps_local_state_when_hardware_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                        "inputs": [{"id": 1, "name": "Radio"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path)
            with patch.object(RussoundBackend, "set_zone_bass", return_value=False):
                payload = update_zone_setting(state, 1, 1, "bass", 7)

            self.assertEqual(payload.zones[0].bass, 7)

    def test_zone_source_updates_only_the_target_zone(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "zones": [
                            {"name": "Living Room", "controller": 1, "zone": 1},
                            {"name": "Kitchen", "controller": 1, "zone": 2},
                            {"name": "Patio", "controller": 1, "zone": 3},
                        ],
                        "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}],
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(config_path, state_path)
            update_zone_setting(state, 1, 1, "source", 2)
            self.assertEqual(state["zones"][0].source, 2)
            self.assertEqual(state["zones"][1].source, 1)
            self.assertEqual(state["zones"][2].source, 1)

    def test_zone_source_changes_are_forwarded_to_russound_backend(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def connect(self) -> bool:
                return True

            def is_connected(self) -> bool:
                return True

            def set_source(self, controller: int, zone: int, source: int) -> None:
                self.calls.append((controller, zone, source))

        dummy_client = DummyClient()
        with patch.object(RussoundBackend, "_connect", return_value=dummy_client):
            with tempfile.TemporaryDirectory() as tmp_dir:
                config_path = Path(tmp_dir) / "config.json"
                state_path = Path(tmp_dir) / "state.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "zones": [{"name": "Living Room", "controller": 1, "zone": 1}],
                            "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}],
                        }
                    ),
                    encoding="utf-8",
                )

                state = load_state(config_path, state_path)
                update_zone_setting(state, 1, 1, "source", 2)

                self.assertEqual(dummy_client.calls[0], (1, 1, 1))

    def test_zone_mapping_uses_configured_controller_and_zone(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def connect(self) -> bool:
                return True

            def is_connected(self) -> bool:
                return True

            def set_power(self, controller: int, zone: int, power: int) -> None:
                self.calls.append((controller, zone, power))

        dummy_client = DummyClient()
        with patch.object(RussoundBackend, "_connect", return_value=dummy_client):
            with tempfile.TemporaryDirectory() as tmp_dir:
                config_path = Path(tmp_dir) / "config.json"
                state_path = Path(tmp_dir) / "state.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "controllers": [{"id": 2, "zone_count": 4}],
                            "zones": [{"name": "Living Room", "controller": 2, "zone": 3}],
                            "inputs": [{"id": 1, "name": "Radio"}],
                        }
                    ),
                    encoding="utf-8",
                )

                state = load_state(config_path, state_path)
                update_zone_setting(state, 2, 3, "power", True)

                self.assertEqual(dummy_client.calls[0], (2, 3, 1))

    def test_enabling_a_zone_turns_system_power_on(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(json.dumps({"zones": [{"name": "Living Room", "controller": 1, "zone": 1}], "inputs": [{"id": 1, "name": "Radio"}]}), encoding="utf-8")
            state = load_state(config_path, state_path)
            update_zone_setting(state, 1, 1, "power", True)
            self.assertTrue(state["system_power"])

    def test_system_power_can_only_be_turned_off_globally(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(json.dumps({"zones": [{"name": "Living Room", "controller": 1, "zone": 1}], "inputs": [{"id": 1, "name": "Radio"}]}), encoding="utf-8")
            state = load_state(config_path, state_path)
            update_zone_setting(state, 1, 1, "power", True)
            with patch.object(RussoundBackend, "turn_all_zones_on", return_value=True):
                update_system_power(state, True)
            self.assertTrue(state["system_power"])
            with patch.object(RussoundBackend, "turn_all_zones_off", return_value=True):
                update_system_power(state, False)
            self.assertFalse(state["system_power"])
            self.assertFalse(state["zones"][0].power)

    def test_shortcut_applies_selected_zones_and_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(json.dumps({"zones": [{"id": "living", "name": "Living Room", "controller": 1, "zone": 1}, {"id": "patio", "name": "Patio", "controller": 1, "zone": 2}], "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}]}), encoding="utf-8")
            state = load_state(config_path, state_path)
            shortcut = {"zone_addresses": [{"controller": 1, "zone": 1}, {"controller": 1, "zone": 2}], "source": 2}
            apply_shortcut(state, shortcut)
            self.assertTrue(state["system_power"])
            self.assertTrue(state["zones"][0].power)
            self.assertTrue(state["zones"][1].power)
            self.assertEqual(state["zones"][0].source, 2)
            self.assertEqual(state["zones"][1].source, 2)


if __name__ == "__main__":
    unittest.main()

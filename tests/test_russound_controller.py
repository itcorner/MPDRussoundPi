import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.russound_controller import (
    apply_shortcut,
    build_config_editor_payload,
    build_view_payload,
    load_state,
    persist_state,
    RussoundBackend,
    set_shared_source,
    update_config_zones,
    update_system_power,
    update_zone_setting,
    Zone,
)


class RussoundControllerTests(unittest.TestCase):
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
            self.assertEqual(state["zones"][0]["name"], "Living Room")
            self.assertEqual(state["zones"][0]["source"], 1)
            self.assertEqual(state["zones"][0]["volume"], 20)
            self.assertEqual(state["inputs"][0]["name"], "Radio")

    def test_build_view_payload_requires_a_config_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"

            payload = build_view_payload(None, state_path)

            self.assertTrue(payload["config_required"])
            self.assertIsNone(payload["config"])
            self.assertIn("config file is required", payload["message"])
            self.assertEqual(payload["state"], {"system_power": False, "zones": [], "inputs": []})

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
                        {"controller": 1, "zone": 1, "enabled": True, "visible": True, "name": "Lounge"},
                        {"controller": 1, "zone": 2, "enabled": True, "visible": False, "name": "Patio"},
                    ],
                    "source_slots": [
                        {"id": 1, "name": "Tuner"},
                    ],
                },
            )

            persisted_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual([zone["name"] for zone in persisted_config["zones"]], ["Lounge", "Patio"])
            self.assertEqual(persisted_config["zones"][1]["visible"], False)
            self.assertEqual(response["zone_slots"][1]["enabled"], True)
            self.assertEqual(
                persisted_config["shortcuts"][0]["zone_addresses"],
                [{"controller": 1, "zone": 1}],
            )
            self.assertNotIn("zone_ids", persisted_config["shortcuts"][0])
            self.assertEqual(persisted_config["inputs"][0]["name"], "Tuner")

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
            self.assertEqual(payload["source_slots"], [{"id": 1, "name": "Radio"}])

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

            with self.assertRaisesRegex(ValueError, "unknown source 2"):
                update_config_zones(
                    config_path,
                    state_path,
                    {
                        "zone_slots": [
                            {"controller": 1, "zone": 1, "enabled": True, "visible": True, "name": "Living Room"},
                        ],
                        "source_slots": [
                            {"id": 2, "name": "TV"},
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

        self.assertTrue(zone.power)
        self.assertEqual(zone.source, 1)
        self.assertEqual(zone.volume, 45)
        self.assertEqual(zone.to_dict(), {
            "name": "Living Room",
            "power": True,
            "source": 1,
            "volume": 45,
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
            persist_state(state_path, state)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["system_power"])
            self.assertTrue(persisted["zones"][0]["power"])
            self.assertEqual(persisted["zones"][0]["source"], 1)
            self.assertEqual(persisted["zones"][0]["volume"], 45)

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
            self.assertEqual(state["zones"][0]["source"], 2)
            self.assertEqual(state["zones"][1]["source"], 1)
            self.assertEqual(state["zones"][2]["source"], 1)

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
            update_system_power(state, True)
            self.assertTrue(state["system_power"])
            update_system_power(state, False)
            self.assertFalse(state["system_power"])
            self.assertFalse(state["zones"][0]["power"])

    def test_shortcut_applies_selected_zones_and_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            state_path = Path(tmp_dir) / "state.json"
            config_path.write_text(json.dumps({"zones": [{"id": "living", "name": "Living Room", "controller": 1, "zone": 1}, {"id": "patio", "name": "Patio", "controller": 1, "zone": 2}], "inputs": [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}]}), encoding="utf-8")
            state = load_state(config_path, state_path)
            shortcut = {"zone_addresses": [{"controller": 1, "zone": 1}, {"controller": 1, "zone": 2}], "source": 2}
            apply_shortcut(state, shortcut)
            self.assertTrue(state["system_power"])
            self.assertTrue(state["zones"][0]["power"])
            self.assertTrue(state["zones"][1]["power"])
            self.assertEqual(state["zones"][0]["source"], 2)
            self.assertEqual(state["zones"][1]["source"], 2)


if __name__ == "__main__":
    unittest.main()

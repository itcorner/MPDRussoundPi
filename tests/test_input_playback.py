import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from web.config_types import validate_russound_config
from web.input_playback import resolve_input_names
from web.russound_controller import RussoundController
from web.russound_server import RussoundHTTPServer


BINDING = {"agent_id": "musicpi", "output_id": "stereo_front"}
INPUT = {"id": 1, "name": "Radio", "playback_output": BINDING}


def playback(status="ok", selection=None):
    return {"agents": [{
        "agent_id": "musicpi", "status": status,
        "snapshot": {"stale": False, "outputs": [{
            "id": "stereo_front", "label": "Stereo Front",
            "selection": selection if selection is not None else {"key": "tunein:s8007", "label": "OE3"},
            "playback": {"value": {"state": "stop", "now_playing": {"title": "Not the input name"}}},
        }]},
    }]}


class InputPlaybackTests(unittest.TestCase):
    def test_station_label_is_runtime_only_and_unmapped_inputs_are_unchanged(self):
        inputs = [copy.deepcopy(INPUT), {"id": 2, "name": "TV"}]
        before = copy.deepcopy(inputs)
        resolved = resolve_input_names(inputs, playback())
        self.assertEqual(resolved[0]["display_name"], "OE3")
        self.assertEqual(resolved[0]["playback_status"], "ok")
        self.assertEqual(resolved[1]["display_name"], "TV")
        self.assertEqual(inputs, before)
        self.assertEqual(resolve_input_names([{"id": 3}], {"agents": []})[0]["display_name"], "Source 3")

    def test_no_station_and_offline_and_unknown_mappings(self):
        empty = {"key": None, "label": None}
        cases = [
            (playback(selection=empty), "Radio", "no_station"),
            (playback(status="unavailable"), "OE3", "unavailable"),
            ({"agents": []}, "Radio", "unmapped"),
            ({"agents": [{"agent_id": "musicpi", "status": "unavailable", "snapshot": None}]},
             "Radio", "unavailable"),
        ]
        unknown = playback()
        unknown["agents"][0]["snapshot"]["outputs"] = []
        cases.append((unknown, "Radio", "unmapped"))
        stale = playback()
        stale["agents"][0]["snapshot"]["stale"] = True
        cases.append((stale, "OE3", "unavailable"))
        for snapshot, name, status in cases:
            with self.subTest(name=name, status=status):
                result = resolve_input_names([INPUT], snapshot)[0]
                self.assertEqual((result["display_name"], result["playback_status"]), (name, status))
                self.assertTrue(result["playback_message"])

    def test_binding_validation(self):
        validate_russound_config({"inputs": [INPUT]})
        for binding in (None, [], {}, {"agent_id": "musicpi"}, {**BINDING, "extra": True},
                        {**BINDING, "agent_id": "../escape"}, {**BINDING, "output_id": 1}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                validate_russound_config({"inputs": [{**INPUT, "playback_output": binding}]})

    def test_editor_binding_round_trip_removal_and_invalid_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"controllers": [{"id": 1, "zone_count": 6}], "inputs": [INPUT]}))
            controller = RussoundController(path, Path(directory) / "state.json")
            slot = controller.build_config_editor_payload()["source_slots"][0]
            self.assertEqual(slot["playback_output"], BINDING)
            controller.update_config_zones(payload={"zone_slots": [], "source_slots": [slot]})
            self.assertEqual(controller.load_config()["inputs"][0], INPUT)
            saved = path.read_bytes()
            with self.assertRaises(ValueError):
                controller.update_config_zones(payload={
                    "zone_slots": [], "source_slots": [{**slot, "playback_output": {"bad": "mapping"}}],
                })
            self.assertEqual(path.read_bytes(), saved)
            without_binding = {key: value for key, value in slot.items() if key != "playback_output"}
            controller.update_config_zones(payload={"zone_slots": [], "source_slots": [without_binding]})
            self.assertEqual(controller.load_config()["inputs"][0]["playback_output"], BINDING)
            controller.update_config_zones(payload={
                "zone_slots": [], "source_slots": [{**slot, "playback_output": None}],
            })
            self.assertNotIn("playback_output", controller.load_config()["inputs"][0])

    def test_http_state_events_editor_and_config_save_use_cached_station(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "controllers": [{"id": 1, "zone_count": 6}], "zones": [], "inputs": [INPUT],
            }))
            agent = Mock()
            agent.snapshot.return_value = {**playback(), "revision": 1}
            server = RussoundHTTPServer(path, Path(directory) / "state.json", playback_clients=[agent])
            client = server.app.test_client()
            try:
                before = path.read_bytes()
                state = client.get("/api/state").json
                self.assertEqual(state["resolved_inputs"][0]["display_name"], "OE3")
                self.assertEqual(state["config"]["inputs"][0]["name"], "Radio")
                self.assertEqual(server._build_state_payload_for_event()["resolved_inputs"], state["resolved_inputs"])
                editor = client.get("/api/config").json
                self.assertEqual(editor["playback_outputs"][0]["output_id"], "stereo_front")
                self.assertEqual(editor["source_slots"][0]["playback_output"], BINDING)
                updated = playback(selection={"key": "tunein:s25744", "label": "Radio 88.6"})
                agent.snapshot.return_value = {**updated, "revision": 2}
                self.assertEqual(client.get("/api/state").json["resolved_inputs"][0]["display_name"], "Radio 88.6")
                self.assertEqual(path.read_bytes(), before)
                with patch.object(server, "broadcast_state_change"):
                    saved = client.post("/api/config", json={"zone_slots": [], "source_slots": editor["source_slots"]})
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.json["resolved_inputs"][0]["display_name"], "Radio 88.6")
                agent.poll_once.assert_not_called()
            finally:
                agent.close()

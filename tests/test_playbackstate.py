import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from playbackfixtures import configuration

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    outputconfig = importlib.import_module("lib.outputconfig")
    state = importlib.import_module("lib.playbackstate")
    Station = importlib.import_module("lib.stations").Station


class PlaybackStateTests(unittest.TestCase):
    def setUp(self):
        self.config = outputconfig.parse_configuration(json.dumps(configuration()).encode())
        self.initial = state.parse_state(None, self.config)

    def parse(self, value):
        return state.parse_state(json.dumps(value).encode(), self.config)

    def test_missing_file_and_entries_use_seed_defaults(self):
        front, rear = self.initial.outputs["front"], self.initial.outputs["rear"]
        self.assertEqual(front.station, self.config.stations[0])
        self.assertEqual(front.intention, "play")
        self.assertEqual(front.gain_db, -7.1)
        self.assertEqual(front.resolved_url, front.station.url)
        self.assertIsNone(rear.station)
        self.assertIsNone(rear.resolved_url)
        self.assertIsNone(rear.managed_song_id)
        self.assertEqual(rear.intention, "stop")
        self.assertEqual(rear.gain_db, 0.0)
        self.assertEqual(self.initial.favorites, self.config.stations)
        self.assertEqual(self.parse({}), self.initial)
        self.assertEqual(self.parse({"favorites": []}).favorites, ())

    def test_unknown_outputs_dropped_and_missing_outputs_defaulted(self):
        stored = self.initial.to_json()
        stored["outputs"].pop("rear")
        stored["outputs"]["removed-output"] = {"invalid": "ignored"}
        self.assertEqual(self.parse(stored), self.initial)

    def test_saved_values_override_changed_seed_defaults(self):
        selected = Station.tunein("s99", "New Radio")
        saved = self.initial.with_output("front", station=selected, gain_db=-12.5,
                                         intention="pause", resolved_url="https://example.org/live",
                                         managed_song_id="9")
        config = configuration()
        config["outputs"][0]["default_gain_db"] = -2
        config["outputs"][0].pop("default_station")
        changed = outputconfig.parse_configuration(json.dumps(config).encode())
        self.assertEqual(state.parse_state(json.dumps(saved.to_json()).encode(), changed), saved)
        self.assertEqual(self.initial.outputs["front"].gain_db, -7.1)

    def test_stationless_output_must_remain_stopped(self):
        for intention in ("pause", "play", "invalid"):
            stored = self.initial.to_json()
            stored["outputs"]["rear"]["intention"] = intention
            with self.subTest(intention=intention), self.assertRaises(ValueError):
                self.parse(stored)

    def test_invalid_or_legacy_state_is_not_silently_reset(self):
        for stored in (None, [], {"front": "play"}, {"outputs": []}, {"favorites": None}):
            with self.subTest(stored=stored), self.assertRaises(ValueError):
                self.parse(stored)
        for field, value in (("intention", "invalid"), ("gain_db", True), ("gain_db", 1),
                             ("resolved_url", "file:///radio"), ("managed_song_id", "invalid"),
                             ("managed_song_id", 8), ("managed_song_id", "9" * 11)):
            stored = self.initial.to_json()
            stored["outputs"]["front"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.parse(stored)
        for raw in (b"{", b"\xff"):
            with self.assertRaises((ValueError, UnicodeError)):
                state.parse_state(raw, self.config)

    def test_output_state_requires_exact_fields(self):
        for extra in (False, True):
            stored = self.initial.to_json()
            if extra:
                stored["outputs"]["front"]["unexpected"] = 1
            else:
                stored["outputs"]["front"].pop("managed_song_id")
            with self.assertRaisesRegex(ValueError, "invalid output state"):
                self.parse(stored)

    def test_favorites_duplicate_noop_capacity_remove_and_order(self):
        full = self.initial
        for index in range(18):
            full = full.add_favorite(Station.tunein(f"s{index}", f"Radio {index}"))
        self.assertEqual(len(full.favorites), 20)
        self.assertIs(full.add_favorite(full.favorites[0]), full)
        with self.assertRaises(state.FavoritesFull):
            full.add_favorite(Station.tunein("s999", "Overflow"))
        moved = full.move_favorite("tunein:s17", 0)
        self.assertEqual(moved.favorites[0].key, "tunein:s17")
        self.assertEqual(moved.favorites[1:], full.favorites[:-1])
        removed = moved.remove_favorite("radio")
        self.assertNotIn("radio", [station.key for station in removed.favorites])
        self.assertEqual(removed.outputs["front"], self.initial.outputs["front"])
        self.assertEqual(full.favorites[0].key, "radio")
        for operation in (full.remove_favorite, lambda key: full.move_favorite(key, 0), full.favorite):
            with self.assertRaises(KeyError):
                operation("absent")
        for index in (-1, 20, True):
            with self.subTest(index=index), self.assertRaises(ValueError):
                full.move_favorite("radio", index)

    def test_atomic_state_roundtrip_preserves_mode_and_read_returns_raw_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "playback.json"
            missing, raw = state.read_state(path, self.config)
            self.assertEqual(missing, self.initial)
            self.assertIsNone(raw)
            state.write_state(path, self.initial)
            path.chmod(0o640)
            updated = self.initial.with_output("front", gain_db=-6)
            state.write_state(path, updated)
            observed, raw = state.read_state(path, self.config)
            self.assertEqual(observed, updated)
            self.assertEqual(raw, path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual([item.name for item in path.parent.iterdir()], ["playback.json"])

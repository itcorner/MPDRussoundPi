import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from playbackfixtures import configuration

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    outputconfig = importlib.import_module("lib.outputconfig")
    stations = importlib.import_module("lib.stations")


class OutputConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.raw = configuration()

    def parse(self):
        return outputconfig.parse_configuration(json.dumps(self.raw).encode())

    def test_outputs_defaults_and_station_references(self):
        config = self.parse()
        self.assertEqual(config.raw, self.raw)
        self.assertIsInstance(config.outputs, tuple)
        self.assertEqual(config.output("front").default_station, config.stations[0])
        self.assertIsNone(config.output("rear").default_station)
        self.assertEqual(config.output("rear").default_gain_db, 0.0)
        with self.assertRaises(KeyError):
            config.output("absent")

    def test_old_streams_format_is_rejected_with_migration_message(self):
        self.raw["streams"] = []
        with self.assertRaises(ValueError) as caught:
            self.parse()
        self.assertEqual(str(caught.exception), "old config format; see docs/documentation/mpd-headless-deployment.md")

    def test_invalid_json_and_nonobject_are_rejected(self):
        for raw in (b"{", b"\xff", b"[]", b"null"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                outputconfig.parse_configuration(raw)

    def test_station_limit_and_duplicate_keys(self):
        self.raw["stations"] = [stations.Station.tunein(f"s{i}", f"Radio {i}").to_json() for i in range(21)]
        self.raw["outputs"][0].pop("default_station")
        with self.assertRaisesRegex(ValueError, "At most 20"):
            self.parse()
        self.raw["stations"] = [configuration()["stations"][0]] * 2
        with self.assertRaisesRegex(ValueError, "keys must be unique"):
            self.parse()

    def test_unknown_default_station_is_rejected(self):
        for value in ("missing", 1, True, {}):
            with self.subTest(value=value):
                self.raw["outputs"][0]["default_station"] = value
                with self.assertRaisesRegex(ValueError, "default_station"):
                    self.parse()

    def test_unique_id_port_and_sink(self):
        for field in ("id", "port", "sink"):
            with self.subTest(field=field):
                self.raw = configuration()
                self.raw["outputs"][1][field] = self.raw["outputs"][0][field]
                with self.assertRaisesRegex(ValueError, "unique"):
                    self.parse()

    def test_unknown_output_fields_rejected(self):
        for field, value in (("stream", "https://example.org"), ("gain_db", -3),
                             ("name", "old"), ("queue_managed", False)):
            self.raw = configuration()
            self.raw["outputs"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unsupported output fields"):
                self.parse()

    def test_invalid_ports_sinks_labels_and_gain(self):
        for field, value in (("port", True), ("port", 0), ("port", 65536), ("port", "6601"),
                             ("sink", "missing"), ("label", ""), ("id", "Upper"),
                             ("default_gain_db", 1), ("default_gain_db", True),
                             ("default_gain_db", float("nan"))):
            self.raw = configuration()
            self.raw["outputs"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.parse()

    def test_empty_or_nonlist_outputs_rejected(self):
        for value in ([], None, {}, [None]):
            self.raw["outputs"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse()

    def test_watchdog_defaults_apply_without_section(self):
        config = self.parse()
        self.assertEqual(config.watchdog, outputconfig.WatchdogConfig())
        self.assertTrue(config.watchdog.enabled)
        self.assertEqual(config.watchdog.stall_threshold_s, 120.0)

    def test_watchdog_overrides_merge_with_defaults(self):
        self.raw["watchdog"] = {"enabled": False, "stall_threshold_s": 60, "restart_limit": 5}
        config = self.parse()
        self.assertFalse(config.watchdog.enabled)
        self.assertEqual(config.watchdog.stall_threshold_s, 60.0)
        self.assertEqual(config.watchdog.restart_limit, 5)
        self.assertEqual(config.watchdog.probe_interval_s, 10.0)

    def test_watchdog_unknown_fields_rejected(self):
        self.raw["watchdog"] = {"stall_seconds": 60}
        with self.assertRaisesRegex(ValueError, "unsupported watchdog fields"):
            self.parse()

    def test_watchdog_invalid_values_rejected(self):
        for field, value in (("enabled", 1), ("unresponsive_probes", 0),
                             ("unresponsive_probes", True), ("restart_limit", 101),
                             ("restart_limit", 2.0), ("probe_interval_s", 0),
                             ("stall_threshold_s", float("nan")), ("restart_window_s", -1),
                             ("restart_grace_s", -1), ("escalation_window_s", "3600"),
                             ("probe_interval_s", True), ("stall_threshold_s", 86401)):
            with self.subTest(field=field, value=value):
                self.raw["watchdog"] = {field: value}
                with self.assertRaises(ValueError):
                    self.parse()
        self.raw["watchdog"] = []
        with self.assertRaisesRegex(ValueError, "watchdog must be an object"):
            self.parse()

    def test_watchdog_grace_may_be_zero(self):
        self.raw["watchdog"] = {"restart_grace_s": 0}
        self.assertEqual(self.parse().watchdog.restart_grace_s, 0.0)


class StationTests(unittest.TestCase):
    def test_url_roundtrip_and_public_view_never_exposes_url(self):
        value = configuration()["stations"][0]
        station = stations.Station.parse(value)
        self.assertEqual(station.to_json(), value)
        self.assertEqual(station.public(), {
            "key": "radio", "name": "Private Radio", "type": "url", "station_id": None,
        })
        self.assertNotIn(value["url"], json.dumps(station.public()))
        with self.assertRaises(ValueError):
            stations.Station.parse(value, allow_url=False)

    def test_tunein_roundtrip_and_canonical_key(self):
        station = stations.Station.tunein("s8007", "Radio")
        self.assertEqual(station.key, "tunein:s8007")
        self.assertEqual(stations.Station.parse(station.to_json()), station)
        self.assertNotIn("url", station.to_json())
        wrong = dict(station.to_json(), key="different")
        with self.assertRaisesRegex(ValueError, "key must be"):
            stations.Station.parse(wrong)

    def test_invalid_station_types_fields_names_and_urls(self):
        url = configuration()["stations"][0]
        tunein = configuration()["stations"][1]
        for value in (None, [], {}, dict(url, key="Upper"), dict(url, url="file:///radio"),
                      dict(url, name=""), dict(url, name="a" * 121), dict(url, name="a\nb"),
                      dict(url, unexpected=True), dict(url, type="unknown"),
                      dict(tunein, url="https://example.org"), dict(tunein, station_id="invalid")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stations.Station.parse(value)

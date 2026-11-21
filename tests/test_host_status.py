from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web import host_status
from web.playback_status import HOST_POLL_INTERVAL_SECONDS, PlaybackAgentConfig, PlaybackStatusClient
from web.russound_server import RussoundHTTPServer

FIXTURES = json.loads((Path(__file__).parent / "fixtures/host-info-v1.json").read_text())
PLAYBACK = json.loads((Path(__file__).parent / "fixtures/playback-read-v1.json").read_text())


def response(**changes):
    document = copy.deepcopy(FIXTURES["agent_response"])
    document.update(changes)
    return document


def with_value(section, **values):
    document = response()
    document[section]["value"].update(values)
    return document


class ValidationTests(unittest.TestCase):
    def test_the_reference_document_is_accepted_and_reduced_to_the_allowlist(self):
        sections = host_status.validate_host_response(response())
        self.assertEqual(set(sections), set(host_status.SECTIONS))
        for name, probe in sections.items():
            self.assertEqual(set(probe), {"status", "age_ms", "stale", "error", "value"}, name)
            self.assertEqual(probe["status"], "ok")
        self.assertEqual(sections["network"]["value"]["addresses"], [{"family": 4, "address": "192.168.1.105", "prefix": 24}])
        self.assertEqual(sections["memory"]["value"]["swap_used_bytes"], 258654208)

    def test_unknown_fields_and_sections_are_never_forwarded(self):
        document = with_value("network", ssid="heimnetz")
        document["secrets"] = {"status": "ok", "value": {"token": "x"}}
        sections = host_status.validate_host_response(document)
        self.assertNotIn("secrets", sections)
        self.assertEqual(sections["network"]["status"], "error", "a section carrying an extra field is dropped whole")
        self.assertIsNone(sections["network"]["value"])
        self.assertNotIn("heimnetz", json.dumps(sections))
        self.assertEqual(sections["cpu"]["status"], "ok", "other sections are unaffected")

    def test_invalid_values_degrade_only_their_section(self):
        bad = {
            "system": {"model": "x\u0007y"}, "cpu": {"busy_percent": 101}, "memory": {"available_bytes": 10**15},
            "temperature": {"celsius": float("nan")}, "disk": {"free_bytes": -1},
            "clock": {"synchronized": 1},
        }
        for section, values in bad.items():
            with self.subTest(section=section):
                sections = host_status.validate_host_response(with_value(section, **values))
                self.assertEqual(sections[section]["status"], "error")
                self.assertEqual(sections[section]["error"], {"code": "invalid_agent_response", "retryable": True})
                self.assertEqual(sections["network"]["status"], "ok")

    def test_numbers_must_be_numbers_not_booleans_or_text(self):
        for value in (True, "5", None, [1]):
            sections = host_status.validate_host_response(with_value("cpu", frequency_mhz=value))
            if value is None:
                self.assertEqual(sections["cpu"]["status"], "ok")
            else:
                self.assertEqual(sections["cpu"]["status"], "error", repr(value))

    def test_addresses_are_real_non_local_addresses(self):
        for address, family in (("127.0.0.1", 4), ("169.254.1.2", 4), ("fe80::1", 6), ("224.0.0.1", 4),
                                ("0.0.0.0", 4), ("not-an-address", 4), ("192.168.1.5", 6), ("2001:db8::1", 4)):
            document = with_value("network", addresses=[{"family": family, "address": address, "prefix": 24}])
            with self.subTest(address=address):
                self.assertEqual(host_status.validate_host_response(document)["network"]["status"], "error")
        ok = with_value("network", addresses=[{"family": 6, "address": "2001:DB8::0001", "prefix": 64}])
        self.assertEqual(host_status.validate_host_response(ok)["network"]["value"]["addresses"][0]["address"], "2001:db8::1")
        many = with_value("network", addresses=[{"family": 4, "address": f"10.0.0.{n}", "prefix": 8} for n in range(1, 10)])
        self.assertEqual(host_status.validate_host_response(many)["network"]["status"], "error", "at most 8 addresses")
        self.assertIsNone(host_status.validate_host_response(with_value("network", addresses=None))["network"]["value"]["addresses"])

    def test_agent_probe_errors_are_reduced_to_known_codes_without_agent_text(self):
        document = response(power={
            "status": "error", "attempted_at": "x", "observed_at": None, "age_ms": None, "stale": True,
            "error": {"code": "secret_code", "message": "token=abc /private/path", "retryable": True},
            "value": None, "last_known": None,
        })
        power = host_status.validate_host_response(document)["power"]
        self.assertEqual(power["error"], {"code": "probe_failed", "retryable": True})
        self.assertNotIn("abc", json.dumps(power))
        for status in ("pending", "unsupported"):
            document["power"] = {"status": status, "age_ms": None, "stale": True, "error": None, "value": None}
            probe = host_status.validate_host_response(document)["power"]
            self.assertEqual((probe["status"], probe["value"], probe["stale"]), (status, None, True))

    def test_garbage_documents_do_not_raise(self):
        for garbage in ({}, {"system": 5}, {"system": {"status": "weird"}}, {name: None for name in host_status.SECTIONS}):
            sections = host_status.validate_host_response(garbage)
            self.assertTrue(all(probe["status"] == "error" for probe in sections.values()))


class AssessmentTests(unittest.TestCase):
    def labels(self, **changes):
        sections = host_status.validate_host_response(response())
        for section, values in changes.items():
            sections[section]["value"].update(values)
        return host_status.assess(sections)

    def test_wifi_thresholds(self):
        def wifi(dbm):
            document = with_value("network", wifi={"signal_dbm": dbm, "link_rate_mbps": None, "band": None})
            return host_status.assess(host_status.validate_host_response(document))["wifi"]
        self.assertEqual(wifi(-60), {"level": "ok", "label": "good"})
        self.assertEqual(wifi(-61), {"level": "warning", "label": "fair"})
        self.assertEqual(wifi(-70), {"level": "warning", "label": "fair"})
        self.assertEqual(wifi(-71), {"level": "warning", "label": "weak"})

    def test_memory_temperature_power_and_clock(self):
        total = 1000
        self.assertEqual(self.labels(memory={"total_bytes": total, "available_bytes": 100})["memory"]["label"], "ok")
        self.assertEqual(self.labels(memory={"total_bytes": total, "available_bytes": 99})["memory"]["label"], "low")
        self.assertEqual(self.labels(temperature={"celsius": 69.9})["temperature"]["level"], "ok")
        self.assertEqual(self.labels(temperature={"celsius": 70.0})["temperature"], {"level": "warning", "label": "high"})
        self.assertEqual(self.labels(temperature={"celsius": 80.0})["temperature"], {"level": "problem", "label": "critical"})
        flags = {"under_voltage": False, "frequency_capped": False, "throttled": False, "soft_temperature_limit": False}
        self.assertEqual(self.labels(power={"since_boot": dict(flags, throttled=True)})["power"],
                         {"level": "warning", "label": "occurred since boot"})
        self.assertEqual(self.labels(power={"now": dict(flags, under_voltage=True)})["power"],
                         {"level": "problem", "label": "problem now"})
        self.assertEqual(self.labels(clock={"synchronized": False})["clock"]["level"], "warning")

    def test_network_state_and_overall(self):
        no_uplink = with_value("network", state="no_uplink", interface_type=None, addresses=[], wifi=None)
        assessment = host_status.assess(host_status.validate_host_response(no_uplink))
        self.assertEqual(assessment["network"], {"level": "problem", "label": "no uplink"})
        self.assertNotIn("wifi", assessment)
        self.assertEqual(host_status.overall(assessment), "problem")
        reference = host_status.assess(host_status.validate_host_response(response()))
        self.assertEqual(host_status.overall(reference), "warning", "weak Wi-Fi is the worst item on the reference Pi")
        pending = {name: {"status": "pending", "age_ms": None, "stale": True, "error": None, "value": None}
                   for name in host_status.SECTIONS}
        unknown = host_status.assess(pending)
        self.assertEqual(host_status.overall(unknown), "unknown")
        self.assertEqual(unknown["network"]["level"], "unknown")


class ViewTests(unittest.TestCase):
    def test_reference_view_is_plain_text_without_withheld_facts(self):
        sections = host_status.validate_host_response(response())
        view = host_status.snapshot_view(sections)["view"]
        self.assertEqual(view["summary"], ["Wi-Fi weak (-76 dBm)", "CPU 33 %", "Memory 51 % used", "44 °C"])
        rows = {row["label"]: row["value"] for row in view["rows"]}
        self.assertEqual(rows["Addresses"], "192.168.1.105/24")
        self.assertEqual(rows["Wi-Fi link"], "-76 dBm (weak), 270 Mbit/s, 5 GHz")
        self.assertEqual(rows["Uptime"], "5 d 1 h 35 min")
        self.assertEqual(rows["Power and throttling"], "no issues")
        self.assertEqual(rows["Disk"], "37.8 GiB free of 58.0 GiB")
        self.assertTrue(all(isinstance(item, str) for row in view["rows"] for item in row.values()))
        text = json.dumps(view)
        for forbidden in FIXTURES["forbidden_text"]:
            self.assertNotIn(forbidden, text)

    def test_degraded_hosts_still_render(self):
        document = response()
        document["power"] = {"status": "unsupported", "age_ms": None, "stale": True, "error": None, "value": None}
        document["network"] = {"status": "error", "age_ms": None, "stale": True,
                               "error": {"code": "probe_failed"}, "value": None}
        view = host_status.snapshot_view(host_status.validate_host_response(document))["view"]
        labels = [row["label"] for row in view["rows"]]
        self.assertNotIn("Network", labels)
        self.assertNotIn("Power and throttling", labels)
        self.assertIn("Memory", labels)


class AgentClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        token = Path(self.directory.name) / "token"
        token.write_text("mock-value-" + "x" * 32 + "\n")
        self.client = PlaybackStatusClient(PlaybackAgentConfig("http://127.0.0.1:8081", "musicpi", str(token)))
        self.health = {"api_version": 1, "agent_id": "musicpi", "epoch": "e", "status": "alive"}
        self.features = ["inventory", "playback", "now_playing", "audio_diagnostics", "host_info"]
        self.host_response: object = response()
        self.paths: list[str] = []
        self.clock = [1000.0]
        patcher = patch("web.playback_status.time.monotonic", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(self.client, "_get_json", side_effect=self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, path):
        self.paths.append(path)
        if path == "/v1/health":
            return self.health
        if path == "/v1/capabilities":
            return {"api_version": 1, "agent_id": "musicpi", "epoch": "e", "read_features": self.features, "write_operations": []}
        if path == "/v1/outputs":
            return PLAYBACK["healthy_inventory"]
        if path == "/v1/host":
            if isinstance(self.host_response, Exception):
                raise self.host_response
            return self.host_response
        raise AssertionError(path)

    def test_host_is_fetched_when_advertised_and_validated(self):
        self.assertTrue(self.client.poll_once())
        self.assertIn("/v1/host", self.paths)
        host = self.client.host_snapshot()
        self.assertEqual((host["status"], host["agent_id"], host["stale"]), ("ok", "musicpi", False))
        self.assertEqual(host["host"]["view"]["summary"][0], "Wi-Fi weak (-76 dBm)")
        self.assertEqual(host["error"], None)

    def test_older_agents_without_the_capability_are_unsupported_and_never_asked(self):
        self.features = ["inventory", "playback"]
        self.client.poll_once()
        self.assertNotIn("/v1/host", self.paths)
        host = self.client.host_snapshot()
        self.assertEqual((host["status"], host["host"]), ("unsupported", None))

    def test_host_data_does_not_touch_the_playback_revision(self):
        self.client.poll_once()
        revision = self.client.snapshot()["revision"]
        self.clock[0] += HOST_POLL_INTERVAL_SECONDS + 1
        changed = response()
        changed["cpu"]["value"]["busy_percent"] = 90.5
        self.host_response = changed
        self.client.poll_once()
        self.assertEqual(self.client.host_snapshot()["host"]["sections"]["cpu"]["value"]["busy_percent"], 90.5)
        self.assertEqual(self.client.snapshot()["revision"], revision, "playback consumers must not wake for CPU changes")

    def test_host_is_polled_at_its_own_interval(self):
        self.client.poll_once()
        self.client.poll_once()
        self.assertEqual(self.paths.count("/v1/host"), 1)
        self.clock[0] += HOST_POLL_INTERVAL_SECONDS + 0.1
        self.client.poll_once()
        self.assertEqual(self.paths.count("/v1/host"), 2)

    def test_a_failing_host_route_does_not_make_playback_unavailable_and_serves_no_old_values(self):
        self.client.poll_once()
        self.clock[0] += HOST_POLL_INTERVAL_SECONDS + 1
        self.host_response = TimeoutError()
        self.assertTrue(self.client.poll_once(), "playback polling is unaffected")
        self.assertEqual(self.client.snapshot()["agents"][0]["status"], "ok")
        host = self.client.host_snapshot()
        self.assertEqual((host["status"], host["host"], host["error"]["code"]), ("unavailable", None, "agent_timeout"))

    def test_invalid_agent_identity_or_shape_is_an_error_not_data(self):
        self.host_response = response(agent_id="other")
        self.client.poll_once()
        self.assertEqual(self.client.host_snapshot()["error"]["code"], "agent_identity_mismatch")
        self.clock[0] += HOST_POLL_INTERVAL_SECONDS + 1
        self.host_response = ValueError("invalid_agent_response")  # what _get_json raises for a non-object body
        self.client.poll_once()
        host = self.client.host_snapshot()
        self.assertEqual((host["status"], host["host"], host["error"]["code"]), ("unavailable", None, "invalid_agent_response"))
        # A well-formed document whose sections are garbage still yields a host, with every section degraded.
        self.clock[0] += HOST_POLL_INTERVAL_SECONDS + 1
        self.host_response = response(cpu="garbage", memory=None)
        self.client.poll_once()
        sections = self.client.host_snapshot()["host"]["sections"]
        self.assertEqual((sections["cpu"]["status"], sections["memory"]["status"], sections["system"]["status"]),
                         ("error", "error", "ok"))

    def test_unreachable_agent_marks_the_host_unavailable(self):
        self.health = {"api_version": 1, "agent_id": "wrong", "epoch": "e", "status": "alive"}
        self.assertFalse(self.client.poll_once())
        self.assertEqual(self.client.host_snapshot()["status"], "unavailable")

    def test_effective_age_grows_and_old_data_is_flagged_stale(self):
        self.client.poll_once()
        first = self.client.host_snapshot()["host"]["sections"]["memory"]["age_ms"]
        self.clock[0] += 12
        later = self.client.host_snapshot()
        self.assertEqual(later["host"]["sections"]["memory"]["age_ms"], first + 12000)
        self.assertFalse(later["stale"])
        self.clock[0] += host_status_stale_seconds()
        self.assertTrue(self.client.host_snapshot()["stale"])


def host_status_stale_seconds():
    from web.playback_status import HOST_STALE_AFTER_SECONDS
    return HOST_STALE_AFTER_SECONDS


class StatusEndpointTests(unittest.TestCase):
    def test_endpoint_lists_validated_hosts_and_never_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("mock-value-" + "x" * 32 + "\n")
            client = PlaybackStatusClient(PlaybackAgentConfig("http://127.0.0.1:8081", "musicpi", str(token)))
            client._set_host("ok", host=host_status.snapshot_view(host_status.validate_host_response(response())))
            server = RussoundHTTPServer(None, None, playback_clients=[client])
            try:
                with patch.object(client, "_get_json", side_effect=AssertionError("a request must not probe")):
                    body = server.app.test_client().get("/api/status/host").json
                self.assertEqual((body["api_version"], body["configured"]), (1, True))
                host = body["hosts"][0]
                self.assertEqual((host["agent_id"], host["status"]), ("musicpi", "ok"))
                self.assertEqual(host["host"]["view"]["overall"], "warning")
                text = json.dumps(body)
                for forbidden in FIXTURES["forbidden_text"]:
                    self.assertNotIn(forbidden, text)
            finally:
                server.server_close()

    def test_endpoint_without_agents(self):
        server = RussoundHTTPServer(None, None)
        try:
            self.assertEqual(server.app.test_client().get("/api/status/host").json,
                             {"api_version": 1, "configured": False, "hosts": []})
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()

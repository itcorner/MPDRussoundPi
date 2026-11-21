import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from mpdrussoundpi.russound.lib import hostinfo
from mpdrussoundpi.russound.lib.hostinfo import HostCollector

FIXTURE = json.loads((Path(__file__).parent / "fixtures/host-info-v1.json").read_text())


class Clock:
    def __init__(self):
        self.wall = 1_795_000_000.0
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class ParserTests(unittest.TestCase):
    def test_loadavg_and_meminfo(self):
        self.assertEqual(hostinfo.parse_loadavg(FIXTURE["sources"]["/proc/loadavg"]), (1.31, 1.03, 0.96))
        memory = hostinfo.parse_meminfo(FIXTURE["sources"]["/proc/meminfo"])
        self.assertEqual((memory["MemTotal"], memory["MemAvailable"]), (927444 * 1024, 452448 * 1024))
        self.assertEqual(memory["SwapTotal"] - memory["SwapFree"], (926716 - 674124) * 1024)
        self.assertEqual(hostinfo.parse_meminfo("MemTotal: 10 kB\nMemAvailable: 5 kB\n")["SwapTotal"], 0)
        with self.assertRaises(ValueError):
            hostinfo.parse_meminfo("MemTotal: 10 kB\n")

    def test_cpu_busy_is_a_delta_and_never_a_spike(self):
        first = hostinfo.parse_cpu_times(FIXTURE["sources"]["/proc/stat"])
        second = hostinfo.parse_cpu_times(FIXTURE["sources"]["/proc/stat_next"])
        self.assertIsNone(hostinfo.cpu_busy_percent(None, first))
        # busy 1550 -> 2160 jiffies and total 9650 -> 10780: 610 of 1130
        self.assertEqual(hostinfo.cpu_busy_percent(first, second), 54.0)
        self.assertIsNone(hostinfo.cpu_busy_percent(second, first), "counter went backwards")
        self.assertIsNone(hostinfo.cpu_busy_percent(first, first), "no time passed")

    def test_throttle_flags_decode_now_and_since_boot(self):
        self.assertEqual(hostinfo.parse_throttled("throttled=0x0\n")["now"]["throttled"], False)
        decoded = hostinfo.parse_throttled("throttled=0x50005")
        self.assertEqual(decoded["raw"], "0x50005")
        self.assertTrue(decoded["now"]["under_voltage"] and decoded["now"]["throttled"])
        self.assertFalse(decoded["now"]["frequency_capped"])
        self.assertTrue(decoded["since_boot"]["under_voltage"] and decoded["since_boot"]["throttled"])
        self.assertFalse(decoded["since_boot"]["soft_temperature_limit"])
        for bad in ("", "throttled=zz", "error"):
            with self.assertRaises(ValueError):
                hostinfo.parse_throttled(bad)

    def test_default_interface_prefers_the_lowest_metric_and_falls_back_to_ipv6(self):
        route = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n"
                 "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
                 "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n")
        self.assertEqual(hostinfo.parse_default_interface(route), "eth0")
        self.assertEqual(hostinfo.parse_default_interface(FIXTURE["sources"]["/proc/net/route"]), "wlan0")
        self.assertIsNone(hostinfo.parse_default_interface("Iface\tDestination\n"))
        ipv6 = "0" * 32 + " 00 " + "0" * 32 + " 00 " + "f" * 31 + "1 00000400 00000001 00000000 00000003 wlan0\n"
        self.assertEqual(hostinfo.parse_default_interface("Iface\n", ipv6), "wlan0")

    def test_wireless_signal_link_and_addresses(self):
        self.assertEqual(hostinfo.parse_wireless_dbm(FIXTURE["sources"]["/proc/net/wireless"], "wlan0"), -76)
        self.assertIsNone(hostinfo.parse_wireless_dbm(FIXTURE["sources"]["/proc/net/wireless"], "wlan1"))
        self.assertEqual(hostinfo.parse_link(FIXTURE["commands"][
            "nmcli -t -f IN-USE,FREQ,RATE device wifi list ifname wlan0 --rescan no"]), ("5 GHz", 270.0))
        self.assertEqual(hostinfo.parse_link(":2437 MHz:130 Mbit/s\n"), (None, None))
        self.assertEqual(hostinfo.parse_link("*:2437 MHz:65 Mbit/s")[0], "2.4 GHz")
        self.assertEqual(hostinfo.parse_link("*:6115 MHz:1200 Mbit/s")[0], "6 GHz")
        # Only global addresses; link-local IPv6 is never reported.
        self.assertEqual(hostinfo.parse_addresses(FIXTURE["commands"]["ip -j addr show dev wlan0"]),
                         [{"family": 4, "address": "192.168.1.105", "prefix": 24}])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for path, content in FIXTURE["sources"].items():
            if path.endswith("_next"):
                continue
            target = self.root / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        (self.root / "sys/class/net/wlan0/wireless").mkdir(parents=True)
        self.clock = Clock()
        self.calls: list[list[str]] = []
        self.tools = {"vcgencmd", "timedatectl", "nmcli", "ip"}

    def fake_run(self, argv):
        self.calls.append(list(argv))
        try:
            return FIXTURE["commands"][" ".join(argv)]
        except KeyError:
            raise subprocess.CalledProcessError(1, argv, stderr="secret detail") from None

    def collector(self, **overrides):
        options = dict(root=self.root, run=self.fake_run, wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono,
                       which=lambda name: f"/usr/bin/{name}" if name in self.tools else None,
                       statvfs=lambda path: type("Usage", (), {
                           "f_frsize": 4096, "f_blocks": 62239682560 // 4096, "f_bavail": 40565358592 // 4096})())
        options.update(overrides)
        return HostCollector(**options)

    def refresh_all(self, collector):
        for name in hostinfo.SCHEMA_SECTIONS:
            collector.refresh(name)

    def test_probe_objects_follow_the_read_contract(self):
        collector = self.collector()
        pending = collector.snapshot()
        self.assertTrue(all(pending[name]["status"] == "pending" and pending[name]["stale"] for name in hostinfo.SCHEMA_SECTIONS))
        self.refresh_all(collector)
        self.clock.advance(2)
        snapshot = collector.snapshot()
        expected = FIXTURE["agent_response"]
        for name in hostinfo.SCHEMA_SECTIONS:
            with self.subTest(section=name):
                probe = snapshot[name]
                self.assertEqual(set(probe), set(expected[name]))
                self.assertEqual(probe["status"], "ok")
                self.assertFalse(probe["stale"])
                self.assertEqual(probe["age_ms"], 2000)
                self.assertEqual(set(probe["value"]), set(expected[name]["value"]))
        self.assertEqual(snapshot["system"]["value"], expected["system"]["value"] | {"uptime_seconds": 437704})
        self.assertEqual(snapshot["memory"]["value"]["swap_used_bytes"], 258654208)
        self.assertEqual(snapshot["network"]["value"], expected["network"]["value"])
        self.assertEqual(snapshot["temperature"]["value"], {"celsius": 44.3})
        self.assertEqual(snapshot["clock"]["value"], {"synchronized": True})
        self.assertEqual(snapshot["power"]["value"]["raw"], "0x0")

    def test_cpu_percent_appears_after_the_second_sample_only(self):
        collector = self.collector()
        collector.refresh("cpu")
        self.assertIsNone(collector.snapshot()["cpu"]["value"]["busy_percent"])
        (self.root / "proc/stat").write_text(FIXTURE["sources"]["/proc/stat_next"])
        self.clock.advance(5)
        collector.refresh("cpu")
        self.assertEqual(collector.snapshot()["cpu"]["value"]["busy_percent"], 54.0)

    def test_nothing_the_page_must_not_show_is_read_or_reported(self):
        collector = self.collector()
        self.refresh_all(collector)
        text = json.dumps(collector.snapshot())
        for forbidden in FIXTURE["forbidden_text"]:
            self.assertNotIn(forbidden, text)
        # Only fixed commands, no shell, no scan and never the network name.
        for argv in self.calls:
            self.assertIn(argv[0], {"vcgencmd", "timedatectl", "ip", "nmcli"})
            self.assertTrue(all(isinstance(part, str) and ";" not in part and " " not in part for part in argv))
        nmcli = next(argv for argv in self.calls if argv[0] == "nmcli")
        self.assertEqual(nmcli[nmcli.index("--rescan") + 1], "no")
        self.assertEqual(nmcli[nmcli.index("-f") + 1], "IN-USE,FREQ,RATE")
        self.assertNotIn("SSID", " ".join(nmcli))

    def test_failures_are_sanitized_and_do_not_affect_other_sections(self):
        def broken(argv):
            raise subprocess.CalledProcessError(1, argv, stderr="secret detail 192.168.1.50")
        collector = self.collector(run=broken)
        self.refresh_all(collector)
        snapshot = collector.snapshot()
        self.assertEqual(snapshot["power"]["status"], "error")
        self.assertEqual(snapshot["power"]["error"], {"code": "probe_failed", "message": "The host probe failed.", "retryable": True})
        self.assertTrue(snapshot["power"]["stale"])
        self.assertIsNone(snapshot["power"]["value"])
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertEqual(snapshot["cpu"]["status"], "ok")
        # Addresses and link details degrade without failing the whole network section.
        network = snapshot["network"]
        self.assertEqual(network["status"], "ok")
        self.assertIsNone(network["value"]["addresses"])
        self.assertEqual(network["value"]["wifi"], {"signal_dbm": -76, "link_rate_mbps": None, "band": None})

    def test_missing_tools_and_sensors_are_unsupported_not_zero(self):
        self.tools.clear()
        (self.root / "sys/class/thermal/thermal_zone0/temp").unlink()
        collector = self.collector()
        self.refresh_all(collector)
        snapshot = collector.snapshot()
        for name in ("power", "clock", "temperature"):
            self.assertEqual(snapshot[name]["status"], "unsupported", name)
            self.assertIsNone(snapshot[name]["value"])
            self.assertIsNone(snapshot[name]["error"])
        self.assertEqual(snapshot["network"]["value"]["wifi"]["link_rate_mbps"], None)

    def test_ethernet_hotspot_and_missing_uplink(self):
        shutil_path = self.root / "proc/net/route"
        shutil_path.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                               "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n")
        (self.root / "sys/class/net/eth0/device").mkdir(parents=True)
        (self.root / "sys/class/net/eth0/operstate").write_text("up\n")
        collector = self.collector(run=lambda argv: '[{"addr_info":[{"family":"inet","local":"10.0.0.7","prefixlen":8,"scope":"global"}]}]')
        collector.refresh("network")
        value = collector.snapshot()["network"]["value"]
        self.assertEqual((value["interface_type"], value["state"], value["wifi"]), ("ethernet", "connected", None))
        self.assertEqual(value["addresses"], [{"family": 4, "address": "10.0.0.7", "prefix": 8}])
        shutil_path.write_text("Iface\tDestination\tGateway\tFlags\n")
        collector.refresh("network")
        self.assertEqual(collector.snapshot()["network"]["value"], {
            "state": "no_uplink", "interface_type": None, "addresses": [], "wifi": None})
        shutil_path.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                               "wlan0;rm\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n")
        collector.refresh("network")
        self.assertEqual(collector.snapshot()["network"]["status"], "error", "unsafe interface names are never passed on")

    def test_staleness_follows_the_section_interval(self):
        collector = self.collector()
        collector.refresh("memory")
        self.clock.advance(hostinfo.INTERVALS["memory"] * hostinfo.STALE_FACTOR - 1)
        self.assertFalse(collector.snapshot()["memory"]["stale"])
        self.clock.advance(2)
        probe = collector.snapshot()["memory"]
        self.assertTrue(probe["stale"])
        self.assertEqual(probe["status"], "ok", "an old value stays visible but is flagged")

    def test_a_failed_refresh_never_serves_the_old_value_as_current(self):
        collector = self.collector()
        collector.refresh("memory")
        (self.root / "proc/meminfo").write_text("garbage")
        self.clock.advance(5)
        collector.refresh("memory")
        probe = collector.snapshot()["memory"]
        self.assertEqual((probe["status"], probe["value"], probe["last_known"]), ("error", None, None))

    def test_refresh_due_schedules_by_interval(self):
        collector = self.collector()
        collector.refresh_due()
        before = len(self.calls)
        collector.refresh_due()
        self.assertEqual(len(self.calls), before, "nothing is due immediately afterwards")
        self.clock.advance(hostinfo.INTERVALS["network"] + 1)
        collector.refresh_due()
        self.assertGreater(len(self.calls), before)

    def test_background_thread_collects_and_stops(self):
        collector = self.collector()
        collector.start()
        try:
            deadline = time.monotonic() + 5
            while collector.snapshot()["cpu"]["status"] == "pending" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(collector.snapshot()["cpu"]["status"], "ok")
        finally:
            collector.close()
        with self.assertRaises(RuntimeError):
            collector.start()


if __name__ == "__main__":
    unittest.main()

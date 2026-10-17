import ipaddress
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from jinja2 import Environment, StrictUndefined
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
sys.path.insert(0, str(SCRIPTS))
import playbackegress


class PlaybackEgressTests(unittest.TestCase):
    def test_rules_only_scope_the_persistent_mpd_cgroup(self):
        rules = playbackegress.ruleset(1000, ["192.168.1.1", "fd00::53"])
        self.assertIn('socket cgroupv2 level 4 "user.slice/user-1000.slice/user@1000.service/mpdstreams.slice" jump streams', rules)
        self.assertIn("ct direction reply return", rules)
        self.assertIn("ip daddr 192.168.1.1 udp dport 53 counter return", rules)
        self.assertIn("ip6 daddr fd00::53 udp dport 53 counter return", rules)
        self.assertIn("ip daddr @blocked4 counter reject", rules)
        self.assertIn("ip6 daddr @blocked6 counter reject", rules)
        self.assertNotIn("ct state established", rules)
        self.assertNotIn("flush ruleset", rules)
        self.assertIn("delete table inet mpdrussoundpi_egress", rules)

    def test_rule_addresses_match_shared_policy(self):
        rules = playbackegress.ruleset(1000, [])
        for version in (4, 6):
            networks = playbackegress.blocked_networks(version)
            self.assertTrue(networks)
            for network in networks:
                self.assertIn(str(network), rules)
            for value in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fd00::1", "fe80::1"):
                address = ipaddress.ip_address(value)
                if address.version == version:
                    self.assertTrue(any(address in network for network in networks))

    def test_invalid_uid_or_resolvers_cannot_generate_rules(self):
        for uid, resolvers in ((0, []), (1000, ["192.168.1.1; accept"]), (1000, ["router.local"])):
            with self.assertRaises(ValueError):
                playbackegress.ruleset(uid, resolvers)

    def test_startup_fails_closed_when_filter_is_not_active(self):
        with patch("playbackegress.time.monotonic", side_effect=[0, 0, 31]):
            with patch("playbackegress.time.sleep"), patch("playbackegress.subprocess.run") as run:
                run.return_value.returncode = 3
                with self.assertRaisesRegex(RuntimeError, "refusing to start"):
                    playbackegress.wait_for_filter()

    def test_startup_rejects_an_active_filter_for_a_recreated_cgroup(self):
        with patch("playbackegress.subprocess.run") as run:
            run.return_value.returncode = 0
            with patch("playbackegress.verify_scope", side_effect=RuntimeError("MPD cgroup changed")):
                with self.assertRaisesRegex(RuntimeError, "cgroup changed"):
                    playbackegress.wait_for_filter()

    def test_scope_record_must_match_current_uid_and_cgroup_inode(self):
        scope = Mock()
        scope.stat.return_value.st_ino = 123
        with patch("playbackegress.cgroup_path", return_value=scope):
            with patch.object(Path, "read_text", return_value='{"uid":1000,"cgroup_inode":123}'):
                playbackegress.verify_scope(1000)
            for state in ('{"uid":1000,"cgroup_inode":122}', '{"uid":1001,"cgroup_inode":123}', '{}'):
                with patch.object(Path, "read_text", return_value=state):
                    with self.assertRaisesRegex(RuntimeError, "cgroup changed"):
                        playbackegress.verify_scope(1000)

    def test_root_unit_reloads_when_user_manager_restarts(self):
        root = SCRIPTS.parents[1]
        template = root / "ansible/templates/playback-egress.service.j2"
        unit = Environment(undefined=StrictUndefined).from_string(template.read_text()).render(
            playback_egress_uid=1000, playback_egress_user="musicpi",
            playback_egress_install="/usr/local/lib/mpdrussoundpi-egress",
            playback_egress_resolvers=["192.168.1.1"],
        )
        self.assertIn("PartOf=user@1000.service", unit)
        self.assertIn("--resolver 192.168.1.1", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        play = yaml.safe_load((root / "ansible/playbooks/deploy-playback-egress.yml").read_text())[0]
        self.assertFalse(play["vars"]["playback_egress_activate"])
        self.assertTrue(play["become"])


if __name__ == "__main__":
    unittest.main()

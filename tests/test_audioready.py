import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    ready = importlib.import_module("lib.audioready")


class AudioReadinessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "config.json"
        self.config.write_text((SCRIPTS / "config.json").read_text())
        self.card = {"name": "alsa_card.usb-0d8c_USB_Sound_Device-00",
                     "active_profile": "output:analog-surround-51"}
        self.sinks = [{"name": name} for name in (
            "alsa_output.usb-0d8c_USB_Sound_Device-00.analog-surround-51",
            "sink.stereo_front", "sink.stereo_centerlfe", "sink.stereo_rear",
        )]

    def test_requires_correct_hardware_and_every_virtual_sink(self):
        with patch.object(ready, "_pactl", side_effect=[json.dumps([self.card]), json.dumps(self.sinks)]):
            ready.audio_ready(self.config)
        for sinks in ([], self.sinks[:-1]):
            with patch.object(ready, "_pactl", side_effect=[json.dumps([self.card]), json.dumps(sinks)]):
                with self.assertRaises(RuntimeError):
                    ready.audio_ready(self.config)

    def test_wrong_profile_is_selected_then_must_be_verified_again(self):
        self.card["active_profile"] = "output:analog-surround-71"
        with patch.object(ready, "_pactl", side_effect=[json.dumps([self.card]), json.dumps(self.sinks), ""]) as pactl:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                ready.audio_ready(self.config, enforce_profile=True)
        self.assertEqual(pactl.call_args.args,
                         ("set-card-profile", self.card["name"], "output:analog-surround-51"))

    def test_bounded_wait_and_transient_recovery(self):
        with patch.object(ready, "audio_ready", side_effect=[RuntimeError("not yet"), None]) as check:
            with patch.object(ready.time, "sleep"):
                ready.wait_for_audio(self.config)
        self.assertEqual(check.call_count, 2)
        with patch.object(ready, "audio_ready", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                ready.wait_for_audio(self.config, timeout=0)

    def test_invalid_configuration_does_not_retry(self):
        self.config.write_text("{}")
        with self.assertRaises(ValueError):
            ready.wait_for_audio(self.config)

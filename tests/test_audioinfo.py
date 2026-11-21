import copy
import json
from pathlib import Path
import unittest

from mpdrussoundpi.russound.lib import audioinfo
from mpdrussoundpi.russound.lib.outputconfig import parse_configuration
from playbackfixtures import configuration

FIXTURE = json.loads((Path(__file__).parent / "fixtures/audio-info-v1.json").read_text())


def live_like(**changes):
    """The reference Pi's shape: a USB selector, and a device name that embeds the serial and bus path."""
    config = copy.deepcopy(configuration())
    device = config["outputmappers"]["audio_devices"][0]
    device["name"] = "alsa_card.usb-C-Media_Electronics_Inc._USB_Audio_Device-SERIAL0123456789-bus-path"
    device["hardware-selector"] = {"vendor": "0d8c", "product": "0102"}
    device["private-note"] = "/dev/snd/controlC1"
    config.update(changes)
    return config


def describe(config):
    return audioinfo.describe_audio(parse_configuration(json.dumps(config).encode()))


class AudioDescriptionTests(unittest.TestCase):
    def test_description_matches_the_reference_document(self):
        config = live_like()
        # The reference fixture's sinks are the three stereo outputs of the seed layout.
        for output in config["outputs"]:
            output.pop("default_station", None)
        expected = FIXTURE["agent_response"]
        description = describe(config)
        self.assertEqual(description["devices"][0]["usb"], expected["devices"][0]["usb"])
        self.assertEqual(description["devices"][0]["profile"], "analog-surround-51")
        by_id = {sink["id"]: sink for sink in description["devices"][0]["sinks"]}
        self.assertEqual(by_id["stereo_centerlfe"], expected["devices"][0]["sinks"][1])
        self.assertEqual(set(by_id["stereo_front"]), set(expected["devices"][0]["sinks"][0]))
        self.assertEqual(description["outputs"], [{"id": "front", "sink": "stereo_front"}, {"id": "rear", "sink": "stereo_rear"}])

    def test_nothing_unit_specific_or_private_is_copied(self):
        text = json.dumps(describe(live_like()))
        for forbidden in FIXTURE["forbidden_text"]:
            self.assertNotIn(forbidden, text)
        description = describe(live_like())
        self.assertEqual(set(description), {"devices", "outputs"})
        self.assertEqual(set(description["devices"][0]), {"profile", "usb", "sinks"})
        self.assertEqual(set(description["devices"][0]["usb"]), {"vendor", "product"})

    def test_ids_are_four_digit_lowercase_hex(self):
        config = live_like()
        config["outputmappers"]["audio_devices"][0]["hardware-selector"] = {"vendor": "0D8C", "product": "12"}
        usb = describe(config)["devices"][0]["usb"]
        self.assertEqual(usb, {"vendor": "0d8c", "product": "0012"})

    def test_a_device_without_a_selector_has_no_usb_ids(self):
        config = configuration()
        self.assertIsNone(describe(config)["devices"][0]["usb"])

    def test_missing_or_malformed_parts_degrade_quietly(self):
        class Fake:
            def __init__(self, raw):
                self.raw, self.outputs = raw, ()
        for raw in ({}, {"outputmappers": 5}, {"outputmappers": {"audio_devices": "x"}},
                    {"outputmappers": {"audio_devices": [1, None, {"sinks": 3}]}}):
            result = audioinfo.describe_audio(Fake(raw))
            self.assertEqual(result["outputs"], [])
            self.assertTrue(all(device["sinks"] == [] for device in result["devices"]))
        odd = Fake({"outputmappers": {"audio_devices": [{"card-profile": "p\u0007q", "sinks": [
            {"name": "bad name!"}, {"name": "ok", "master_channel_map": "[ FL; rm ]", "sink_description": "x" * 500},
            {"name": "ok2", "master_channel_map": "[ FL FR ]", "remix": "yes"}]}]}})
        device = audioinfo.describe_audio(odd)["devices"][0]
        self.assertEqual(device["profile"], "pq")
        self.assertEqual([sink["id"] for sink in device["sinks"]], ["ok", "ok2"], "unsafe sink ids are dropped")
        self.assertIsNone(device["sinks"][0]["card_channels"])
        self.assertEqual(len(device["sinks"][0]["description"]), audioinfo.MAX_TEXT)
        self.assertTrue(device["sinks"][1]["remix"])

    def test_counts_are_bounded(self):
        class Fake:
            raw = {"outputmappers": {"audio_devices": [
                {"sinks": [{"name": f"s{n}"} for n in range(40)]} for _ in range(20)]}}
            outputs = ()
        result = audioinfo.describe_audio(Fake())
        self.assertEqual(len(result["devices"]), audioinfo.MAX_DEVICES)
        self.assertEqual(len(result["devices"][0]["sinks"]), audioinfo.MAX_SINKS)


if __name__ == "__main__":
    unittest.main()

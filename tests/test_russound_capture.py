from __future__ import annotations

import argparse
import unittest

from tool.russound_capture import (
    RussoundTrafficDecoder,
    SerialTarget,
    TcpTarget,
    build_rnet_frame,
    build_get_zone_info_frame,
    build_mute_frame,
    build_power_frame,
    build_set_volume_frame,
    build_volume_event_frame,
    extract_frames,
    parse_hex_input,
    resolve_target,
)


class RussoundCaptureTests(unittest.TestCase):
    def test_extract_frames_handles_partial_chunks(self) -> None:
        chunks = [b"noise\xf0\x00\x01", b"\x02\xf7tail"]
        self.assertEqual(list(extract_frames(chunks)), [b"\xf0\x00\x01\x02\xf7"])

    def test_parse_hex_input_accepts_comments_and_whitespace(self) -> None:
        self.assertEqual(list(parse_hex_input(["# captured", "F0 01 F7", ""])), [bytes.fromhex("F0 01 F7")])

    def test_decode_known_zone_power_update(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 00 00 7F 00 00 70 05 02 02 00 00 F1 23 00 01 00 02 00 01 F7")
        record = decoder.decode(frame)
        self.assertEqual(record["semantic"]["zone_updates"], [{"controller": 1, "zone": 3, "setting": "power", "value": True}])

    def test_decode_source_toggle_event(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 00 00 7F 00 00 00 05 03 02 00 00 02 04 03 6B 00 00 00 00 00 01 04 F7")
        event = decoder.decode(frame)["semantic"]["event"]
        self.assertEqual(event["name"], "source_toggle")
        self.assertEqual(event["id"], "6B")
        self.assertEqual(event["target"], {"controller": 1, "zone": 1})

    def test_decode_special_device_address_semantics(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 7F 7E 7D 00 00 7F 05 02 02 00 00 7F 00 00 00 00 00 01 01 F7")
        address = decoder.decode(frame)["semantic"]["address"]
        self.assertEqual(address["target"]["controller"]["meaning"], "all controllers")
        self.assertEqual(address["target"]["zone"]["meaning"], "controller link")
        self.assertEqual(address["target"]["keypad"]["meaning"], "source/tuner class")
        self.assertEqual(address["source"]["keypad"]["meaning"], "controller itself / all devices")

    def test_decode_volume_up_event(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 00 00 7F 00 00 00 05 03 02 00 00 02 04 03 7F 00 00 00 00 00 01 18 F7")
        event = decoder.decode(frame)["semantic"]["event"]
        self.assertEqual(event["name"], "volume_up")
        self.assertEqual(event["data"], 0)

    def test_decode_direct_display_feedback(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex(
            "F0 00 00 70 00 00 7F 00 02 01 01 02 01 01 00 00 01 00 "
            "10 00 00 60 F1 15 2A 20 4D 75 74 65 64 20 2A 00 5A 5A 5A 24 F7"
        )
        display = decoder.decode(frame)["semantic"]["display"]
        self.assertEqual(display["message"], "direct display feedback")
        self.assertEqual(display["text"], "* Muted *")
        self.assertEqual(display["payload_length"], 0x10)

    def test_decode_path_based_display_feedback(self) -> None:
        decoder = RussoundTrafficDecoder()
        payload = [
            0xF0, 0x00, 0x00, 0x70, 0x00, 0x00, 0x7F, 0x00,
            0x02, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00,
            *b"Zone 1\x00",
        ]
        display = decoder.decode(build_rnet_frame(payload))["semantic"]["display"]
        self.assertEqual(display["text"], "Zone 1")
        self.assertEqual(display["flash_time_10ms"], 1)

    def test_decode_path_based_zone_info_request(self) -> None:
        decoder = RussoundTrafficDecoder()
        request = decoder.decode(build_get_zone_info_frame(1, 2))["semantic"]["request"]
        self.assertEqual(request["path"], [0x02, 0x00, 0x01, 0x07])

    def test_decode_zone_info_response_from_source_path(self) -> None:
        decoder = RussoundTrafficDecoder()
        payload = [
            0xF0, 0x00, 0x00, 0x7F, 0x00, 0x00, 0x7D, 0x00,
            0x00, 0x04, 0x02, 0x00, 0x00, 0x07,
            1, 2, 20, 10, 11, 1, 10, 1, 0, 0, 0,
        ]
        response = decoder.decode(build_rnet_frame(payload))["semantic"]["response"]
        self.assertEqual(response["target"]["zone"], 1)
        self.assertEqual(response["zone"]["volume"], 40)
        self.assertEqual(response["zone"]["bass"], 0)

    def test_decode_system_zone_activity_event(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex(
            "F0 7E 00 70 00 00 7F 05 02 01 00 02 01 00 "
            "F1 37 00 00 00 01 00 01 28 F7"
        )
        event = decoder.decode(frame)["semantic"]["event"]
        self.assertEqual(event["name"], "zone_activity")
        self.assertEqual(event["target_path"], [0x01, 0x00])
        self.assertEqual(event["zones_on"], [1])

    def test_decode_event_handshake(self) -> None:
        decoder = RussoundTrafficDecoder()
        payload = [0xF0, 0x00, 0x00, 0x70, 0x00, 0x00, 0x60, 0x02, 0x06]
        handshake = decoder.decode(build_rnet_frame(payload))["semantic"]["handshake"]
        self.assertEqual(handshake["message"], "event handshake")
        self.assertEqual(handshake["kind"], 0x06)

    def test_decode_controller_set_data_message_with_escaped_source(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex(
            "F0 00 00 7F 00 00 F1 00 01 05 02 00 00 00 02 03 04 05 "
            "00 00 F1 00 F1 00 70 F7"
        )
        request = decoder.decode(frame)["semantic"]["request"]
        self.assertEqual(request["message"], "request-data message")
        self.assertEqual(request["source"]["keypad"], 0xFF)
        self.assertEqual(request["body"], "05 02 00 00 00 02 03 04 05 00 00 FF FF")

    def test_build_volume_up_frame(self) -> None:
        frame = build_volume_event_frame(1, 1, 0x7F)
        self.assertEqual(frame.hex(" ").upper(), "F0 00 00 7F 00 00 70 05 02 02 00 00 7F 00 00 00 00 00 01 7B F7")

    def test_build_volume_down_frame_uses_invert_encoding(self) -> None:
        frame = build_volume_event_frame(1, 1, 0x80)
        self.assertEqual(frame[12:15], bytes.fromhex("F1 7F 00"))
        self.assertEqual(frame[-1], 0xF7)

    def test_build_set_volume_defaults_to_42(self) -> None:
        frame = build_set_volume_frame(1, 1)
        self.assertEqual(frame[15], 0x15)
        self.assertEqual(frame[-1], 0xF7)

    def test_build_get_zone_info_frames_for_zones_one_to_three(self) -> None:
        self.assertEqual(
            build_get_zone_info_frame(1, 1).hex(" ").upper(),
            "F0 00 00 7F 00 00 70 01 04 02 00 00 07 00 00 7C F7",
        )
        self.assertEqual(build_get_zone_info_frame(1, 2)[11], 0x01)
        self.assertEqual(build_get_zone_info_frame(1, 3)[11], 0x02)

    def test_build_mute_frame_for_zone_one(self) -> None:
        frame = build_mute_frame(1, 1)
        self.assertEqual(
            frame.hex(" ").upper(),
            "F0 00 00 7F 00 00 70 05 02 02 00 00 F1 40 00 00 00 0D 00 01 3B F7",
        )

    def test_build_power_frames_for_zone_one(self) -> None:
        self.assertEqual(
            build_power_frame(1, 1, True).hex(" ").upper(),
            "F0 00 00 7F 00 00 70 05 02 02 00 00 F1 23 00 01 00 00 00 01 12 F7",
        )
        self.assertEqual(build_power_frame(1, 1, False)[15], 0x00)

    def test_resolve_target_prefers_host_over_serial_device(self) -> None:
        parser = argparse.Namespace(host="192.168.1.50", port=6666, device="/dev/ttyUSB0", baud=19200)
        target = resolve_target(parser)
        self.assertIsInstance(target, TcpTarget)
        self.assertEqual(target.describe(), "192.168.1.50:6666 (ser2net)")

    def test_resolve_target_falls_back_to_serial_device(self) -> None:
        parser = argparse.Namespace(host=None, port=6666, device="/dev/ttyUSB0", baud=19200)
        target = resolve_target(parser)
        self.assertIsInstance(target, SerialTarget)
        self.assertEqual(target.describe(), "/dev/ttyUSB0 19200 baud")

    def test_valid_checksum_is_compact(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 00 00 7F 00 00 70 05 02 02 00 00 F1 23 00 01 00 02 00 01 14 F7")
        self.assertEqual(decoder.decode(frame)["checksum"], "ok")

    def test_decode_unknown_frame_preserves_raw_and_checksum(self) -> None:
        decoder = RussoundTrafficDecoder()
        frame = bytes.fromhex("F0 7E 00 00 00 00 7F 05 02 01 00 00 02 01 00 F1 37 00 00 00 01 00 01 38 F7")
        record = decoder.decode(frame)
        self.assertEqual(record["raw"], " ".join(f"{byte:02X}" for byte in frame))
        self.assertFalse(record["checksum"]["valid"])
        self.assertEqual(record["semantic"]["event"]["name"], "event_0x02")


if __name__ == "__main__":
    unittest.main()
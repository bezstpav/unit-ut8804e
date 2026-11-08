"""Decode captured meter packets without opening or controlling hardware."""
import json
from pathlib import Path
import unittest

from ut8804e.protocol import decode_packet, unpack_frame


class HardwarePacketTests(unittest.TestCase):
    def test_captured_readings(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "hardware_2026_11_08.json").read_text())
        for case in fixture["readings"]:
            with self.subTest(case=case["name"]):
                decoded = decode_packet(unpack_frame(bytes.fromhex(case["raw_frame_hex"])))
                self.assertEqual(decoded["function"], case["function"])
                self.assertEqual(decoded["function_selector"], case["function_selector"])
                for key, expected in case["primary"].items():
                    self.assertEqual(decoded["primary"][key], expected)

    def test_captured_rejection(self):
        decoded = decode_packet(unpack_frame(bytes.fromhex("abcd05000145529d00")))
        self.assertEqual(decoded["kind"], "ack")
        self.assertEqual(decoded["token"], "ER")
        self.assertIs(decoded["accepted"], False)

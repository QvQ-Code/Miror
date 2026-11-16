"""Tests for sizing, the stream header, and USB device parsing."""

from __future__ import annotations

import unittest

from miror.adb_host import is_usb_device, parse_devices, parse_wm_size
from miror.session import LatestFrame
from miror.support import agent_jar
from miror.annexb import AccessUnitAssembler, nal_type, to_annexb
from miror.protocol import HEADER, KIND_CONFIG, KIND_FRAME, Packet, PacketQueue, pack_packet, unpack_header
from miror.scale import bitrate_for, clean_half, fit_1080p, light_bitrate, present_box
from miror.touch import TouchPort, map_touch, oriented_panel


class ScaleTests(unittest.TestCase):
    def test_agent_jar_is_packed(self) -> None:
        self.assertTrue(agent_jar().is_file())
        self.assertGreater(agent_jar().stat().st_size, 1000)

    def test_native_1080p_stays(self) -> None:
        self.assertEqual(fit_1080p(1920, 1080), (1920, 1080))
        self.assertEqual(fit_1080p(1080, 1920), (1080, 1920))

    def test_tall_phone_fits_box(self) -> None:
        self.assertEqual(fit_1080p(1080, 2400), (864, 1920))
        self.assertEqual(fit_1080p(1440, 3200), (864, 1920))

    def test_smaller_phone_is_unchanged(self) -> None:
        self.assertEqual(fit_1080p(720, 1280), (720, 1280))

    def test_alignment_and_cap(self) -> None:
        width, height = fit_1080p(1080, 2340)
        self.assertLessEqual(max(width, height), 1920)
        self.assertLessEqual(min(width, height), 1080)
        self.assertEqual(width % 8, 0)
        self.assertEqual(height % 8, 0)
        self.assertEqual((width, height), (880, 1920))

    def test_bitrate_tracks_pixels(self) -> None:
        self.assertEqual(bitrate_for(1920, 1080), 12_000_000)
        self.assertGreaterEqual(bitrate_for(320, 480), 2_000_000)

    def test_nubia_size_tiers(self) -> None:
        self.assertEqual(fit_1080p(1080, 2408, 960, 540), (424, 960))
        self.assertEqual(clean_half(1080, 2408), (544, 1208))
        self.assertEqual(fit_1080p(1080, 2408, 1920, 1080), (856, 1920))
        self.assertEqual(light_bitrate(424, 960, 30), 4_000_000)
        self.assertEqual(light_bitrate(424, 960, 60), 8_000_000)
        self.assertEqual(light_bitrate(544, 1208, 30), 6_457_861)
        self.assertEqual(light_bitrate(544, 1208, 60), 12_915_723)
        self.assertEqual(light_bitrate(856, 1920, 60), 32_301_886)

    def test_touch_maps_onto_the_panel(self) -> None:
        self.assertEqual(map_touch(50, 100, 100, 200, 100, 200, 1080, 2408), (540, 1204))
        self.assertIsNone(map_touch(10, 100, 200, 200, 100, 200, 1080, 2408))
        self.assertEqual(map_touch(100, 100, 200, 200, 100, 200, 1080, 2408), (540, 1204))
        self.assertEqual(map_touch(0, 100, 200, 200, 100, 200, 1080, 2408, clamp=True)[0], 0)
        self.assertEqual(oriented_panel(100, 200, 2408, 1080), (1080, 2408))
        self.assertEqual(map_touch(50, 100, 100, 200, 100, 200, 2408, 1080), (540, 1204))

    def test_closed_touch_port_drops_events(self) -> None:
        port = TouchPort()
        port.close()
        port.down(1, 2)
        port.move(3, 4)
        port.up(5, 6)
        port.close()

    def test_present_box_leaves_424_and_544_alone(self) -> None:
        self.assertEqual(present_box(424, 960, 424, 960), (0, 0))
        self.assertEqual(present_box(544, 1208, 476, 1056), (0, 0))
        self.assertEqual(present_box(856, 1904, 474, 1056), (474, 1056))
        self.assertEqual(present_box(1904, 856, 1904, 856), (0, 0))
        self.assertEqual(present_box(1904, 856, 475, 1056), (0, 0))
        self.assertEqual(present_box(856, 1904, 900, 2000), (0, 0))

    def test_queue_keeps_the_newest_frame(self) -> None:
        queue = PacketQueue(limit=8)
        queue.push(Packet(KIND_CONFIG, 0, 0, 0, 0, b"cfg"))
        queue.push(Packet(KIND_FRAME, 1, 1, 1, 1, b"old"))
        queue.push(Packet(KIND_FRAME, 1, 2, 1, 1, b"new"))
        first, _reset = queue.pop(0.1)
        second, _reset = queue.pop(0.1)
        self.assertEqual(first.payload, b"cfg")
        self.assertEqual(second.payload, b"new")
        self.assertIsNone(queue.pop(0.01))

    def test_queue_keeps_p_frames_in_order(self) -> None:
        queue = PacketQueue(limit=8)
        queue.push(Packet(KIND_CONFIG, 0, 0, 0, 0, b"cfg"))
        queue.push(Packet(KIND_FRAME, 1, 1, 1, 1, b"key"))
        queue.push(Packet(KIND_FRAME, 0, 2, 1, 1, b"p1"))
        queue.push(Packet(KIND_FRAME, 0, 3, 1, 1, b"p2"))
        payloads = []
        for _ in range(4):
            item = queue.pop(0.1)
            self.assertIsNotNone(item)
            payloads.append(item[0].payload)
        self.assertEqual(payloads, [b"cfg", b"key", b"p1", b"p2"])

    def test_queue_overflow_waits_for_a_key(self) -> None:
        queue = PacketQueue(limit=2)
        queue.push(Packet(KIND_FRAME, 1, 1, 1, 1, b"key"))
        queue.push(Packet(KIND_FRAME, 0, 2, 1, 1, b"p1"))
        queue.push(Packet(KIND_FRAME, 0, 3, 1, 1, b"p2"))
        queue.push(Packet(KIND_FRAME, 0, 4, 1, 1, b"p3"))
        queue.push(Packet(KIND_FRAME, 1, 5, 1, 1, b"key2"))
        item = queue.pop(0.1)
        self.assertIsNotNone(item)
        self.assertEqual(item[0].payload, b"key2")
        self.assertIsNone(queue.pop(0.01))


class LatestFrameTests(unittest.TestCase):
    def test_push_reuses_the_same_slots(self) -> None:
        latest = LatestFrame()
        latest.push(bytes([1]) * 12, 2, 2)
        latest.push(bytes([2]) * 12, 2, 2)
        slots = {id(latest._front), id(latest._back)}
        for value in range(3, 25):
            latest.push(bytes([value]) * 12, 2, 2)
        self.assertEqual({id(latest._front), id(latest._back)}, slots)
        data, width, height, seq, _fps = latest.snapshot()
        self.assertEqual(data, bytes([24]) * 12)
        self.assertEqual((width, height, seq), (2, 2, 24))

    def test_label_stays_at_the_encoder_size(self) -> None:
        latest = LatestFrame()
        latest.push(bytes([1]) * 12, 2, 3, 856, 1904)
        nbytes, label_w, label_h, seq, _fps, bmp_w, bmp_h = latest.describe()
        self.assertEqual((nbytes, label_w, label_h, seq, bmp_w, bmp_h), (12, 856, 1904, 1, 2, 3))


class ProtocolTests(unittest.TestCase):
    def test_header_size_and_roundtrip(self) -> None:
        self.assertEqual(HEADER.size, 24)
        raw = pack_packet(b"abc", 2, 1, 99, 1080, 1920)
        length, kind, flags, pts, width, height = unpack_header(raw[:24])
        self.assertEqual((length, kind, flags, pts, width, height), (3, 2, 1, 99, 1080, 1920))
        self.assertEqual(raw[24:], b"abc")


class AnnexTests(unittest.TestCase):
    def test_length_prefixed_becomes_annexb(self) -> None:
        nal = bytes([0x65, 0x88])
        payload = len(nal).to_bytes(4, "big") + nal
        converted = to_annexb(payload)
        self.assertTrue(converted.startswith(b"\x00\x00\x00\x01"))
        self.assertEqual(nal_type(converted), 5)

    def test_access_units_split_on_aud(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65\x88"
        pframe = b"\x00\x00\x00\x01\x41\x9a"
        assembler = AccessUnitAssembler()
        units = assembler.feed(aud + idr + aud + pframe + aud)
        self.assertEqual(len(units), 2)
        self.assertIn(b"\x65\x88", units[0])
        self.assertIn(b"\x41\x9a", units[1])


class AdbParseTests(unittest.TestCase):
    def test_usb_only(self) -> None:
        text = "\n".join(
            [
                "List of devices attached",
                "R58ABC device usb:1-2 product:a52 model:SM_A526B device:a52 transport_id:1",
                "emulator-5554 device product:sdk model:sdk device:generic transport_id:2",
                "192.168.1.20:5555 device product:a52 model:SM_A526B device:a52 transport_id:3",
                "ABCDEF unauthorized transport_id:4",
                "adb-1._adb-tls-connect._tcp device product:a52 model:Pixel transport_id:5",
                "320437149112 device product:P720F03_A model:nubia_8150N device:P720F03_A transport_id:2",
            ]
        )
        devices = parse_devices(text)
        self.assertEqual([item.serial for item in devices], ["R58ABC", "ABCDEF", "320437149112"])
        self.assertEqual(devices[0].model, "SM_A526B")
        self.assertEqual(devices[0].label, "SM A526B — siap")
        self.assertFalse(is_usb_device("emulator-5554", "emulator-5554 device"))

    def test_wm_size_prefers_override(self) -> None:
        text = "Physical size: 1080x2400\nOverride size: 720x1600\n"
        self.assertEqual(parse_wm_size(text), (720, 1600))


if __name__ == "__main__":
    unittest.main()

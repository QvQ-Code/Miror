"""Turn codec buffers into Annex-B access units a decoder can eat one frame at a time."""

from __future__ import annotations


def has_start_code(data: bytes) -> bool:
    return data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01")


def avcc_config_to_annexb(data: bytes) -> bytes | None:
    if len(data) < 7 or data[0] != 1:
        return None
    offset = 5
    sps_count = data[offset] & 0x1F
    offset += 1
    parts: list[bytes] = []
    try:
        for _ in range(sps_count):
            if offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
            if length <= 0 or offset + length > len(data):
                return None
            parts.append(b"\x00\x00\x00\x01" + data[offset : offset + length])
            offset += length
        if offset >= len(data):
            return b"".join(parts) or None
        pps_count = data[offset]
        offset += 1
        for _ in range(pps_count):
            if offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
            if length <= 0 or offset + length > len(data):
                return None
            parts.append(b"\x00\x00\x00\x01" + data[offset : offset + length])
            offset += length
    except (IndexError, ValueError):
        return None
    return b"".join(parts) or None


def lengths_to_annexb(data: bytes, size: int = 4) -> bytes | None:
    offset = 0
    parts: list[bytes] = []
    if len(data) < size:
        return None
    while offset + size <= len(data):
        length = int.from_bytes(data[offset : offset + size], "big")
        offset += size
        if length <= 0 or offset + length > len(data):
            return None
        parts.append(b"\x00\x00\x00\x01" + data[offset : offset + length])
        offset += length
    if offset != len(data) or not parts:
        return None
    return b"".join(parts)


def to_annexb(data: bytes) -> bytes:
    if not data or has_start_code(data):
        return data
    if data[0] == 1:
        converted = avcc_config_to_annexb(data)
        if converted:
            return converted
    converted = lengths_to_annexb(data)
    return converted if converted else data


def _start_codes(buf: bytearray) -> list[int]:
    starts: list[int] = []
    index = 0
    limit = len(buf)
    while index + 2 < limit:
        if buf[index] == 0 and buf[index + 1] == 0 and buf[index + 2] == 1:
            starts.append(index)
            index += 3
            continue
        if (
            index + 3 < limit
            and buf[index] == 0
            and buf[index + 1] == 0
            and buf[index + 2] == 0
            and buf[index + 3] == 1
        ):
            starts.append(index)
            index += 4
            continue
        index += 1
    return starts


def nal_type(nal: bytes) -> int:
    if nal.startswith(b"\x00\x00\x00\x01"):
        header = 4
    elif nal.startswith(b"\x00\x00\x01"):
        header = 3
    else:
        return -1
    if header >= len(nal):
        return -1
    return nal[header] & 0x1F


class AccessUnitAssembler:
    """Group a raw H.264 byte stream into one access unit per picture."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._current = bytearray()
        self._seen_vcl = False

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        starts = _start_codes(self._buf)
        if len(starts) < 2:
            return self._close_if_tail_starts_unit()
        nals = [bytes(self._buf[a:b]) for a, b in zip(starts, starts[1:])]
        self._buf = bytearray(self._buf[starts[-1] :])
        units: list[bytes] = []
        for nal in nals:
            units.extend(self._take(nal))
        units.extend(self._close_if_tail_starts_unit())
        return units

    def _take(self, nal: bytes) -> list[bytes]:
        kind = nal_type(nal)
        units: list[bytes] = []
        if kind == 9 or (kind in (1, 5) and self._seen_vcl):
            if self._current:
                units.append(bytes(self._current))
            self._current = bytearray()
            self._seen_vcl = False
        self._current.extend(nal)
        if kind in (1, 5):
            self._seen_vcl = True
        return units

    def _close_if_tail_starts_unit(self) -> list[bytes]:
        """An AUD or a new picture at the unread tail ends the previous picture."""
        if not self._current:
            return []
        kind = nal_type(bytes(self._buf[:8]))
        if kind == 9 or (kind in (1, 5) and self._seen_vcl):
            unit = bytes(self._current)
            self._current = bytearray()
            self._seen_vcl = False
            return [unit]
        return []

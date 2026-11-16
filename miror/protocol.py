"""Length-prefixed stream shared by the phone agent and the desktop."""

from __future__ import annotations

import collections
import socket
import struct
import threading
from dataclasses import dataclass

HEADER = struct.Struct("<IBBHQII")
KIND_CONFIG = 1
KIND_FRAME = 2
KIND_EOS = 3
FLAG_KEY = 1


class StreamClosed(Exception):
    pass


@dataclass
class Packet:
    kind: int
    flags: int
    pts: int
    width: int
    height: int
    payload: bytes

    @property
    def sync(self) -> bool:
        return self.kind == KIND_CONFIG or (self.flags & FLAG_KEY) != 0


def pack_packet(payload: bytes, kind: int, flags: int, pts: int, width: int, height: int) -> bytes:
    return HEADER.pack(len(payload), kind, flags, 0, pts & 0xFFFFFFFFFFFFFFFF, width, height) + payload


def unpack_header(header: bytes) -> tuple[int, int, int, int, int, int]:
    if len(header) != HEADER.size:
        raise ValueError("short header")
    length, kind, flags, _reserved, pts, width, height = HEADER.unpack(header)
    return length, kind, flags, pts, width, height


def read_exact(sock: socket.socket, size: int, stop: threading.Event) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        if stop.is_set():
            raise StreamClosed()
        try:
            chunk = sock.recv(size - len(buf))
        except socket.timeout:
            continue
        except OSError as exc:
            raise StreamClosed() from exc
        if not chunk:
            raise StreamClosed()
        buf += chunk
    return bytes(buf)


def read_packet(sock: socket.socket, stop: threading.Event) -> Packet:
    header = read_exact(sock, HEADER.size, stop)
    length, kind, flags, pts, width, height = unpack_header(header)
    if length < 0 or length > 8_000_000:
        raise ValueError(f"bad frame length {length}")
    payload = read_exact(sock, length, stop) if length else b""
    return Packet(kind, flags, pts, width, height, payload)


class PacketQueue:
    """In-order pictures. A keyframe may replace what is still queued.

    A P-frame is kept behind the frames already waiting. The queue never blocks
    the phone. If it is already full, later P-frames are dropped until the next
    keyframe, because a gap in the reference chain cannot be decoded.
    """

    def __init__(self, limit: int = 4) -> None:
        self._items: collections.deque[Packet] = collections.deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.closed = False
        self.limit = limit
        self._need_key = False

    def push(self, packet: Packet) -> None:
        with self._cv:
            if self.closed:
                return
            if packet.kind == KIND_FRAME and packet.sync:
                self._need_key = False
                self._items = collections.deque(
                    item for item in self._items if item.kind != KIND_FRAME
                )
                self._items.append(packet)
            elif packet.kind == KIND_FRAME:
                if self._need_key:
                    return
                queued = sum(1 for item in self._items if item.kind == KIND_FRAME)
                if queued >= self.limit:
                    self._need_key = True
                    return
                self._items.append(packet)
            else:
                self._items.append(packet)
            self._cv.notify()

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def pop(self, timeout: float) -> tuple[Packet, bool] | None:
        with self._cv:
            if not self._items and not self.closed:
                self._cv.wait(timeout)
            if not self._items:
                return None
            packet = self._items.popleft()
            self._cv.notify()
            return packet, False

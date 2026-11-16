"""Map a mouse gesture on the mirror to a phone touch."""

from __future__ import annotations

import queue
import subprocess
import threading


def oriented_panel(frame_w: int, frame_h: int, panel_w: int, panel_h: int) -> tuple[int, int]:
    """Panel pixels in the same orientation as the picture."""

    if panel_w <= 0 or panel_h <= 0:
        return frame_w, frame_h
    if (frame_w > frame_h) != (panel_w > panel_h):
        return panel_h, panel_w
    return panel_w, panel_h


def map_touch(
    x: float,
    y: float,
    view_w: int,
    view_h: int,
    frame_w: int,
    frame_h: int,
    panel_w: int,
    panel_h: int,
    clamp: bool = False,
) -> tuple[int, int] | None:
    """View pixel to phone pixel. Clicks in the black bars are ignored."""

    if frame_w <= 0 or frame_h <= 0 or view_w <= 0 or view_h <= 0:
        return None
    scale = min(view_w / frame_w, view_h / frame_h)
    width = max(1, int(frame_w * scale))
    height = max(1, int(frame_h * scale))
    left = (view_w - width) // 2
    top = (view_h - height) // 2
    if clamp:
        x = min(max(x, left), left + width - 1)
        y = min(max(y, top), top + height - 1)
    elif x < left or y < top or x >= left + width or y >= top + height:
        return None
    panel_x, panel_y = oriented_panel(frame_w, frame_h, panel_w, panel_h)
    px = min(panel_x - 1, max(0, int((x - left) / width * panel_x)))
    py = min(panel_y - 1, max(0, int((y - top) / height * panel_y)))
    return px, py


class TouchPort:
    """One long-lived adb shell. Each gesture is a real touch, not a status-bar command."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._down = False

    def open(self, adb, serial: str) -> None:
        self.close()
        proc = adb.open_shell(serial)
        self._queue = queue.Queue(maxsize=32)
        thread = threading.Thread(target=self._write, args=(proc,), name="miror-touch", daemon=True)
        with self._lock:
            self._proc = proc
            self._down = False
            self._thread = thread
        thread.start()

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            down = self._down
            thread = self._thread
            self._proc = None
            self._down = False
            self._thread = None
        if proc is None and thread is None:
            return
        if down:
            self._send(b"input motionevent CANCEL 0 0\n")
        self._send(None)
        if thread is not None:
            thread.join(timeout=0.4)
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass

    def down(self, x: int, y: int) -> None:
        with self._lock:
            if self._proc is None:
                return
            self._down = True
        self._send(f"input motionevent DOWN {int(x)} {int(y)}\n".encode("ascii"))

    def move(self, x: int, y: int) -> None:
        with self._lock:
            if not self._down:
                return
        self._send(f"input motionevent MOVE {int(x)} {int(y)}\n".encode("ascii"))

    def up(self, x: int, y: int) -> None:
        with self._lock:
            if not self._down:
                return
            self._down = False
        self._send(f"input motionevent UP {int(x)} {int(y)}\n".encode("ascii"))

    def _send(self, line: bytes | None) -> None:
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            pass

    def _write(self, proc: subprocess.Popen[bytes]) -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        while True:
            line = self._queue.get()
            if line is None:
                return
            try:
                stdin.write(line)
                stdin.flush()
            except (OSError, ValueError):
                return

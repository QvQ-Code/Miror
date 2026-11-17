"""Map a mouse gesture on the mirror to a phone touch."""

from __future__ import annotations

import queue
import subprocess
import threading
import time


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


_SPECIAL = {
    "backspace": "KEYCODE_DEL",
    "delete": "KEYCODE_FORWARD_DEL",
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "home": "KEYCODE_MOVE_HOME",
    "end": "KEYCODE_MOVE_END",
    "pageup": "KEYCODE_PAGE_UP",
    "pagedown": "KEYCODE_PAGE_DOWN",
    "back": "KEYCODE_BACK",
    "caps": "KEYCODE_CAPS_LOCK",
}


def keyboard_line(
    text: str = "",
    special: str = "",
    ctrl: bool = False,
    alt: bool = False,
    shift: bool = False,
) -> bytes | None:
    """One shell line. This does not show or hide the phone's own keyboard."""

    if ctrl or alt or (shift and special):
        code = _combo_code(text, special)
        if code is None:
            return None
        parts: list[str] = []
        if ctrl:
            parts.append("KEYCODE_CTRL_LEFT")
        if alt:
            parts.append("KEYCODE_ALT_LEFT")
        if shift:
            parts.append("KEYCODE_SHIFT_LEFT")
        parts.append(code)
        if len(parts) == 1:
            return f"input keyevent {code}\n".encode("ascii")
        return ("input keycombination " + " ".join(parts) + "\n").encode("ascii")
    if special:
        code = _SPECIAL.get(special)
        if code is None and special.startswith("f") and special[1:].isdigit():
            number = int(special[1:])
            if 1 <= number <= 12:
                code = f"KEYCODE_F{number}"
        if code is None:
            return None
        if special == "enter":
            # Hide the system keyboard only when it is actually open, then deliver Enter.
            return (
                b"dumpsys input_method | grep -q mInputShown=true && input keyevent KEYCODE_BACK\n"
                b"input keyboard keyevent KEYCODE_ENTER\n"
            )
        return f"input keyevent {code}\n".encode("ascii")
    if not text or any(ord(ch) < 32 for ch in text):
        return None
    quoted = "'" + text.replace("'", "'\\''") + "'"
    return f"input keyboard text {quoted}\n".encode("utf-8")


def _combo_code(text: str, special: str) -> str | None:
    if special:
        code = _SPECIAL.get(special)
        if code is not None:
            return code
        if special.startswith("f") and special[1:].isdigit():
            number = int(special[1:])
            if 1 <= number <= 12:
                return f"KEYCODE_F{number}"
        return None
    if len(text) != 1:
        return None
    if text == " ":
        return "KEYCODE_SPACE"
    upper = text.upper()
    if ("A" <= upper <= "Z") or ("0" <= upper <= "9"):
        return "KEYCODE_" + upper
    return None


class TouchPort:
    """One long-lived adb shell. Each gesture is a real touch, not a status-bar command."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._down = False
        self._origin = (0, 0)
        self._adb = None
        self._serial = ""
        self._hide_gen = 0
        self._hide_running = False

    def open(self, adb, serial: str) -> None:
        self.close()
        proc = adb.open_shell(serial)
        self._queue = queue.Queue(maxsize=32)
        thread = threading.Thread(target=self._write, args=(proc,), name="miror-touch", daemon=True)
        with self._lock:
            self._proc = proc
            self._down = False
            self._adb = adb
            self._serial = serial
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
            self._adb = None
            self._serial = ""
            self._hide_gen += 1
            self._hide_running = False
        if proc is None and thread is None:
            return
        if down:
            self._send(b"input motionevent CANCEL 0 0\n", important=True)
        self._send(None, important=True)
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
            self._origin = (int(x), int(y))
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
            origin = self._origin
            adb = self._adb
            serial = self._serial
        self._send(f"input motionevent UP {int(x)} {int(y)}\n".encode("ascii"))
        if abs(int(x) - origin[0]) <= 64 and abs(int(y) - origin[1]) <= 64:
            self._schedule_hide(adb, serial)

    def _schedule_hide(self, adb, serial: str) -> None:
        """After a mirror tap, hide the system keyboard if the phone opened it.

        A finger on the phone itself does not come through here, so that keyboard stays.
        The check runs beside the touch shell and never waits on the picture.
        """

        if adb is None or not serial:
            return
        with self._lock:
            if self._hide_running:
                return
            self._hide_running = True
            self._hide_gen += 1
            generation = self._hide_gen

        def run() -> None:
            try:
                time.sleep(0.25)
                with self._lock:
                    if generation != self._hide_gen:
                        return
                adb.shell(
                    serial,
                    "sh",
                    "-c",
                    "dumpsys input_method | grep -q mInputShown=true && input keyevent KEYCODE_BACK",
                    timeout=3,
                )
            except Exception:
                return
            finally:
                with self._lock:
                    if generation == self._hide_gen:
                        self._hide_running = False

        threading.Thread(target=run, name="miror-ime", daemon=True).start()

    def keys(self, line: bytes) -> None:
        if not line:
            return
        with self._lock:
            if self._proc is None:
                return
        self._send(line, important=True)

    def _send(self, line: bytes | None, important: bool = False) -> None:
        try:
            self._queue.put_nowait(line)
            return
        except queue.Full:
            if not important:
                return
        try:
            self._queue.get_nowait()
        except queue.Empty:
            return
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

"""ADB over USB. The phone's own USB-debugging dialog authorizes the computer."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from miror.support import app_dir, log

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
CREATE_NO_WINDOW = 0x08000000
_SIZE = re.compile(r"(\d+)\s*x\s*(\d+)", re.IGNORECASE)


@dataclass
class Device:
    serial: str
    state: str
    model: str = ""

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") if self.model else self.serial
        if self.state == "device":
            tail = "siap"
        elif self.state == "unauthorized":
            tail = "menunggu izin"
        elif self.state == "offline":
            tail = "offline"
        else:
            tail = self.state
        return f"{name} — {tail}"


def is_usb_device(serial: str, line: str) -> bool:
    lowered = serial.lower()
    if serial.startswith("emulator-"):
        return False
    if "adb-tls" in lowered or "_tcp" in lowered or ":" in serial:
        return False
    return True


def parse_devices(text: str) -> list[Device]:
    devices: list[Device] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in {"device", "unauthorized", "offline"}:
            continue
        if not is_usb_device(serial, line):
            continue
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1]
        devices.append(Device(serial=serial, state=state, model=model))
    return devices


def _local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def parse_wm_size(text: str) -> tuple[int, int] | None:
    override = None
    physical = None
    for line in text.splitlines():
        match = _SIZE.search(line)
        if not match:
            continue
        pair = (int(match.group(1)), int(match.group(2)))
        if "Override" in line:
            override = pair
        elif "Physical" in line:
            physical = pair
        elif physical is None:
            physical = pair
    return override or physical


class AdbHost:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or app_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tools = self.root / "platform-tools"
        # A frozen zip can carry platform-tools next to Miror.exe. Source runs
        # keep using the copy already in %LOCALAPPDATA%\Miror.
        if root is None and getattr(sys, "frozen", False):
            beside = Path(sys.executable).resolve().parent / "platform-tools"
            if (beside / "adb.exe").is_file():
                self.tools = beside
        self.adb_path = self.tools / "adb.exe"
        self.server_started = False
        self._owned_server = False

    def env(self) -> dict[str, str]:
        return os.environ.copy()

    def ensure_binary(self) -> None:
        if self.adb_path.exists():
            return
        self.tools.mkdir(parents=True, exist_ok=True)
        archive = self.root / "platform-tools.zip"
        log("Mengunduh platform-tools")
        with urlopen(PLATFORM_TOOLS_URL, timeout=120) as response, archive.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(self.root)
        if not self.adb_path.exists():
            raise FileNotFoundError(f"adb tidak ada setelah unduhan: {self.adb_path}")
        log(f"ADB siap di {self.adb_path}")

    def start_server(self) -> None:
        self.ensure_binary()
        # A second server cannot share the phone's USB interface. Join the
        # server that is already running, and only stop one this process started.
        self._owned_server = not _local_port_open(5037)
        result = self._run(["start-server"], check=False, timeout=30)
        if result.returncode != 0:
            raise RuntimeError((result.stdout or result.stderr or "adb start-server gagal").strip())
        self.server_started = True
        log("Server ADB siap. Jika ponsel belum mengizinkan PC ini, ketuk Izinkan.")

    def shutdown(self) -> None:
        if not self.server_started or not self._owned_server or not self.adb_path.exists():
            return
        self._run(["kill-server"], check=False, timeout=20)
        self.server_started = False

    def devices(self) -> list[Device]:
        result = self._run(["devices", "-l"], check=True, timeout=15)
        return parse_devices(result.stdout)

    def shell(self, serial: str, *args: str, timeout: float = 20) -> str:
        result = self._run(["-s", serial, "shell", *args], check=False, timeout=timeout)
        return result.stdout

    def push(self, serial: str, source: Path, remote: str) -> None:
        result = self._run(
            ["-s", serial, "push", str(source), remote],
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout.strip() or "adb push gagal")

    def forward(self, serial: str, remote: str = "localabstract:miror") -> int:
        self.remove_forwards(serial, remote)
        result = self._run(["-s", serial, "forward", "tcp:0", remote], check=False, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stdout.strip() or "adb forward gagal")
        numbers = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if not numbers:
            raise RuntimeError(result.stdout.strip() or "adb forward tidak memberi port")
        return int(numbers[-1])

    def remove_forward(self, serial: str, port: int) -> None:
        self._run(["-s", serial, "forward", "--remove", f"tcp:{port}"], check=False, timeout=10)

    def remove_forwards(self, serial: str, remote: str) -> None:
        listed = self._run(["forward", "--list"], check=False, timeout=10)
        for line in listed.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == serial and parts[2] == remote:
                self._run(["-s", serial, "forward", "--remove", parts[1]], check=False, timeout=10)

    def open_shell(self, serial: str) -> subprocess.Popen[bytes]:
        """Interactive shell so touch lines run as they are written."""

        return subprocess.Popen(
            [str(self.adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env(),
            creationflags=CREATE_NO_WINDOW,
        )

    def popen(self, args: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [str(self.adb_path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=self.env(),
            creationflags=CREATE_NO_WINDOW,
        )

    def kill_remote(self, serial: str, pid: int | None) -> None:
        if not pid:
            return
        self._run(["-s", serial, "shell", "kill", "-9", str(pid)], check=False, timeout=8)

    def wm_size(self, serial: str) -> tuple[int, int] | None:
        return parse_wm_size(self.shell(serial, "wm", "size", timeout=10))

    def screenrecord_limit(self, serial: str) -> str | None:
        text = self.shell(serial, "screenrecord", "--help", timeout=10)
        lowered = text.lower()
        if "output-format" not in lowered and "h264" not in lowered:
            return None
        if "set to 0" in lowered or "remove the time limit" in lowered:
            return "0"
        return "180"

    def connect_local(self, port: int, timeout: float = 5) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 856 pictures are large. A bigger window lets one slow paint finish
        # without the phone blocking and the fps number falling.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        sock.settimeout(timeout)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        return sock

    def _run(self, args: list[str], check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [str(self.adb_path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self.env(),
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"adb {' '.join(args)} terlalu lama") from exc
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        result.stdout = output
        if check and result.returncode != 0:
            raise RuntimeError(output or f"adb {' '.join(args)} gagal")
        return result

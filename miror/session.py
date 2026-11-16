"""Start the phone agent and read frames in decode order."""

from __future__ import annotations

import collections
import threading
import time

from PySide6.QtCore import QObject, Signal

from miror.adb_host import AdbHost
from miror.annexb import AccessUnitAssembler
from miror.decode import Decoder
from miror.protocol import (
    FLAG_KEY,
    KIND_CONFIG,
    KIND_EOS,
    KIND_FRAME,
    PacketQueue,
    StreamClosed,
    read_packet,
)
from miror.playback import AacStream, PcmPlayer
from miror.scale import bitrate_for, fit_1080p, present_box
from miror.support import agent_jar, log
from miror.touch import TouchPort


class LatestFrame:
    """The newest RGB picture.

    The two slots are allocated once. Keeping a fresh 1.2 MB object every frame
    promoted it into an old generation and paused the PC for about 90 ms every
    15 seconds, which stalled the phone send too.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._front = bytearray()
        self._back = bytearray()
        self.width = 0
        self.height = 0
        self.label_w = 0
        self.label_h = 0
        self.seq = 0
        self._times: collections.deque[float] = collections.deque()

    def push(
        self,
        data: bytes | bytearray,
        width: int,
        height: int,
        label_w: int = 0,
        label_h: int = 0,
    ) -> None:
        size = len(data)
        if size <= 0:
            return
        if len(self._back) != size:
            self._back = bytearray(size)
        self._back[:] = data
        now = time.monotonic()
        shown_w = int(label_w) if label_w else int(width)
        shown_h = int(label_h) if label_h else int(height)
        with self._lock:
            self._back, self._front = self._front, self._back
            self.width = width
            self.height = height
            self.label_w = shown_w
            self.label_h = shown_h
            self.seq += 1
            self._times.append(now)
            while self._times and now - self._times[0] > 1.0:
                self._times.popleft()

    def tick(self) -> None:
        """Count a repeated picture without copying or redrawing it.

        Copying the 856 bitmap sixty times a second paused the PC and the
        fps number dipped even though the phone was still on time.
        """

        now = time.monotonic()
        with self._lock:
            if self.seq == 0:
                return
            self._times.append(now)
            while self._times and now - self._times[0] > 1.0:
                self._times.popleft()

    def describe(self) -> tuple[int, int, int, int, int, int, int] | None:
        with self._lock:
            if self.seq == 0 or len(self._front) == 0:
                return None
            return (
                len(self._front),
                self.label_w,
                self.label_h,
                self.seq,
                len(self._times),
                self.width,
                self.height,
            )

    def copy_front(self, dest: bytearray) -> tuple[int, int, int, int] | None:
        with self._lock:
            if self.seq == 0 or len(self._front) != len(dest):
                return None
            dest[:] = self._front
            return self.width, self.height, self.seq, len(self._times)

    def snapshot(self) -> tuple[bytes, int, int, int, int] | None:
        with self._lock:
            if self.seq == 0 or len(self._front) == 0:
                return None
            return bytes(self._front), self.width, self.height, self.seq, len(self._times)


class MirrorSession(QObject):
    status = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, adb: AdbHost) -> None:
        super().__init__()
        self.adb = adb
        self.frames = LatestFrame()
        self.serial = ""
        self.running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = None
        self._sock = None
        self._port = 0
        self._pid: int | None = None
        self._lock = threading.Lock()
        self.fps_cap = 60
        self.size_tier = 1
        self.draw_size = (0, 0)
        self.panel = (0, 0)
        self.touch = TouchPort()
        self._audio_denied = threading.Event()
        self._audio_ready = threading.Event()
        self._audio_sock = None
        self._audio_port = 0
        self._audio_thread: threading.Thread | None = None

    def start(self, serial: str, fps: int = 60, tier: int = 1) -> None:
        if self.running:
            return
        leftover = self._audio_thread
        if leftover is not None and leftover.is_alive() and leftover is not threading.current_thread():
            leftover.join(0.3)
        self.serial = serial
        self.fps_cap = 30 if int(fps) <= 30 else 60
        self.size_tier = tier if int(tier) in (2, 3) else 1
        self.running = True
        self._stop.clear()
        self._audio_denied.clear()
        self._audio_ready.clear()
        self._thread = threading.Thread(target=self._run, name="miror-session", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.touch.close()
        with self._lock:
            sock = self._sock
            proc = self._proc
            audio = self._audio_sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if audio is not None:
            try:
                audio.close()
            except OSError:
                pass
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def _prepare_touch(self) -> None:
        try:
            size = self.adb.wm_size(self.serial)
            if size:
                self.panel = size
        except Exception as exc:
            log(f"ukuran layar {exc}")
        if self._stop.is_set():
            return
        try:
            self.touch.open(self.adb, self.serial)
        except Exception as exc:
            log(f"touch {exc}")

    def _run(self) -> None:
        error = ""
        try:
            self._prepare_touch()
            if not self._stop.is_set():
                self._run_agent()
        except Exception as first:
            if not self._stop.is_set():
                self.status.emit("Encoder ponsel gagal. Beralih ke screenrecord.")
                log(f"agent gagal: {first}")
                self._cleanup_agent()
                try:
                    self._run_screenrecord()
                except Exception as second:
                    if not self._stop.is_set():
                        error = str(second)
                        log(f"screenrecord gagal: {second}")
        finally:
            self.touch.close()
            self._cleanup_agent()
            audio_thread = self._audio_thread
            if audio_thread is not None and audio_thread is not threading.current_thread():
                audio_thread.join(0.8)
            self.running = False
            if error:
                self.failed.emit(error)
            self.finished.emit()

    def _run_agent(self) -> None:
        jar = agent_jar()
        if not jar.exists():
            raise FileNotFoundError(f"Agent belum dibangun: {jar}")
        serial = self.serial
        self.status.emit("Mengirim agent ke ponsel...")
        self.adb.push(serial, jar, "/data/local/tmp/miror-agent.jar")
        self._port = self.adb.forward(serial)
        log(f"forward tcp:{self._port} -> localabstract:miror")
        proc = self.adb.popen(
            [
                "-s",
                serial,
                "shell",
                "CLASSPATH=/data/local/tmp/miror-agent.jar",
                "exec",
                "app_process",
                "/",
                "com.miror.Agent",
                "1920",
                "1080",
                str(self.fps_cap),
                str(self.size_tier),
            ]
        )
        with self._lock:
            self._proc = proc
        ready = threading.Event()
        failed = threading.Event()
        notes: list[str] = []

        def on_line(text: str) -> None:
            if text:
                notes.append(text)
                log("phone " + text)
            if text.startswith("MIROR_PID "):
                try:
                    self._pid = int(text.split()[1])
                except (IndexError, ValueError):
                    pass
            if text == "MIROR_READY":
                ready.set()
            if text == "MIROR_AUDIO_READY":
                self._audio_ready.set()
            if "audio ditolak" in text:
                self._audio_denied.set()
            if text.startswith("MIROR_FAIL"):
                failed.set()

        reader = self._pump_stdout(proc, on_line)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not self._stop.is_set():
            if ready.is_set() or failed.is_set() or proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is not None:
            reader.join(1.0)
        if self._stop.is_set():
            return
        if not ready.is_set():
            tail = "\n".join(notes[-20:])
            raise RuntimeError(tail or "Agent tidak menjawab")
        self.status.emit("Menyambungkan gambar...")
        sock = None
        last_error = ""
        for _ in range(40):
            if self._stop.is_set():
                return
            try:
                sock = self.adb.connect_local(self._port, timeout=0.4)
                break
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.1)
        if sock is None:
            raise RuntimeError(last_error or "Socket mirror tidak terbuka")
        with self._lock:
            self._sock = sock
        self._audio_thread = threading.Thread(target=self._play_audio, name="miror-audio", daemon=True)
        self._audio_thread.start()
        self.status.emit("Mirror jalan")
        self._decode_socket(sock)
        if failed.is_set() and not self._stop.is_set():
            raise RuntimeError("\n".join(notes[-12:]) or "Agent berhenti")

    def _run_screenrecord(self) -> None:
        serial = self.serial
        if self._stop.is_set():
            return
        limit = self.adb.screenrecord_limit(serial)
        if limit is None:
            raise RuntimeError("Ponsel tidak mendukung screenrecord H.264")
        size = self.adb.wm_size(serial)
        if size is None:
            raise RuntimeError("Ukuran layar tidak terbaca")
        width, height = fit_1080p(*size)
        bitrate = bitrate_for(width, height)
        self.status.emit(f"Mirror {width}×{height}")
        while not self._stop.is_set():
            proc = self.adb.popen(
                [
                    "-s",
                    serial,
                    "exec-out",
                    "screenrecord",
                    "--output-format",
                    "h264",
                    "--bit-rate",
                    str(bitrate),
                    "--size",
                    f"{width}x{height}",
                    "--time-limit",
                    limit,
                    "-",
                ]
            )
            with self._lock:
                self._proc = proc
            assembler = AccessUnitAssembler()
            decoder = Decoder()
            assert proc.stdout is not None
            self._boost()
            idle = time.monotonic()
            while not self._stop.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                idle = time.monotonic()
                for unit in assembler.feed(chunk):
                    rgb = decoder.decode(unit, 0)
                    if rgb is not None:
                        self.frames.push(rgb, decoder.width or width, decoder.height or height)
                if proc.poll() is not None:
                    break
            if self._stop.is_set() or limit == "0":
                break
            if time.monotonic() - idle < 1 and proc.poll() not in (None, 0):
                raise RuntimeError("screenrecord berhenti")
            self.status.emit("Melanjutkan rekaman layar...")

    def _decode_socket(self, sock) -> None:
        # A catch-up burst has to fit. Overflow drops until the next key and
        # the picture freezes, so the limit stays above one encoder backlog.
        queue = PacketQueue(limit=24)
        stop = self._stop

        def reader() -> None:
            try:
                while not stop.is_set():
                    packet = read_packet(sock, stop)
                    if packet.kind == KIND_EOS:
                        break
                    if packet.kind not in (KIND_CONFIG, KIND_FRAME):
                        continue
                    queue.push(packet)
            except (StreamClosed, OSError, ValueError) as exc:
                if not stop.is_set():
                    log(f"stream berhenti: {exc}")
            finally:
                queue.close()

        decoder = Decoder()
        thread = threading.Thread(target=reader, name="miror-read", daemon=True)
        thread.start()
        self._boost()
        current = (0, 0)
        last_payload = b""
        wait_key = False
        while not stop.is_set():
            item = queue.pop(0.3)
            if item is None:
                if queue.closed:
                    break
                continue
            packet, reset = item
            if reset or (packet.width, packet.height) != current:
                decoder.reset()
                current = (packet.width, packet.height)
                last_payload = b""
                wait_key = False
            if packet.kind == KIND_CONFIG:
                decoder.decode(packet.payload, packet.pts, render=False)
                last_payload = b""
                continue
            while True:
                if packet.kind == KIND_CONFIG:
                    decoder.decode(packet.payload, packet.pts, render=False)
                    last_payload = b""
                    break
                if wait_key and (packet.flags & FLAG_KEY) == 0:
                    break
                width = packet.width or decoder.width
                height = packet.height or decoder.height
                # Same bytes as the picture already on screen. Count it for the
                # fps number and do not decode or copy it again.
                if packet.payload == last_payload and width > 0 and height > 0:
                    self.frames.tick()
                    break
                newer = queue.pop(0)
                if newer is not None:
                    # This picture is already old. Keep the reference and color
                    # only the newest one, or 856 spends the slot on a stale frame.
                    skipped = decoder.decode(packet.payload, packet.pts, render=False)
                    if skipped is None:
                        wait_key = True
                        last_payload = b""
                        packet = newer[0]
                        continue
                    last_payload = packet.payload
                    packet = newer[0]
                    if (packet.width, packet.height) != current and packet.width and packet.height:
                        decoder.reset()
                        current = (packet.width, packet.height)
                        last_payload = b""
                        wait_key = False
                    continue
                src_w = packet.width or current[0] or width
                src_h = packet.height or current[1] or height
                box_w, box_h = present_box(src_w, src_h, *self.draw_size)
                rgb = decoder.decode(packet.payload, packet.pts, out_w=box_w, out_h=box_h)
                if rgb is None:
                    wait_key = True
                    last_payload = b""
                    break
                wait_key = False
                label_w = src_w or decoder.width
                label_h = src_h or decoder.height
                last_payload = packet.payload
                if not rgb or label_w <= 0 or label_h <= 0:
                    break
                self.frames.push(
                    rgb,
                    decoder.width or label_w,
                    decoder.height or label_h,
                    label_w,
                    label_h,
                )
                break
        thread.join(timeout=1)

    def _play_audio(self) -> None:
        """Second socket. This thread never waits on the picture, and the picture never waits on it."""

        player = None
        sock = None
        port = 0
        try:
            if not self._wait_audio_gate():
                return
            port = self.adb.forward(self.serial, "localabstract:miror-audio")
            with self._lock:
                self._audio_port = port
            log(f"forward tcp:{port} -> localabstract:miror-audio")
            sock = self._connect_audio(port)
            if sock is None:
                return
            with self._lock:
                self._audio_sock = sock
            player = PcmPlayer()
            player.start()
            decoder = AacStream()
            heard = False
            while not self._stop.is_set():
                try:
                    packet = read_packet(sock, self._stop)
                except StreamClosed:
                    break
                except (OSError, ValueError) as exc:
                    if not self._stop.is_set():
                        log(f"audio stream {exc}")
                    break
                if packet.kind == KIND_EOS:
                    break
                if packet.kind not in (KIND_CONFIG, KIND_FRAME):
                    continue
                try:
                    chunks = decoder.chunks(packet.kind, packet.payload)
                except Exception as exc:
                    log(f"audio decode {exc}")
                    continue
                for pcm in chunks:
                    if not heard:
                        heard = True
                        log("audio jalan")
                    player.write(pcm)
        except Exception as exc:
            if not self._stop.is_set():
                log(f"audio {exc}")
        finally:
            if player is not None:
                player.close()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._lock:
                if self._audio_sock is sock:
                    self._audio_sock = None
                if port and self._audio_port == port:
                    self._audio_port = 0
                else:
                    port = 0
            if port and self.serial:
                try:
                    self.adb.remove_forward(self.serial, port)
                except Exception:
                    pass

    def _wait_audio_gate(self) -> bool:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._audio_denied.is_set():
                log("audio tidak diizinkan ponsel")
                return False
            if self._audio_ready.is_set():
                return True
            time.sleep(0.05)
        if self._stop.is_set():
            return False
        if self._audio_denied.is_set():
            log("audio tidak diizinkan ponsel")
            return False
        log("audio tidak tersambung")
        return False

    def _connect_audio(self, port: int):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._audio_denied.is_set():
                log("audio tidak diizinkan ponsel")
                return None
            try:
                return self.adb.connect_local(port, timeout=0.2)
            except OSError:
                time.sleep(0.1)
        if not self._stop.is_set() and not self._audio_denied.is_set():
            log("audio tidak tersambung")
        return None

    def _cleanup_agent(self) -> None:
        with self._lock:
            sock = self._sock
            proc = self._proc
            self._sock = None
            self._proc = None
            port = self._port
            pid = self._pid
            self._port = 0
            audio = self._audio_sock
            audio_port = self._audio_port
            self._audio_sock = None
            self._audio_port = 0
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if audio is not None:
            try:
                audio.close()
            except OSError:
                pass
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        if self.serial:
            try:
                self.adb.kill_remote(self.serial, pid)
            except Exception:
                pass
            if port:
                try:
                    self.adb.remove_forward(self.serial, port)
                except Exception:
                    pass
            if audio_port:
                try:
                    self.adb.remove_forward(self.serial, audio_port)
                except Exception:
                    pass

    @staticmethod
    def _pump_stdout(proc, on_line) -> None:
        def run() -> None:
            try:
                assert proc.stdout is not None
                for raw in iter(proc.stdout.readline, b""):
                    on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
            except Exception:
                pass

        thread = threading.Thread(target=run, name="miror-log", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _boost() -> None:
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.SetThreadPriority(kernel.GetCurrentThread(), 1)
        except Exception:
            pass

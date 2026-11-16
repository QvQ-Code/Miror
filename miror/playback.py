"""Play mirrored PCM without touching the picture thread."""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

from miror.support import log


class PcmQueue:
    """A short queue. Extra sound is dropped so playback cannot pile up."""

    def __init__(self, limit: int = 8) -> None:
        self.limit = limit
        self._items: list[bytes] = []

    def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        if len(self._items) >= self.limit:
            self._items.pop(0)
        self._items.append(pcm)

    def pop(self) -> bytes | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def __len__(self) -> int:
        return len(self._items)


class AacStream:
    """Decode AAC on the audio thread. The picture decoder never sees these bytes."""

    def __init__(self) -> None:
        import av

        self._av = av
        self._ctx = None
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)

    def chunks(self, kind: int, payload: bytes) -> list[bytes]:
        if kind == 1:
            self._open(payload)
            return []
        ctx = self._ctx
        if ctx is None or not payload:
            return []
        packet = self._av.Packet(payload)
        out: list[bytes] = []
        for frame in ctx.decode(packet):
            for item in self._resampler.resample(frame):
                pcm = self._pcm(item)
                if pcm:
                    out.append(pcm)
        return out

    def _open(self, extradata: bytes) -> None:
        if not extradata:
            return
        ctx = self._av.CodecContext.create("aac", "r")
        ctx.extradata = extradata
        ctx.sample_rate = 48000
        ctx.layout = "stereo"
        ctx.open()
        self._ctx = ctx
        self._resampler = self._av.AudioResampler(format="s16", layout="stereo", rate=48000)

    @staticmethod
    def _pcm(frame) -> bytes:
        # The driver buffer is longer than the samples. Playing the tail clicks.
        count = int(frame.samples) * 4
        if count <= 0:
            return b""
        raw = bytes(frame.planes[0])
        if len(raw) < count:
            count = len(raw) - (len(raw) % 4)
        pcm = raw[:count]
        extra = len(pcm) % 4
        if extra:
            pcm = pcm[:-extra]
        return pcm


class _WaveFormat(ctypes.Structure):
    _fields_ = (
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    )


class _WaveHdr(ctypes.Structure):
    _fields_ = (
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    )


_WHDR_PREPARED = 0x00000002
_WHDR_INQUEUE = 0x00000010


class PcmPlayer:
    def __init__(self) -> None:
        self._pending: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._opened = False
        self._open_failed = False
        self._handle = wintypes.HANDLE()
        self._winmm = None
        self._slots: list[tuple[_WaveHdr, ctypes.Array]] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miror-play", daemon=True)
        self._thread.start()

    def write(self, pcm: bytes) -> None:
        if self._stop.is_set() or not pcm:
            return
        extra = len(pcm) % 4
        if extra:
            pcm = pcm[:-extra]
        if not pcm:
            return
        try:
            self._pending.put_nowait(pcm)
        except queue.Full:
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self._pending.put_nowait(pcm)
            except queue.Full:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._pending.put_nowait(b"")
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.4)
        self._shutdown()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                pcm = self._pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if not pcm or self._stop.is_set():
                continue
            if not self._opened and not self._open():
                continue
            self._submit(pcm)

    def _open(self) -> bool:
        if self._open_failed:
            return False
        try:
            winmm = ctypes.WinDLL("winmm")
        except OSError as exc:
            self._open_failed = True
            log(f"audio pemutar {exc}")
            return False
        self._bind(winmm)
        fmt = _WaveFormat()
        fmt.wFormatTag = 1
        fmt.nChannels = 2
        fmt.nSamplesPerSec = 48000
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = 4
        fmt.nAvgBytesPerSec = 48000 * 4
        fmt.cbSize = 0
        handle = wintypes.HANDLE()
        code = winmm.waveOutOpen(
            ctypes.byref(handle),
            wintypes.UINT(0xFFFFFFFF),
            ctypes.byref(fmt),
            ctypes.c_void_p(),
            ctypes.c_void_p(),
            wintypes.DWORD(0),
        )
        if code != 0:
            self._open_failed = True
            log(f"audio pemutar {code}")
            return False
        self._winmm = winmm
        self._handle = handle
        self._opened = True
        return True

    @staticmethod
    def _bind(winmm) -> None:
        header = ctypes.POINTER(_WaveHdr)
        winmm.waveOutOpen.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.UINT,
            ctypes.POINTER(_WaveFormat),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        winmm.waveOutOpen.restype = wintypes.UINT
        for name in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            fn = getattr(winmm, name)
            fn.argtypes = [wintypes.HANDLE, header, wintypes.UINT]
            fn.restype = wintypes.UINT
        for name in ("waveOutReset", "waveOutClose"):
            fn = getattr(winmm, name)
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = wintypes.UINT

    def _submit(self, pcm: bytes) -> None:
        winmm = self._winmm
        if winmm is None:
            return
        slot = self._free_slot()
        if slot is None:
            return
        header, _old = slot
        self._unprepare(header)
        block = ctypes.create_string_buffer(pcm)
        header.lpData = ctypes.cast(block, ctypes.c_void_p)
        header.dwBufferLength = len(pcm)
        header.dwBytesRecorded = len(pcm)
        header.dwFlags = 0
        header.dwLoops = 0
        if winmm.waveOutPrepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header)) != 0:
            return
        self._slots.append((header, block))
        winmm.waveOutWrite(self._handle, ctypes.byref(header), ctypes.sizeof(header))

    def _free_slot(self) -> tuple[_WaveHdr, ctypes.Array] | None:
        kept: list[tuple[_WaveHdr, ctypes.Array]] = []
        free = None
        for header, block in self._slots:
            if header.dwFlags & _WHDR_INQUEUE:
                kept.append((header, block))
                continue
            if free is None:
                free = (header, block)
                continue
            self._unprepare(header)
        self._slots = kept
        if free is not None:
            return free
        if len(self._slots) >= 6:
            return None
        return (_WaveHdr(), ctypes.create_string_buffer(0))

    def _unprepare(self, header: _WaveHdr) -> None:
        winmm = self._winmm
        if winmm is None or not (header.dwFlags & _WHDR_PREPARED):
            return
        winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(header), ctypes.sizeof(header))
        header.dwFlags = 0

    def _shutdown(self) -> None:
        winmm = self._winmm
        if winmm is None or not self._opened:
            return
        try:
            winmm.waveOutReset(self._handle)
            for header, _block in self._slots:
                self._unprepare(header)
            winmm.waveOutClose(self._handle)
        except OSError:
            pass
        self._slots.clear()
        self._opened = False
        self._winmm = None

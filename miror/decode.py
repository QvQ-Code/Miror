"""Low-latency H.264 decode. One thread, no frame reordering."""

from __future__ import annotations

from miror.annexb import to_annexb


class Decoder:
    def __init__(self) -> None:
        self._ctx = None
        self._av = None
        self._reformatter = None
        self._numpy = None
        self._range = None
        self._space = None
        self._area = None
        self.width = 0
        self.height = 0
        self._scratch = bytearray()
        self._open_backend()
        self._warm()

    def _open_backend(self) -> None:
        try:
            import av
        except ImportError as exc:
            raise RuntimeError(
                "Decoder video belum terpasang. Jalankan: python -m pip install av numpy"
            ) from exc
        self._av = av

    def _tools(self):
        if self._reformatter is None:
            from av.video.reformatter import (
                ColorRange,
                Colorspace,
                Interpolation,
                VideoReformatter,
            )

            self._reformatter = VideoReformatter()
            self._range = ColorRange
            self._space = Colorspace
            self._area = Interpolation.AREA
        return self._reformatter

    def _warm(self) -> None:
        """Pay the first color-conversion cost before a live frame arrives."""
        try:
            for width, height in ((424, 960), (544, 1208), (856, 1920)):
                frame = self._av.VideoFrame(width, height, "yuv420p")
                self._to_rgb(frame)
            # The portrait 856 window is the expensive shrink. Build it once.
            tall = self._av.VideoFrame(856, 1904, "yuv420p")
            self._to_rgb(tall, 474, 1052)
        except Exception:
            return

    def _to_rgb(self, frame, out_w: int = 0, out_h: int = 0) -> bytes:
        try:
            reformatter = self._tools()
            src_range = self._range.JPEG
            current = getattr(frame, "color_range", None)
            if current is not None and int(current) not in (0, int(self._range.UNSPECIFIED)):
                src_range = current
            kwargs = {
                "format": "rgb24",
                "src_colorspace": self._space.ITU709,
                "src_color_range": src_range,
            }
            src_w = int(frame.width)
            src_h = int(frame.height)
            # Only 856 asks for a smaller box. Area averaging is the shrink
            # that still fits a 60 fps slot here. Bicubic on this box did not.
            # Two scaler threads, not two decoder threads.
            if (
                out_w > 0
                and out_h > 0
                and out_w <= src_w
                and out_h <= src_h
                and (out_w < src_w or out_h < src_h)
            ):
                kwargs["width"] = int(out_w)
                kwargs["height"] = int(out_h)
                kwargs["interpolation"] = self._area
                kwargs["threads"] = 2
            rgb = reformatter.reformat(frame, **kwargs)
            try:
                return self._keep_plane(rgb)
            except Exception:
                return self._keep(rgb.to_ndarray())
        except Exception:
            return self._keep(frame.to_ndarray(format="rgb24"))

    def _keep_plane(self, frame) -> bytearray:
        # One copy into the reused buffer. A fresh ndarray every frame was
        # collected often enough to stall the corner.
        if frame.format.name != "rgb24":
            raise ValueError(frame.format.name)
        width = int(frame.width)
        height = int(frame.height)
        row = width * 3
        need = row * height
        if width <= 0 or height <= 0 or need <= 0:
            raise ValueError(f"bad rgb {width}x{height}")
        self.width = width
        self.height = height
        if len(self._scratch) != need:
            self._scratch = bytearray(need)
        plane = frame.planes[0]
        stride = int(plane.line_size)
        if stride < row:
            raise ValueError(f"short stride {stride}")
        if self._numpy is None:
            import numpy as np

            self._numpy = np
        np = self._numpy
        src = np.frombuffer(memoryview(plane), dtype=np.uint8, count=stride * height)
        dest = np.frombuffer(self._scratch, dtype=np.uint8).reshape(height, row)
        if stride == row:
            np.copyto(dest, src.reshape(height, row))
        else:
            np.copyto(dest, src.reshape(height, stride)[:, :row])
        return self._scratch

    def _keep(self, array) -> bytearray:
        # 856 RGB is about 5 MB and is not one packed block. A fresh object
        # every frame made the PC pause, and the fps number dipped.
        if self._numpy is None:
            import numpy as np

            self._numpy = np
        np = self._numpy

        height = int(array.shape[0])
        width = int(array.shape[1])
        self.width = width
        self.height = height
        need = height * width * 3
        if len(self._scratch) != need:
            self._scratch = bytearray(need)
        dest = np.frombuffer(self._scratch, dtype=np.uint8).reshape(height, width, 3)
        np.copyto(dest, array[:, :, :3])
        return self._scratch

    def reset(self) -> None:
        self._ctx = None

    def decode(
        self,
        payload: bytes,
        pts: int,
        render: bool = True,
        out_w: int = 0,
        out_h: int = 0,
    ) -> bytes | None:
        data = to_annexb(payload)
        if not data:
            return None
        av = self._av
        if self._ctx is None:
            ctx = av.CodecContext.create("h264", "r")
            # A second thread holds pictures back. An empty decode was then
            # treated as a failure, so 856 rarely got a frame onto the screen.
            ctx.thread_count = 1
            flags = getattr(getattr(av.codec, "context", None), "Flags", None)
            low_delay = getattr(flags, "LOW_DELAY", None) if flags else None
            if low_delay is not None:
                ctx.flags |= low_delay
            ctx.open()
            self._ctx = ctx
        packet = av.Packet(data)
        packet.pts = pts
        packet.dts = pts
        try:
            frames = self._ctx.decode(packet)
            latest = None
            for frame in frames:
                self.width = int(frame.width)
                self.height = int(frame.height)
                if not render:
                    continue
                latest = self._to_rgb(frame, out_w, out_h)
            if not render:
                return b""
            # No picture yet is not a broken stream. The next packet can
            # still produce one. None is reserved for a decode error.
            return latest if latest is not None else b""
        except Exception:
            self.reset()
            return None

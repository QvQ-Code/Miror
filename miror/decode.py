"""Low-latency H.264 decode. One thread, no frame reordering."""

from __future__ import annotations

from miror.annexb import to_annexb


class Decoder:
    def __init__(self) -> None:
        self._ctx = None
        self._av = None
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

    def _warm(self) -> None:
        """Pay the first color-conversion cost before a live frame arrives."""
        try:
            for width, height in ((424, 960), (544, 1208), (856, 1920)):
                frame = self._av.VideoFrame(width, height, "yuv420p")
                self._to_rgb(frame)
        except Exception:
            return

    def _to_rgb(self, frame, out_w: int = 0, out_h: int = 0) -> bytes:
        try:
            from av.video.reformatter import ColorRange, Colorspace, Interpolation

            src_range = ColorRange.JPEG
            current = getattr(frame, "color_range", None)
            if current is not None and int(current) not in (0, int(ColorRange.UNSPECIFIED)):
                src_range = current
            kwargs = {
                "format": "rgb24",
                "src_colorspace": Colorspace.ITU709,
                "src_color_range": src_range,
            }
            src_w = int(frame.width)
            src_h = int(frame.height)
            # Only 856 asks for a smaller box. Scaling here lets the window blit
            # 1:1 instead of smooth-scaling a 5 MB picture on the UI thread.
            if (
                out_w > 0
                and out_h > 0
                and out_w <= src_w
                and out_h <= src_h
                and (out_w < src_w or out_h < src_h)
            ):
                kwargs["width"] = int(out_w)
                kwargs["height"] = int(out_h)
                kwargs["interpolation"] = Interpolation.BICUBIC
            rgb = frame.reformat(**kwargs)
            return self._keep(rgb.to_ndarray())
        except Exception:
            return self._keep(frame.to_ndarray(format="rgb24"))

    def _keep(self, array) -> bytearray:
        # 856 RGB is about 5 MB and is not one packed block. A fresh object
        # every frame made the PC pause, and the fps number dipped.
        import numpy as np

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
            return latest
        except Exception:
            self.reset()
            return None

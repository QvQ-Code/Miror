"""Desktop window: a borderless phone-sized view of the USB mirror."""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from miror.adb_host import AdbHost, Device
from miror.session import MirrorSession
from miror.support import app_icon
from miror.touch import map_touch

_WIDGET_MAX = 16777215
_TIER_FRAME = {1: (424, 960), 2: (544, 1208), 3: (856, 1920)}


def blit_rgb888(target: QImage, source: bytearray, width: int, height: int) -> None:
    """Copy a packed RGB888 buffer into an existing RGB32 image."""

    wrapper = QImage(source, width, height, width * 3, QImage.Format.Format_RGB888)
    painter = QPainter(target)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(0, 0, wrapper)
    finally:
        painter.end()


class ElideLabel(QLabel):
    """A one-line label that can shrink so the phone bar keeps its buttons."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(48, hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(120, hint.height())


class FrameView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self.handler = None
        self.setMinimumSize(1, 1)
        self.setMouseTracking(True)
        self.setAutoFillBackground(True)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.handler is not None:
            self.handler(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.handler is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.handler(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.handler is not None:
            self.handler(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self.handler is not None:
            self.handler(event)
        event.accept()

    def set_frame(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image is None or self._image.isNull():
            painter.setPen(QColor("#8b939c"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Layar ponsel")
            return
        target = self._fit(self._image.width(), self._image.height(), self.width(), self.height())
        slack_w = self.width() - target.width()
        slack_h = self.height() - target.height()
        if 0 <= slack_w <= 4 and 0 <= slack_h <= 4:
            from PySide6.QtCore import QRect

            # A hairline gap reads as a margin. Fill the view instead.
            target = QRect(0, 0, self.width(), self.height())
        near = (
            abs(self._image.width() - target.width()) <= 2
            and abs(self._image.height() - target.height()) <= 2
        )
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not near)
        painter.drawImage(target, self._image)

    @staticmethod
    def _fit(src_w: int, src_h: int, dst_w: int, dst_h: int):
        from PySide6.QtCore import QRect

        if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
            return QRect(0, 0, dst_w, dst_h)
        scale = min(dst_w / src_w, dst_h / src_h)
        width = max(1, int(src_w * scale))
        height = max(1, int(src_h * scale))
        left = (dst_w - width) // 2
        top = (dst_h - height) // 2
        return QRect(left, top, width, height)


class HeaderBar(QWidget):
    """The only place that drags the window. Buttons keep their own clicks."""

    def __init__(self, owner: "MainWindow") -> None:
        super().__init__()
        self.owner = owner
        self.setObjectName("header")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner._header_mouse(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.owner._header_mouse(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner._header_mouse(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner._header_mouse(event)
        event.accept()


class MainWindow(QMainWindow):
    devices_ready = Signal(object)
    boot_done = Signal()
    boot_failed = Signal(str)

    def __init__(self, adb: AdbHost | None = None, start_adb: bool = True) -> None:
        super().__init__()
        self.adb = adb or AdbHost()
        self.session = MirrorSession(self.adb)
        self._devices: list[Device] = []
        self._polling = False
        self._adb_ready = False
        self._shown_seq = -1
        self._status_text = ""
        self._rgb_buf = bytearray()
        self._view_image: QImage | None = None
        self._error = ""
        self._drag_pos: QPoint | None = None
        self._saved_pos: QPoint | None = None
        self._resume_after_stop = False
        self._frame_size = _TIER_FRAME[2]
        self.fps_cap = 60
        self.size_tier = 2
        self.show_fps = True
        self._finger = False
        self._last_touch = 0.0
        self._last_point: tuple[int, int] | None = None
        self.setWindowTitle("Miror")
        icon_file = app_icon()
        if icon_file.is_file():
            icon = QIcon(str(icon_file))
            self.setWindowIcon(icon)
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(icon)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMouseTracking(True)
        self._build()
        self.session.status.connect(self.status.setText)
        self.session.failed.connect(self._on_failed)
        self.session.finished.connect(self._on_finished)
        self.devices_ready.connect(self._apply_devices)
        self.boot_done.connect(self._on_boot)
        self.boot_failed.connect(self._on_boot_failed)
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, self._leave_fullscreen)
        if start_adb:
            self.status.setText("Menyiapkan ADB...")
            threading.Thread(target=self._boot, name="miror-boot", daemon=True).start()
        else:
            self.status.setText("ADB tidak dijalankan.")
        self._sync_buttons()
        self._mark_fps()
        self._mark_size()
        self._apply_phone_size(*self._frame_size, center=True)

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = HeaderBar(self)
        self.header.setFixedHeight(28)
        row = QHBoxLayout(self.header)
        row.setContentsMargins(6, 3, 4, 3)
        row.setSpacing(4)
        self.status = ElideLabel("Menyiapkan...")
        self.status.setObjectName("status")
        self.status.setWordWrap(False)
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status, 1)

        self.size_group = QFrame()
        self.size_group.setObjectName("fpsGroup")
        self.size_group.setFrameShape(QFrame.Shape.NoFrame)
        size_row = QHBoxLayout(self.size_group)
        size_row.setContentsMargins(2, 0, 2, 0)
        size_row.setSpacing(1)
        self.size_424 = QPushButton("424")
        self.size_544 = QPushButton("544")
        self.size_856 = QPushButton("856")
        self.size_buttons = QButtonGroup(self)
        self.size_buttons.setExclusive(True)
        for tier, button in ((1, self.size_424), (2, self.size_544), (3, self.size_856)):
            button.setObjectName("fps")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(22)
            self.size_buttons.addButton(button, tier)
            size_row.addWidget(button)
        self.size_buttons.idClicked.connect(self.set_size)
        row.addWidget(self.size_group)

        self.fps_group = QFrame()
        self.fps_group.setObjectName("fpsGroup")
        self.fps_group.setFrameShape(QFrame.Shape.NoFrame)
        fps_row = QHBoxLayout(self.fps_group)
        fps_row.setContentsMargins(2, 0, 2, 0)
        fps_row.setSpacing(1)
        self.fps_30 = QPushButton("30")
        self.fps_60 = QPushButton("60")
        for button in (self.fps_30, self.fps_60):
            button.setObjectName("fps")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(22)
        self.fps_buttons = QButtonGroup(self)
        self.fps_buttons.setExclusive(True)
        self.fps_buttons.addButton(self.fps_30, 30)
        self.fps_buttons.addButton(self.fps_60, 60)
        self.fps_buttons.idClicked.connect(self.set_fps)
        fps_row.addWidget(self.fps_30)
        fps_row.addWidget(self.fps_60)
        row.addWidget(self.fps_group)

        self.fps_toggle = QPushButton("FPS")
        self.fps_toggle.setObjectName("fps")
        self.fps_toggle.setCheckable(True)
        self.fps_toggle.setChecked(True)
        self.fps_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fps_toggle.setFixedHeight(22)
        self.fps_toggle.toggled.connect(self._on_fps_toggled)
        row.addWidget(self.fps_toggle)

        self.start_btn = QPushButton("Mulai")
        self.start_btn.setObjectName("start")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setFixedHeight(22)
        self.start_btn.clicked.connect(self.start_mirror)
        row.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Berhenti")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setFixedHeight(22)
        self.stop_btn.clicked.connect(self.stop_mirror)
        self.stop_btn.setEnabled(False)
        row.addWidget(self.stop_btn)
        self.devices = QComboBox()
        self.devices.setObjectName("devices")
        self.devices.setVisible(False)
        self.devices.setMaximumWidth(96)
        self.devices.setFixedHeight(22)
        row.addWidget(self.devices)
        self.min_btn = QPushButton("–")
        self.min_btn.setObjectName("icon")
        self.min_btn.setFixedSize(22, 22)
        self.min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_btn.clicked.connect(self.showMinimized)
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("close")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.min_btn)
        row.addWidget(self.close_btn)
        layout.addWidget(self.header)

        self.view = FrameView()
        self.view.handler = self._view_mouse
        layout.addWidget(self.view, 1)

        self.fps_hud = QLabel(self.view)
        self.fps_hud.setObjectName("fpsHud")
        self.fps_hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.fps_hud.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.fps_hud.hide()

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._present)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1500)
        self.poll_timer.timeout.connect(self.refresh)
        self.setStyleSheet(_STYLE)
        self.setFont(QFont("Segoe UI", 10))

    def _header_reserve(self) -> int:
        header = getattr(self, "header", None)
        if header is None:
            return 76
        hint = header.sizeHint().height()
        return hint if hint > 0 else 76

    def phone_size(self, frame_w: int, frame_h: int) -> tuple[int, int]:
        if frame_w <= 0 or frame_h <= 0:
            frame_w, frame_h = 1080, 2408
        screen = self.screen()
        if screen is None:
            avail_w, avail_h = 1280, 800
        else:
            area = screen.availableGeometry()
            avail_w, avail_h = area.width(), area.height()
        # The header sits above the picture. A frame that still fits under it
        # stays 1:1. A frame that does not fit uses the whole work area, with
        # no extra side gap.
        picture_h = max(1, avail_h - self._header_reserve())
        if frame_w <= avail_w and frame_h <= picture_h:
            return frame_w, frame_h
        max_h = min(frame_h, max(360, picture_h))
        max_w = min(frame_w, max(240, avail_w))
        width = max(1, int(round(max_h * frame_w / frame_h)))
        height = max_h
        if width > max_w:
            width = max_w
            height = max(1, int(round(width * frame_h / frame_w)))
        return width, height

    def _apply_phone_size(self, frame_w: int, frame_h: int, center: bool = False) -> None:
        self._frame_size = (frame_w, frame_h)
        if self.isFullScreen():
            self._publish_draw_size(frame_w, frame_h)
            return
        width, height = self.phone_size(frame_w, frame_h)
        reserve = self._header_reserve()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_WIDGET_MAX, _WIDGET_MAX)
        self.setFixedSize(width, height + reserve)
        self._publish_draw_size(frame_w, frame_h)
        if center:
            screen = self.screen()
            if screen is not None:
                area = screen.availableGeometry()
                x = area.x() + max(0, (area.width() - width) // 2)
                y = area.y() + max(0, (area.height() - (height + reserve)) // 2)
                self.move(x, y)
        self._place_fps_hud()

    def _place_fps_hud(self) -> None:
        self.fps_hud.adjustSize()
        hint = self.fps_hud.sizeHint()
        self.fps_hud.setGeometry(10, 8, max(52, hint.width()), max(22, hint.height()))
        self.fps_hud.raise_()

    def _set_fps_readout(self, fps: int) -> None:
        text = f"{int(fps)} fps"
        if self.fps_hud.text() != text:
            self.fps_hud.setText(text)
            self._place_fps_hud()
        if not self.show_fps:
            self.fps_hud.hide()
            return
        if not self.fps_hud.isVisible():
            self.fps_hud.show()
            self.fps_hud.raise_()

    def _on_fps_toggled(self, checked: bool) -> None:
        self.show_fps = bool(checked)
        if not self.show_fps:
            self.fps_hud.hide()
        elif self.fps_hud.text() and self.session.running:
            self.fps_hud.show()
            self._place_fps_hud()

    def _header_mouse(self, event) -> bool:
        kind = event.type()
        left = Qt.MouseButton.LeftButton
        if kind == QEvent.Type.MouseButtonDblClick and event.button() == left:
            self._drag_pos = None
            self.header.setCursor(Qt.CursorShape.OpenHandCursor)
            self.toggle_fullscreen()
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == left:
            if not self.isFullScreen():
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self.header.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if kind == QEvent.Type.MouseMove:
            if (
                self._drag_pos is not None
                and event.buttons() & left
                and not self.isFullScreen()
            ):
                self.move(event.globalPosition().toPoint() - self._drag_pos)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._drag_pos = None
            self.header.setCursor(Qt.CursorShape.OpenHandCursor)
            return True
        return False

    def _view_mouse(self, event) -> bool:
        kind = event.type()
        left = Qt.MouseButton.LeftButton
        if kind == QEvent.Type.MouseButtonDblClick:
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == left:
            self._touch_down(event.position())
            return True
        if kind == QEvent.Type.MouseMove and self._finger:
            self._touch_move(event.position(), throttle=True)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == left and self._finger:
            self._touch_move(event.position(), throttle=False)
            self._touch_up(event.position())
            return True
        return False

    def _touch_point(self, pos, clamp: bool) -> tuple[int, int] | None:
        if not self.session.running:
            return None
        panel_w, panel_h = self.session.panel
        if panel_w <= 0 or panel_h <= 0:
            return None
        frame_w, frame_h = self._frame_size
        return map_touch(
            pos.x(),
            pos.y(),
            self.view.width(),
            self.view.height(),
            frame_w,
            frame_h,
            panel_w,
            panel_h,
            clamp=clamp,
        )

    def _touch_down(self, pos) -> None:
        point = self._touch_point(pos, clamp=False)
        if point is None:
            return
        self._last_point = point
        self.session.touch.down(*point)
        self._finger = True
        self._last_touch = time.monotonic()

    def _touch_move(self, pos, throttle: bool) -> None:
        if not self._finger:
            return
        now = time.monotonic()
        if throttle and now - self._last_touch < 0.02:
            return
        point = self._touch_point(pos, clamp=True)
        if point is None:
            return
        self._last_point = point
        self.session.touch.move(*point)
        self._last_touch = now

    def _touch_up(self, pos) -> None:
        if not self._finger:
            return
        point = self._touch_point(pos, clamp=True) or self._last_point
        self._finger = False
        if point is None:
            return
        self.session.touch.up(*point)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_fps_hud()
        self._publish_draw_size()

    def _publish_draw_size(self, frame_w: int = 0, frame_h: int = 0) -> None:
        if frame_w <= 0 or frame_h <= 0:
            frame_w, frame_h = self._frame_size
        view_w = self.view.width()
        view_h = self.view.height()
        if view_w < 160 or view_h < 160:
            self.session.draw_size = (0, 0)
            return
        rect = FrameView._fit(frame_w, frame_h, view_w, view_h)
        self.session.draw_size = (rect.width(), rect.height())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._place_fps_hud()

    def _boot(self) -> None:
        try:
            self.adb.ensure_binary()
            self.adb.start_server()
        except Exception as exc:
            self.boot_failed.emit(str(exc))
            return
        self.boot_done.emit()

    def _on_boot(self) -> None:
        self._adb_ready = True
        self.status.setText("Colokkan ponsel lewat USB.")
        self.poll_timer.start()
        self.refresh()

    def _on_boot_failed(self, message: str) -> None:
        self.status.setText("ADB gagal. " + message)

    def refresh(self) -> None:
        if not self._adb_ready or self._polling:
            return
        self._polling = True

        def work() -> None:
            try:
                devices = self.adb.devices()
            except Exception as exc:
                self.boot_failed.emit(str(exc))
                devices = []
            finally:
                self._polling = False
            self.devices_ready.emit(devices)

        threading.Thread(target=work, name="miror-poll", daemon=True).start()

    def _apply_devices(self, devices: list[Device]) -> None:
        self._devices = devices
        if self.session.running:
            alive = any(item.serial == self.session.serial and item.state == "device" for item in devices)
            if not alive:
                self._resume_after_stop = False
                self.session.stop()
                self._error = "Ponsel terputus."
            return
        current = self.devices.currentData()
        self.devices.blockSignals(True)
        self.devices.clear()
        for device in devices:
            self.devices.addItem(device.label, device.serial)
        if current:
            index = self.devices.findData(current)
            if index >= 0:
                self.devices.setCurrentIndex(index)
        self.devices.blockSignals(False)
        self.devices.setVisible(len(devices) > 1)
        self._describe(devices)
        self._sync_buttons()

    def _describe(self, devices: list[Device]) -> None:
        if self.session.running:
            return
        if not devices:
            self.status.setText("Tidak ada ponsel USB.")
            return
        selected = self._selected()
        if selected is None:
            return
        if selected.state == "unauthorized":
            self.status.setText("Ketuk Izinkan di ponsel.")
        elif selected.state == "device":
            self.status.setText("Siap. Tekan Mulai.")
        else:
            self.status.setText(f"Ponsel {selected.state}.")

    def _selected(self) -> Device | None:
        serial = self.devices.currentData()
        for device in self._devices:
            if device.serial == serial:
                return device
        return None

    def set_fps(self, fps: int) -> None:
        fps = 30 if int(fps) <= 30 else 60
        changed = fps != self.fps_cap
        self.fps_cap = fps
        self._mark_fps()
        if not changed or not self.session.running:
            return
        self._resume_after_stop = True
        self.status.setText(f"Mengganti ke {fps} fps...")
        self._finger = False
        self.session.stop()

    def _mark_fps(self) -> None:
        self.fps_30.setChecked(self.fps_cap == 30)
        self.fps_60.setChecked(self.fps_cap == 60)

    def set_size(self, tier: int) -> None:
        tier = int(tier) if int(tier) in (1, 2, 3) else 1
        changed = tier != self.size_tier
        self.size_tier = tier
        self._mark_size()
        self._frame_size = _TIER_FRAME[tier]
        if not self.session.running and not self.isFullScreen():
            self._apply_phone_size(*self._frame_size, center=False)
        if not changed or not self.session.running:
            return
        label = f"{_TIER_FRAME[tier][0]}×{_TIER_FRAME[tier][1]}"
        self._resume_after_stop = True
        self.status.setText(f"Mengganti ke {label}...")
        self._finger = False
        self.session.stop()

    def _mark_size(self) -> None:
        self.size_424.setChecked(self.size_tier == 1)
        self.size_544.setChecked(self.size_tier == 2)
        self.size_856.setChecked(self.size_tier == 3)

    def start_mirror(self) -> None:
        device = self._selected()
        if device is None or device.state != "device" or self.session.running:
            return
        self._error = ""
        self._resume_after_stop = False
        self._shown_seq = -1
        self.view.set_frame(None)
        self._publish_draw_size()
        self.status.setText("Menyambungkan...")
        self.session.start(device.serial, self.fps_cap, self.size_tier)
        self.timer.start()
        self._sync_buttons()

    def stop_mirror(self) -> None:
        self._resume_after_stop = False
        self._finger = False
        self.session.stop()

    def _on_failed(self, message: str) -> None:
        self._error = message
        self._resume_after_stop = False

    def _on_finished(self) -> None:
        resume = self._resume_after_stop
        self._resume_after_stop = False
        if resume and not self._error:
            device = self._selected()
            if device is not None and device.state == "device":
                self.session.start(device.serial, self.fps_cap, self.size_tier)
                if not self.timer.isActive():
                    self.timer.start()
                self._sync_buttons()
                self.status.setText(f"Mengganti ke {self.fps_cap} fps...")
                return
        self.timer.stop()
        self._finger = False
        self.fps_hud.hide()
        self._sync_buttons()
        if self._error:
            self.status.setText(self._error)
            self._error = ""
        elif not self.session.running:
            self.status.setText("Mirror berhenti.")

    def _present(self) -> None:
        info = self.session.frames.describe()
        if info is None:
            return
        nbytes, label_w, label_h, seq, fps, _bmp_w, _bmp_h = info
        if label_w <= 0 or label_h <= 0:
            return
        if seq == self._shown_seq:
            self._set_fps_readout(fps)
            text = f"{label_w}×{label_h}"
            if text != self._status_text:
                self._status_text = text
                self.status.setText(text)
            return
        if (label_w, label_h) != self._frame_size:
            self._apply_phone_size(label_w, label_h, center=False)
        else:
            self._publish_draw_size(label_w, label_h)
        if len(self._rgb_buf) != nbytes:
            self._rgb_buf = bytearray(nbytes)
        copied = self.session.frames.copy_front(self._rgb_buf)
        if copied is None:
            return
        width, height, seq, fps = copied
        # The buffer is the picture we draw. For 856 it is already the window
        # box, so paint is a 1:1 blit. The status keeps the encoder size.
        self._view_image = QImage(
            self._rgb_buf, width, height, width * 3, QImage.Format.Format_RGB888
        )
        self._shown_seq = seq
        self.view.set_frame(self._view_image)
        self._set_fps_readout(fps)
        text = f"{label_w}×{label_h}"
        if text != self._status_text:
            self._status_text = text
            self.status.setText(text)

    def _sync_buttons(self) -> None:
        running = self.session.running
        device = self._selected()
        ready = device is not None and device.state == "device" and self._adb_ready
        self.start_btn.setEnabled(ready and not running)
        self.start_btn.setVisible(not running)
        self.stop_btn.setEnabled(running)
        self.stop_btn.setVisible(running)
        self.devices.setEnabled(not running)
        if not running:
            self.devices.setVisible(len(self._devices) > 1)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._leave_fullscreen()
            return
        self._saved_pos = self.pos()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_WIDGET_MAX, _WIDGET_MAX)
        self.showFullScreen()
        self._place_fps_hud()

    def _leave_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        self.showNormal()
        frame_w, frame_h = self._frame_size
        self._apply_phone_size(frame_w, frame_h, center=False)
        if self._saved_pos is not None:
            self.move(self._saved_pos)
        self._place_fps_hud()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._resume_after_stop = False
        self.session.stop()
        thread = self.session._thread
        if thread is not None:
            thread.join(timeout=2)
        try:
            self.adb.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        width, height = self.phone_size(*self._frame_size)
        return QSize(width, height + self._header_reserve())


_STYLE = """
QMainWindow, QWidget { background: #000000; color: #f3f5f7; }
#header { background: #14161b; }
#status { background: transparent; color: #f3f5f7; font-size: 11px; }
#fpsHud {
    background: rgba(18, 20, 24, 188);
    color: #f4f6f8;
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 12px;
    font-weight: 600;
}
#fpsGroup { background: #12141a; border-radius: 8px; }
#fps {
    background: transparent;
    color: #b7bec8;
    border: none;
    border-radius: 6px;
    padding: 0 5px;
    font-size: 11px;
    font-weight: 600;
    min-width: 0px;
}
#fps:checked { background: #f4f6f8; color: #121316; }
#fps:hover { color: #ffffff; }
#fps:checked:hover { color: #121316; }
#start, #stop {
    border-radius: 8px;
    padding: 0 8px;
    min-height: 0px;
    font-size: 11px;
    font-weight: 600;
}
#start { background: #f4f6f8; color: #121316; border: none; }
#start:hover { background: #ffffff; }
#start:disabled { background: #2a2e36; color: #8b939e; }
#stop { background: transparent; color: #f4f6f8; border: 1px solid rgba(255, 255, 255, 0.16); }
#stop:hover { background: rgba(255, 255, 255, 0.08); }
#icon, #close {
    background: transparent;
    color: #f4f6f8;
    border: none;
    border-radius: 6px;
    padding: 0;
    font-size: 13px;
    font-weight: 600;
}
#icon:hover { background: rgba(255, 255, 255, 0.08); }
#close:hover { background: #c4453c; color: #ffffff; }
QComboBox {
    background: #12141a;
    border: none;
    border-radius: 10px;
    padding: 4px 8px;
    min-height: 22px;
}
QComboBox::drop-down { border: none; }
"""

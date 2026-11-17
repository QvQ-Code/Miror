"""The window builds and the idle controls are in place."""

from __future__ import annotations

import os
import unittest


class WindowTests(unittest.TestCase):
    def test_idle_window(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QImage, QMouseEvent
        from PySide6.QtWidgets import QApplication

        from miror.support import app_icon
        from miror.ui import MainWindow, blit_rgb888

        app = QApplication.instance() or QApplication([])
        window = MainWindow(start_adb=False)
        window.show()
        app.processEvents()
        self.assertEqual(window.windowTitle(), "Miror")
        self.assertEqual(window.start_btn.text(), "Mulai")
        self.assertEqual(window.stop_btn.text(), "Berhenti")
        self.assertFalse(window.start_btn.isEnabled())
        self.assertIn("ADB", window.status.text())
        self.assertEqual(window.fps_cap, 60)
        self.assertTrue(window.fps_60.isChecked())
        self.assertFalse(window.fps_30.isChecked())
        self.assertEqual(window.size_tier, 2)
        self.assertTrue(window.size_544.isChecked())
        self.assertFalse(window.size_424.isChecked())
        self.assertFalse(window.size_856.isChecked())
        self.assertGreater(window.height(), window.width())
        self.assertLess(window.width(), 500)
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(window.fps_hud.isVisible())
        self.assertTrue(window.fps_hud.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        window._set_fps_readout(56)
        self.assertEqual(window.fps_hud.text(), "56 fps")
        self.assertTrue(window.fps_hud.isVisible())
        self.assertLess(window.fps_hud.x(), 20)
        self.assertLess(window.fps_hud.y(), 20)
        self.assertIs(window.fps_hud.parentWidget(), window.view)
        self.assertIs(window.start_btn.parentWidget(), window.header)
        self.assertLess(window.header.y(), window.view.y())
        self.assertLessEqual(window.header.height(), 30)
        picture_w, picture_h = window.phone_size(*window._frame_size)
        self.assertEqual(window.view.width(), picture_w)
        self.assertEqual(window.view.height(), picture_h)
        wide, short = window.phone_size(window.screen().availableGeometry().width() + 200, 300)
        self.assertEqual(wide, window.screen().availableGeometry().width())
        self.assertLess(short, 300)
        window._apply_phone_size(700, 320)
        app.processEvents()
        self.assertEqual(window.view.width(), 700)
        self.assertEqual(window.view.height(), 320)
        self.assertTrue(window.fps_toggle.isChecked())
        self.assertTrue(window.audio_btn.isChecked())
        self.assertTrue(window.session.audio_on)
        self.assertEqual(window.audio_btn.focusPolicy(), Qt.FocusPolicy.NoFocus)
        window.audio_btn.setChecked(False)
        self.assertFalse(window.session.audio_on)
        window.audio_btn.setChecked(True)
        self.assertTrue(window.session.audio_on)
        self.assertTrue(app_icon().is_file())
        self.assertFalse(window.windowIcon().isNull())
        window.fps_toggle.setChecked(False)
        self.assertFalse(window.show_fps)
        self.assertFalse(window.fps_hud.isVisible())
        window.fps_toggle.setChecked(True)
        window._set_fps_readout(56)
        self.assertTrue(window.fps_hud.isVisible())
        self._check_drag_and_touch(window)
        window.set_fps(30)
        self.assertEqual(window.fps_cap, 30)
        self.assertTrue(window.fps_30.isChecked())
        window.set_size(1)
        self.assertEqual(window.size_tier, 1)
        self.assertTrue(window.size_424.isChecked())
        self.assertEqual(window._frame_size, (424, 960))
        self.assertEqual(window.phone_size(100, 180), (100, 180))
        self.assertFalse(window.session.running)
        image = QImage(4, 2, QImage.Format.Format_RGB32)
        buf = bytearray(4 * 2 * 3)
        buf[0:3] = bytes((255, 0, 0))
        blit_rgb888(image, buf, 4, 2)
        color = image.pixelColor(0, 0)
        self.assertGreater(color.red(), 250)
        self.assertLess(color.green(), 5)
        self.assertLess(color.blue(), 5)
        window.close()

    def test_corner_fps_is_logged_once_a_second(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from miror.ui import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow(start_adb=False)
        notes: list[str] = []
        window.session.running = True
        window._hud_log_at = 0.0
        original = __import__("miror.ui", fromlist=["log"]).log
        try:
            import miror.ui as ui

            ui.log = notes.append
            window._set_fps_readout(40)
            window._set_fps_readout(18)
            self.assertEqual(notes, ["hud 40 fps"])
            self.assertEqual(window.fps_hud.text(), "18 fps")
            window._hud_log_at = 0.0
            window._set_fps_readout(18)
            self.assertEqual(notes, ["hud 40 fps", "hud 18 fps"])
            window.session.running = False
            window._hud_log_at = 0.0
            window._set_fps_readout(12)
            self.assertEqual(notes, ["hud 40 fps", "hud 18 fps"])
        finally:
            import miror.ui as ui

            ui.log = original
            window.close()
        app.processEvents()

    def _check_drag_and_touch(self, window) -> None:
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        def mouse(kind, local, buttons):
            return QMouseEvent(
                kind,
                QPointF(local[0], local[1]),
                QPointF(400, 400),
                Qt.MouseButton.LeftButton,
                buttons,
                Qt.KeyboardModifier.NoModifier,
            )

        left = Qt.MouseButton.LeftButton
        app = QApplication.instance()
        window._drag_pos = None
        app.sendEvent(window.view, mouse(QEvent.Type.MouseButtonPress, (12, 12), left))
        self.assertIsNone(window._drag_pos)
        app.sendEvent(window.view, mouse(QEvent.Type.MouseButtonDblClick, (12, 12), left))
        self.assertFalse(window.isFullScreen())
        app.sendEvent(window.header, mouse(QEvent.Type.MouseButtonPress, (8, 8), left))
        self.assertIsNotNone(window._drag_pos)
        app.sendEvent(
            window.header,
            mouse(QEvent.Type.MouseButtonRelease, (8, 8), Qt.MouseButton.NoButton),
        )
        self.assertIsNone(window._drag_pos)

        class Recorder:
            def __init__(self) -> None:
                self.events: list[tuple] = []

            def down(self, x: int, y: int) -> None:
                self.events.append(("down", x, y))

            def move(self, x: int, y: int) -> None:
                self.events.append(("move", x, y))

            def up(self, x: int, y: int) -> None:
                self.events.append(("up", x, y))

            def close(self) -> None:
                return None

            def keys(self, line: bytes) -> None:
                self.events.append(("key", line))

        recorder = Recorder()
        window.session.touch = recorder
        window.session.running = True
        view_w = window.view.width()
        view_h = window.view.height()
        self.assertGreater(view_w, 16)
        self.assertGreater(view_h, 16)
        window._frame_size = (view_w, view_h)
        window.session.panel = (view_w * 2, view_h * 2)
        app.sendEvent(
            window.view,
            mouse(QEvent.Type.MouseButtonPress, (view_w / 2, view_h / 2), left),
        )
        self.assertEqual(recorder.events[0][0], "down")
        self.assertAlmostEqual(recorder.events[0][1], view_w, delta=2)
        self.assertAlmostEqual(recorder.events[0][2], view_h, delta=2)
        self.assertTrue(window._finger)
        window._frame_size = (10, view_h)
        window._finger = False
        recorder.events.clear()
        app.sendEvent(window.view, mouse(QEvent.Type.MouseButtonPress, (0, view_h / 2), left))
        self.assertEqual(recorder.events, [])
        self.assertFalse(window._finger)
        from PySide6.QtGui import QKeyEvent

        app.sendEvent(
            window.view,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"),
        )
        app.sendEvent(
            window.view,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier),
        )
        app.sendEvent(
            window.view,
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_A,
                Qt.KeyboardModifier.ControlModifier,
                "\x01",
            ),
        )
        self.assertEqual(
            recorder.events,
            [
                ("key", b"input keyboard text 'a'\n"),
                ("key", b"input keyevent KEYCODE_DEL\n"),
                ("key", b"input keycombination KEYCODE_CTRL_LEFT KEYCODE_A\n"),
            ],
        )
        window.session.running = False
        recorder.events.clear()
        app.sendEvent(
            window,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"),
        )
        self.assertEqual(recorder.events, [])


if __name__ == "__main__":
    unittest.main()

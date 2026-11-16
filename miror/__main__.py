"""Launch Miror."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if "--check" in args:
        import unittest
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    from miror.support import log

    log("proses mulai")
    try:
        from PySide6.QtWidgets import QApplication

        from miror.ui import MainWindow

        app = QApplication(args)
        app.setApplicationName("Miror")
        window = MainWindow()
        window.show()
        code = app.exec()
        log(f"proses berhenti {code}")
        return code
    except Exception:
        import traceback

        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Paths and a small log shared by the desktop app."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return Path(bundled)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def agent_jar() -> Path:
    return project_root() / "agent" / "miror-agent.jar"


def app_icon() -> Path:
    return project_root() / "assets" / "miror.ico"


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "Miror"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return app_dir() / "miror.log"


def log(message: str) -> None:
    line = time.strftime("%H:%M:%S ") + message
    print(line, flush=True)
    try:
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass

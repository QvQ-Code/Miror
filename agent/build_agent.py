"""Compile the phone agent into agent/miror-agent.jar."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
STUBS = ROOT / "stubs"
STUB_CLASSES = ROOT / "stubclasses"
CLASSES = ROOT / "classes"
JAR = ROOT / "miror-agent.jar"


def javac() -> str:
    found = shutil.which("javac")
    if found:
        return found
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "javac.exe"
        if candidate.exists():
            return str(candidate)
    raise SystemExit("javac tidak ditemukan. Pasang JDK 17 dan set JAVA_HOME.")


def jar_tool(javac_path: str) -> str:
    return str(Path(javac_path).with_name("jar.exe"))


def r8_jar() -> Path:
    override = os.environ.get("MIROR_R8")
    if override and Path(override).exists():
        return Path(override)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Miror" / "build-tools" / "r8.jar"
    if local.exists():
        return local
    raise SystemExit("r8.jar tidak ditemukan. Letakkan di %LOCALAPPDATA%\\Miror\\build-tools\\r8.jar")


def main() -> int:
    compiler = javac()
    if STUB_CLASSES.exists():
        shutil.rmtree(STUB_CLASSES)
    if CLASSES.exists():
        shutil.rmtree(CLASSES)
    STUB_CLASSES.mkdir()
    CLASSES.mkdir()
    stubs = [str(path) for path in STUBS.rglob("*.java")]
    sources = [str(path) for path in SRC.rglob("*.java")]
    subprocess.check_call([compiler, "--release", "8", "-encoding", "UTF-8", "-d", str(STUB_CLASSES), *stubs])
    subprocess.check_call(
        [
            compiler,
            "--release",
            "8",
            "-encoding",
            "UTF-8",
            "-classpath",
            str(STUB_CLASSES),
            "-d",
            str(CLASSES),
            *sources,
        ]
    )
    if JAR.exists():
        JAR.unlink()
    dex_dir = ROOT / "dex"
    stub_jar = ROOT / "stub.jar"
    if dex_dir.exists():
        shutil.rmtree(dex_dir)
    dex_dir.mkdir()
    packer = jar_tool(compiler)
    subprocess.check_call([packer, "cf", str(stub_jar), "-C", str(STUB_CLASSES), "android"])
    classes = [str(path) for path in CLASSES.rglob("*.class")]
    java = str(Path(compiler).with_name("java.exe"))
    subprocess.check_call(
        [
            java,
            "-cp",
            str(r8_jar()),
            "com.android.tools.r8.D8",
            "--min-api",
            "26",
            "--lib",
            str(stub_jar),
            "--output",
            str(dex_dir),
            *classes,
        ]
    )
    subprocess.check_call([packer, "cf", str(JAR), "-C", str(dex_dir), "classes.dex"])
    print(f"wrote {JAR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

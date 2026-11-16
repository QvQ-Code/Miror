# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

spec_root = os.path.dirname(SPEC)
av_datas, av_binaries, av_hidden = collect_all("av")

a = Analysis(
    [os.path.join(spec_root, "miror", "__main__.py")],
    pathex=[spec_root],
    binaries=av_binaries,
    datas=[
        (os.path.join(spec_root, "agent", "miror-agent.jar"), "agent"),
        (os.path.join(spec_root, "assets", "miror.ico"), "assets"),
        *av_datas,
    ],
    hiddenimports=av_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Miror",
    icon=os.path.join(spec_root, "assets", "miror.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Miror",
)

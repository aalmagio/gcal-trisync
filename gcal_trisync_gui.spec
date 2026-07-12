# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_submodules


python_home = sys.base_prefix
os.environ.setdefault("TCL_LIBRARY", os.path.join(python_home, "tcl", "tcl8.6"))
os.environ.setdefault("TK_LIBRARY", os.path.join(python_home, "tcl", "tk8.6"))

hiddenimports = (
    collect_submodules("googleapiclient")
    + collect_submodules("google_auth_oauthlib")
    + collect_submodules("google.oauth2")
)

a = Analysis(
    ["gcal_trisync_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gcal-trisync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

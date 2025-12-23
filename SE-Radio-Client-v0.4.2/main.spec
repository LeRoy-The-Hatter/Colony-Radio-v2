# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SE Radio Client.
- Bundles key assets (icons + overlay PNGs)
- Includes default/user config JSON alongside the exe
- Adds pynput backends explicitly for reliable global hotkeys
"""
from pathlib import Path

_spec_file = Path(globals().get("__file__", Path.cwd() / "main.spec")).resolve()
project_dir = _spec_file.parent
asset_dir = project_dir / "app" / "assets"

# Assets required by the Tkinter overlay and window icon
asset_files = [
    "icon.ico",
    "icon.png",
    "radio_UI.png",
    "radio_UI_knob.png",
    "radio_UI_whitekey_2x.png",
]

datas = []
for fname in asset_files:
    datas.append((str(asset_dir / fname), str(Path("app") / "assets")))

# Ship defaults + user config beside the exe so settings persist
for fname in ["config_default.json", "config_user.json"]:
    datas.append((str(project_dir / fname), "."))

# Bundle UI/UX audio cues
datas.append((str(project_dir / "Audio"), "Audio"))

a = Analysis(
    ['main.py'],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
    ],
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
    name='Colony Radio v0.6.8', 
    icon=str(asset_dir / "icon.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

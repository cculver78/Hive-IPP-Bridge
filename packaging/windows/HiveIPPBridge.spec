# PyInstaller onedir build for the CLI and Windows service modes.
from pathlib import Path

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ


ROOT = Path(SPEC).resolve().parents[2]
ENTRYPOINT = ROOT / "src" / "hive_ipp_bridge" / "__main__.py"

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ntsecuritycon",
        "pywintypes",
        "win32con",
        "servicemanager",
        "win32api",
        "win32event",
        "win32file",
        "win32pipe",
        "win32security",
        "win32service",
        "win32serviceutil",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="HiveIPPBridge",
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HiveIPPBridge",
)

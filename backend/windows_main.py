"""
windows_main.py — Windows EXE entry point for AllDebrid-Client.

This module is used exclusively by PyInstaller when building the Windows EXE.
It patches platform-specific defaults (Linux paths → Windows paths) BEFORE any
other import resolves them, then hands off to the regular main module.

Docker / Linux behaviour is completely unchanged — this file is never imported
in that environment.
"""

import os
import sys
import pathlib

# ── Ensure the bundled backend package is on sys.path ───────────────────────
# PyInstaller places collected files under sys._MEIPASS when running frozen.
if getattr(sys, "frozen", False):
    _base = pathlib.Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    _base = pathlib.Path(__file__).parent

if str(_base) not in sys.path:
    sys.path.insert(0, str(_base))

# ── Default data directory: %APPDATA%\AllDebrid-Client ──────────────────────
_appdata = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home())) / "AllDebrid-Client"
_config_dir = _appdata / "config"
_data_dir   = _appdata / "data"
_dl_dir     = _appdata / "downloads"

# Create directories on first run so the app never crashes on missing paths
for _d in [_config_dir, _data_dir / "watch", _data_dir / "processed",
           _data_dir / "aria2", _dl_dir]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Patch environment variables before config.py reads them ─────────────────
# These only take effect if the user has not already set the variable.
os.environ.setdefault("CONFIG_PATH",     str(_config_dir / "config.json"))
os.environ.setdefault("DB_PATH",         str(_data_dir   / "alldebrid.db"))
os.environ.setdefault("WATCH_FOLDER",    str(_data_dir   / "watch"))
os.environ.setdefault("PROCESSED_FOLDER",str(_data_dir   / "processed"))
os.environ.setdefault("DOWNLOAD_FOLDER", str(_dl_dir))
os.environ.setdefault("ARIA2_LOG",       str(_data_dir   / "aria2" / "aria2.log"))
os.environ.setdefault("ARIA2_SESSION",   str(_data_dir   / "aria2" / "aria2.session"))

# ── Patch config.py defaults at module level before first import ─────────────
# We monkey-patch the module-level CONFIG_PATH constant used by core/config.py
# so that every subsequent import picks up the Windows path automatically.
import importlib, types

_config_mod = types.ModuleType("core.config_pre")  # dummy — just to trigger path patch

# Directly set the env var that config.py reads:
# CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.json"))
# Since we already set CONFIG_PATH above via os.environ.setdefault, this works.

# ── Also patch the AppSettings defaults that hardcode /app/… paths ───────────
# We do this by subclassing / monkey-patching AFTER import.
import core.config as _cfg_mod

# Patch CONFIG_PATH constant in module namespace
_cfg_mod.CONFIG_PATH = pathlib.Path(os.environ["CONFIG_PATH"])

# Override Linux-specific defaults in AppSettings
_AppSettings = _cfg_mod.AppSettings
_AppSettings.model_fields["watch_folder"].default    = str(_data_dir / "watch")
_AppSettings.model_fields["download_folder"].default = str(_dl_dir)
_AppSettings.model_fields["aria2_builtin_log_file"].default     = str(_data_dir / "aria2" / "aria2.log")
_AppSettings.model_fields["aria2_builtin_session_file"].default = str(_data_dir / "aria2" / "aria2.session")

# ── Patch version.py: look for VERSION file next to the EXE ─────────────────
import core.version as _ver_mod
_exe_dir = pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) else _base
_ver_mod._VERSION_CANDIDATES = [  # type: ignore[attr-defined]
    _exe_dir / "VERSION",
    _base / "VERSION",
    pathlib.Path(__file__).parent.parent / "VERSION",
]

# ── Aria2: warn if aria2c is not on PATH ─────────────────────────────────────
import shutil as _shutil
if not _shutil.which("aria2c"):
    print(
        "[AllDebrid-Client] WARNING: aria2c not found on PATH.\n"
        "  Built-in aria2 download mode will not work.\n"
        "  Download aria2 from https://github.com/aria2/aria2/releases and add\n"
        "  it to your PATH, or set 'aria2_mode' to 'external' in settings.\n",
        file=sys.stderr,
    )

# ── Launch the app via uvicorn ───────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")

    print(f"[AllDebrid-Client] Starting on http://{host}:{port}")
    print(f"[AllDebrid-Client] Config : {os.environ['CONFIG_PATH']}")
    print(f"[AllDebrid-Client] Data   : {_data_dir}")
    print(f"[AllDebrid-Client] Open your browser and navigate to http://localhost:{port}")

    import uvicorn
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        workers=1,
        log_level="info",
    )

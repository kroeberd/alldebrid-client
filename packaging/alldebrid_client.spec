# -*- mode: python ; coding: utf-8 -*-
#
# alldebrid_client.spec — PyInstaller build specification for AllDebrid-Client Windows EXE
#
# Build command (run from repo root):
#   pip install pyinstaller
#   pyinstaller packaging/alldebrid_client.spec
#
# Output: dist/alldebrid-client-windows.exe
#
# This spec file is designed to be executed by GitHub Actions.
# Do NOT run it locally and commit the resulting EXE.

import sys
import os
from pathlib import Path

# Resolve paths relative to the spec file location (repo root/packaging/)
SPEC_DIR    = Path(SPECPATH)          # noqa: F821  (injected by PyInstaller)
REPO_ROOT   = SPEC_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
VERSION_FILE = REPO_ROOT / "VERSION"
CHANGELOG_FILE = REPO_ROOT / "CHANGELOG.md"

# ── Analysis ────────────────────────────────────────────────────────────────
a = Analysis(
    [str(BACKEND_DIR / "windows_main.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=[],
    datas=[
        # Frontend static files served by FastAPI
        (str(FRONTEND_DIR / "static"), "frontend/static"),
        # Version and changelog (read at runtime)
        (str(VERSION_FILE),    "."),
        (str(CHANGELOG_FILE),  "."),
        # All backend Python packages
        (str(BACKEND_DIR / "api"),      "api"),
        (str(BACKEND_DIR / "core"),     "core"),
        (str(BACKEND_DIR / "db"),       "db"),
        (str(BACKEND_DIR / "services"), "services"),
    ],
    hiddenimports=[
        # FastAPI / Starlette internals not auto-discovered
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "starlette.routing",
        "starlette.staticfiles",
        "starlette.middleware.cors",
        "fastapi",
        "fastapi.staticfiles",
        "fastapi.middleware.cors",
        # Pydantic v2 validators
        "pydantic",
        "pydantic.v1",
        "pydantic_settings",
        "pydantic_core",
        # Async I/O
        "aiohttp",
        "aiofiles",
        "aiosqlite",
        # Encoding
        "bencodepy",
        "python_multipart",
        "Crypto",
        "Crypto.Cipher",
        "Crypto.Hash",
        "Crypto.Random",
        # Application modules
        "main",
        "api.routes",
        "core.config",
        "core.scheduler",
        "core.version",
        "db.database",
        "db.migration",
        "services.alldebrid",
        "services.aria2",
        "services.aria2_runtime",
        "services.backup",
        "services.db_maintenance",
        "services.flexget",
        "services.integrations",
        "services.jackett",
        "services.manager_v2",
        "services.notifications",
        "services.page_cache",
        "services.stats",
        # asyncpg is optional (PostgreSQL) — include if present, skip gracefully if not
        "asyncpg",
        # h11, httptools for uvicorn
        "h11",
        "httptools",
        "websockets",
        "watchfiles",
    ],
    hookspath=[str(SPEC_DIR / "pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude test infrastructure from the bundle
        "pytest",
        "pytest_asyncio",
        "pytest_cov",
        "_pytest",
        # Exclude large unused packages
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "IPython",
        "notebook",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)

# ── PYZ (pure-Python archive) ────────────────────────────────────────────────
pyz = PYZ(a.pure)  # noqa: F821

# ── EXE (one-file bundle) ────────────────────────────────────────────────────
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="alldebrid-client-windows",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,        # compress with UPX if available (reduces file size ~30%)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,    # keep console so users can see log output + startup URL
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,       # add an .ico file path here to set a custom icon
    onefile=True,
)

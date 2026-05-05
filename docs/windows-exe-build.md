# Windows EXE Build — AllDebrid-Client

AllDebrid-Client runs natively on Windows as a single self-contained `.exe` file
produced by [PyInstaller](https://pyinstaller.org/). No Python installation is
required on the target machine.

---

## Table of Contents

- [How to trigger the build](#how-to-trigger-the-build)
- [Where to download the EXE](#where-to-download-the-exe)
- [First-run setup](#first-run-setup)
- [Configuration](#configuration)
- [Limitations vs. Docker](#limitations-vs-docker)
- [Troubleshooting](#troubleshooting)
- [Build internals](#build-internals)

---

## How to trigger the build

### Automatic (on push)

Every push to the `test-exe-build` branch triggers the workflow automatically:

```
.github/workflows/build-windows-exe.yml
```

### Manual

1. Go to **GitHub → Actions → Build Windows EXE**
2. Click **Run workflow**
3. Select the branch (`test-exe-build` or `main`)
4. Click **Run workflow** (green button)

The build takes approximately **3–5 minutes** on `windows-latest`.

---

## Where to download the EXE

1. Open **GitHub → Actions → Build Windows EXE**
2. Click the most recent successful workflow run
3. Scroll to the **Artifacts** section at the bottom
4. Click `alldebrid-client-windows-vX.Y.Z` to download a ZIP archive
5. Extract the ZIP — it contains `alldebrid-client-windows.exe`

> **Note:** GitHub Actions artifacts are retained for **30 days** by default.
> Download the EXE promptly after the build if you need to keep it longer.

---

## First-run setup

1. Place `alldebrid-client-windows.exe` anywhere on your PC (e.g. `C:\Tools\AllDebrid\`)
2. Double-click the EXE (or run it from a terminal)
3. The console window shows the startup URL:
   ```
   [AllDebrid-Client] Starting on http://0.0.0.0:8080
   [AllDebrid-Client] Open your browser and navigate to http://localhost:8080
   ```
4. Open **http://localhost:8080** in your browser
5. Go to **Settings → General** and enter your AllDebrid API key

Data is stored in:

```
%APPDATA%\AllDebrid-Client\
├── config\config.json      ← settings
├── data\
│   ├── watch\              ← watch folder (drop .torrent files here)
│   ├── processed\          ← processed watch-folder files
│   └── aria2\              ← aria2 logs & session
└── downloads\              ← default download destination
```

---

## Configuration

All settings can be changed via the web UI at **http://localhost:8080/settings**.
They are persisted in `%APPDATA%\AllDebrid-Client\config\config.json`.

### Environment variables

The EXE honours the following environment variables (set before launching):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | HTTP port the server listens on |
| `HOST` | `0.0.0.0` | Bind address (`127.0.0.1` for local-only) |
| `CONFIG_PATH` | `%APPDATA%\AllDebrid-Client\config\config.json` | Full path to config file |
| `DB_PATH` | `%APPDATA%\AllDebrid-Client\data\alldebrid.db` | SQLite database path |
| `WATCH_FOLDER` | `%APPDATA%\AllDebrid-Client\data\watch` | Folder to watch for .torrent files |
| `DOWNLOAD_FOLDER` | `%APPDATA%\AllDebrid-Client\downloads` | Default download destination |

Example — run on port 9090 with a custom config:

```cmd
set PORT=9090
set CONFIG_PATH=D:\MyConfig\alldebrid.json
alldebrid-client-windows.exe
```

### Using aria2 for downloads (optional)

The EXE bundles the AllDebrid-Client backend but **does not bundle `aria2c`**
(the download tool). Without aria2 the client still works — AllDebrid provides
the download links and you can download them manually or use an external client.

To enable the built-in aria2 download mode:

1. Download **aria2** from https://github.com/aria2/aria2/releases
2. Extract `aria2c.exe` and add its folder to your Windows `PATH`
3. Restart AllDebrid-Client
4. Go to **Settings → Download → aria2 Mode** and select **Built-in**

---

## Limitations vs. Docker

| Feature | Docker | Windows EXE |
|---------|--------|-------------|
| aria2 bundled | ✅ installed in image | ⚠️ must be installed separately |
| PostgreSQL | ✅ full support | ✅ full support (asyncpg included) |
| PUID/PGID (file ownership) | ✅ via gosu | ❌ not applicable on Windows |
| Auto-update | ✅ pull new image | ❌ download new EXE manually |
| Watch folder | ✅ | ✅ |
| Sonarr / Radarr integration | ✅ | ✅ (must be reachable by hostname/IP) |
| Jackett integration | ✅ | ✅ |
| FlexGet | ✅ | ⚠️ FlexGet must be installed separately |
| Unraid / NAS support | ✅ Community Apps | ❌ |
| Multi-arch (ARM) | ✅ | ❌ x64 only |
| File size (image vs EXE) | ~200 MB image | ~80 MB EXE |

### PostgreSQL on Windows

The EXE includes the `asyncpg` driver. Set the following in Settings → General:

- **Database type**: PostgreSQL
- **Host / Port / DB / User / Password**: your PG server credentials

---

## Troubleshooting

### "Windows Defender / antivirus blocked the EXE"

PyInstaller-generated executables are sometimes flagged by antivirus software
because they use a self-extracting archive technique. The EXE is built directly
from source in a public GitHub Actions workflow — you can verify the build log
to confirm no third-party binary was injected.

**Solution:** Add an exclusion for the EXE in your antivirus settings, or build
the EXE yourself by running the workflow from your own fork.

### "aria2c not found" warning on startup

See [Using aria2 for downloads](#using-aria2-for-downloads-optional) above.
The application works without aria2 — downloads can be triggered via AllDebrid's
own download links in the torrents list.

### Port 8080 already in use

```cmd
set PORT=8090
alldebrid-client-windows.exe
```

### Config not found / reset after update

Config is stored in `%APPDATA%\AllDebrid-Client\config\config.json`.
The EXE never deletes this file — updating to a new EXE preserves your config.

### Application crashes on startup

Run from a terminal (CMD or PowerShell) to see the full error output:

```cmd
cd C:\Path\To\Exe
alldebrid-client-windows.exe
```

---

## Build internals

| File | Purpose |
|------|---------|
| `packaging/alldebrid_client.spec` | PyInstaller spec — controls what goes into the EXE |
| `packaging/requirements-windows.txt` | Python deps for the Windows build (no test packages) |
| `packaging/pyinstaller_hooks/` | Custom PyInstaller hooks for aiosqlite, pydantic-settings |
| `backend/windows_main.py` | Windows entry point — patches Linux paths before app import |
| `.github/workflows/build-windows-exe.yml` | GitHub Actions workflow |

### Why PyInstaller and not pkg/nexe?

AllDebrid-Client is a **Python** application (FastAPI + uvicorn + aiosqlite).
PyInstaller is the industry-standard tool for packaging Python apps into a
self-contained executable. It correctly handles:

- C extensions (asyncpg, pycryptodome, aiosqlite)
- Data files (frontend HTML/JS/CSS, VERSION, CHANGELOG)
- Async runtime (uvicorn, aiohttp)

### Why not a one-folder bundle?

The `--onefile` flag is used so users receive a single `.exe` rather than a
folder with hundreds of files. PyInstaller extracts the bundle to a temp
directory on first run (`%TEMP%\_MEIxxxxxx`) — this is normal behaviour.

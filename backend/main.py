import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from core.scheduler import start_scheduler, stop_scheduler
from db.database import init_db, _is_postgres, DB_PATH
from services.manager_v2 import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("alldebrid-client")

# Wie oft und wie lange auf PostgreSQL warten (wichtig beim Docker-Start)
_PG_CONNECT_RETRIES = 15
_PG_CONNECT_DELAY = 2.0   # Sekunden


async def _wait_for_postgres():
    """
    Wartet bis PostgreSQL erreichbar ist.
    Notwendig weil der PG-Container beim gemeinsamen Start langsamer hochfährt
    als die Python-App. Gibt nach _PG_CONNECT_RETRIES Versuchen auf.
    """
    from db.database import _build_dsn
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise RuntimeError("asyncpg nicht installiert — pip install asyncpg")

    dsn = _build_dsn()
    for attempt in range(1, _PG_CONNECT_RETRIES + 1):
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            logger.info("PostgreSQL bereit (Versuch %d/%d)", attempt, _PG_CONNECT_RETRIES)
            return
        except Exception as exc:
            if attempt >= _PG_CONNECT_RETRIES:
                raise RuntimeError(
                    f"PostgreSQL nach {_PG_CONNECT_RETRIES} Versuchen nicht erreichbar: {exc}"
                )
            logger.warning(
                "Warte auf PostgreSQL (Versuch %d/%d): %s",
                attempt, _PG_CONNECT_RETRIES, exc,
            )
            await asyncio.sleep(_PG_CONNECT_DELAY)


async def _reset_stuck_downloads_sqlite():
    """
    SQLite: Torrents im Status 'downloading' ohne download_files-Einträge
    zurücksetzen (App-Absturz während _download()).
    """
    import aiosqlite as _aiosqlite
    async with _aiosqlite.connect(DB_PATH) as _db:
        _db.row_factory = _aiosqlite.Row
        stuck = await (await _db.execute(
            """SELECT id, alldebrid_id, name FROM torrents
               WHERE status='downloading'
                 AND id NOT IN (SELECT DISTINCT torrent_id FROM download_files)"""
        )).fetchall()
        for row in stuck:
            await _db.execute(
                "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
            await _db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'warn', ?)",
                (row["id"], "Recovered stuck download on startup — re-queuing"),
            )
            logger.info("Startup: reset stuck torrent %s (%s)", row["id"], row["name"])
        await _db.commit()
    return list(stuck)


async def _reset_stuck_downloads_postgres():
    """
    PostgreSQL: Gleiche Logik wie SQLite-Variante, aber via asyncpg.
    """
    try:
        import asyncpg  # type: ignore
    except ImportError:
        return []
    from db.database import _build_dsn
    conn = await asyncpg.connect(_build_dsn())
    try:
        stuck = await conn.fetch(
            """SELECT id, alldebrid_id, name FROM torrents
               WHERE status='downloading'
                 AND id NOT IN (SELECT DISTINCT torrent_id FROM download_files)"""
        )
        for row in stuck:
            await conn.execute(
                "UPDATE torrents SET status='ready', updated_at=NOW() WHERE id=$1",
                row["id"],
            )
            await conn.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES ($1, 'warn', $2)",
                row["id"], "Recovered stuck download on startup — re-queuing",
            )
            logger.info("Startup: reset stuck torrent %s (%s)", row["id"], row["name"])
    finally:
        await conn.close()
    return list(stuck)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AllDebrid-Client...")

    # 1. Auf Datenbank warten (PostgreSQL kann beim Docker-Start langsam sein)
    if _is_postgres():
        try:
            await _wait_for_postgres()
        except Exception as e:
            logger.error("Datenbankverbindung fehlgeschlagen: %s", e)
            raise

    # 2. Schema initialisieren (idempotent — sicher bei Neustart)
    await init_db()

    # 3. Stuck-Downloads zurücksetzen
    try:
        if _is_postgres():
            stuck = await _reset_stuck_downloads_postgres()
        else:
            stuck = await _reset_stuck_downloads_sqlite()
        for row in stuck:
            if row["alldebrid_id"]:
                asyncio.create_task(
                    manager._start_download(row["id"], str(row["alldebrid_id"]), str(row["name"] or ""))
                )
    except Exception as e:
        logger.warning("Startup stuck-download cleanup failed: %s", e)

    # 4. Bestehende Magnete von AllDebrid importieren
    try:
        await manager.import_existing_magnets()
    except Exception as e:
        logger.warning("Initial AllDebrid import skipped: %s", e)

    # 5. aria2-Zustand beim Start abgleichen
    try:
        await manager.reconcile_aria2_on_startup()
    except Exception as e:
        logger.warning("Startup aria2 reconciliation failed: %s", e)

    await start_scheduler()
    yield
    logger.info("Shutting down AllDebrid-Client...")
    await stop_scheduler()


app = FastAPI(
    title="AllDebrid-Client",
    description="Automated torrent downloading via AllDebrid",
    version="0.6.3",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

# ── Static files ──────────────────────────────────────────────────────────────
_here = Path(__file__).parent
_candidates = []

_env = os.getenv("STATIC_DIR", "").strip()
if _env:
    _candidates.append(Path(_env))

_candidates.append(_here.parent / "frontend" / "static")
_candidates.append(Path("/app/frontend/static"))
_candidates.append(Path("/app/static"))


def _is_valid(p: Path) -> bool:
    return p.is_dir() and (p / "index.html").exists()


_static = next((p for p in _candidates if _is_valid(p)), None)

if _static is None:
    tried = ", ".join(str(p) for p in _candidates)
    raise RuntimeError(
        f"Frontend index.html not found. Tried: [{tried}]. "
        "Fix your Docker build (COPY frontend/ /app/frontend/) "
        "or set the STATIC_DIR environment variable."
    )

logger.info("Serving static files from: %s", _static)
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")

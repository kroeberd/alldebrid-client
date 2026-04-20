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
from core.version import read_version
from db.database import init_db, _is_postgres, DB_PATH
from services.manager_v2 import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("alldebrid-client")

# PostgreSQL connection attempts on startup
_PG_CONNECT_RETRIES = 15
_PG_CONNECT_DELAY   = 10.0  # seconds between attempts (15 × 10s = 150s max wait)


async def _sync_sqlite_to_pg_on_startup() -> int:
    """
    At startup with PostgreSQL active: checks each table for rows that exist
    in SQLite but are missing in PostgreSQL (identified by primary key / hash).
    Copies missing rows without touching existing PG data.
    Returns total number of rows copied.
    """
    import aiosqlite
    from db.database import DB_PATH, _build_dsn
    try:
        import asyncpg
    except ImportError:
        return 0

    if not DB_PATH.exists():
        return 0

    dsn  = _build_dsn()
    try:
        pg = await asyncpg.connect(dsn, timeout=10)
    except Exception as exc:
        logger.warning("Startup sync: cannot connect to PostgreSQL: %s", exc)
        return 0

    total_synced = 0
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30) as sl:
            sl.row_factory = aiosqlite.Row

            # ── torrents ────────────────────────────────────────────────────
            sl_rows = await (await sl.execute("SELECT * FROM torrents")).fetchall()
            for row in sl_rows:
                exists = await pg.fetchval(
                    "SELECT 1 FROM torrents WHERE hash=$1", row["hash"]
                )
                if not exists:
                    try:
                        await pg.execute(
                            """INSERT INTO torrents
                               (hash, name, magnet, status, alldebrid_id, size_bytes,
                                progress, download_url, local_path, source,
                                provider_status, provider_status_code, polling_failures,
                                download_client, label, priority, error_message,
                                created_at, updated_at, completed_at)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                               ON CONFLICT (hash) DO NOTHING""",
                            row["hash"], row["name"], row["magnet"], row["status"],
                            row["alldebrid_id"], row["size_bytes"], row["progress"],
                            row["download_url"], row["local_path"], row["source"],
                            row["provider_status"], row["provider_status_code"],
                            row["polling_failures"], row["download_client"],
                            row["label"], row["priority"], row["error_message"],
                            row["created_at"], row["updated_at"], row["completed_at"],
                        )
                        total_synced += 1
                        logger.debug("Startup sync: copied torrent hash=%s", row["hash"])
                    except Exception as exc:
                        logger.debug("Startup sync: skip torrent %s: %s", row["hash"], exc)

            # ── events ──────────────────────────────────────────────────────
            # Events are append-only — copy any not yet in PG
            pg_event_count = await pg.fetchval("SELECT COUNT(*) FROM events")
            sl_events = await (await sl.execute("SELECT * FROM events ORDER BY id")).fetchall()
            if pg_event_count < len(sl_events):
                for ev in sl_events[pg_event_count:]:
                    try:
                        # Get PG torrent_id from hash
                        if ev["torrent_id"]:
                            sl_t = await (await sl.execute(
                                "SELECT hash FROM torrents WHERE id=?", (ev["torrent_id"],)
                            )).fetchone()
                            pg_tid = await pg.fetchval(
                                "SELECT id FROM torrents WHERE hash=$1",
                                sl_t["hash"] if sl_t else None
                            ) if sl_t else None
                        else:
                            pg_tid = None
                        await pg.execute(
                            "INSERT INTO events (torrent_id, level, message, created_at) "
                            "VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                            pg_tid, ev["level"], ev["message"], ev["created_at"],
                        )
                        total_synced += 1
                    except Exception as exc:
                        logger.debug("Startup sync: skip event %s: %s", ev["id"], exc)

    except Exception as exc:
        logger.warning("Startup sync error: %s", exc)
    finally:
        await pg.close()

    return total_synced


async def _wait_for_postgres() -> bool:
    """
    Waits until PostgreSQL is reachable.
    Returns True if connected, False after all retries are exhausted.
    Never raises — the caller decides whether to fall back to SQLite.
    """
    from db.database import _build_dsn
    try:
        import asyncpg  # type: ignore
    except ImportError:
        logger.error("asyncpg not installed — cannot connect to PostgreSQL")
        return False

    dsn = _build_dsn()
    for attempt in range(1, _PG_CONNECT_RETRIES + 1):
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            await conn.close()
            logger.info("PostgreSQL ready (attempt %d/%d)", attempt, _PG_CONNECT_RETRIES)
            return True
        except Exception as exc:
            remaining = _PG_CONNECT_RETRIES - attempt
            if remaining == 0:
                logger.warning(
                    "PostgreSQL not reachable after %d attempts: %s",
                    _PG_CONNECT_RETRIES, exc,
                )
                return False
            logger.warning(
                "Waiting for PostgreSQL (attempt %d/%d, %d remaining): %s",
                attempt, _PG_CONNECT_RETRIES, remaining, exc,
            )
            await asyncio.sleep(_PG_CONNECT_DELAY)
    return False


def _fallback_to_sqlite():
    """
    Switches the application to SQLite when PostgreSQL is not reachable.
    Updates the live settings in-place and logs a clear warning.
    """
    from core.config import get_settings, apply_settings
    cfg = get_settings()
    logger.warning(
        "⚠️  PostgreSQL unreachable — falling back to SQLite. "
        "Data will be stored in %s. "
        "Restart with a reachable PostgreSQL instance to use PG.",
        DB_PATH,
    )
    # Switch active settings to SQLite without touching config.json
    new_cfg = cfg.model_copy(update={"db_type": "sqlite"})
    apply_settings(new_cfg)


async def _reset_stuck_downloads_sqlite():
    """Resets torrents that were stuck in 'downloading' state when the app last stopped."""
    import aiosqlite as _aiosqlite
    async with _aiosqlite.connect(DB_PATH, timeout=30) as _db:
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


async def _startup_sync_sqlite_to_postgres() -> dict:
    """
    Startup consistency check: ensures all data from SQLite is present in PostgreSQL.

    Compares row counts per table. If SQLite has more rows than PostgreSQL
    (e.g. after a PG outage where writes went to SQLite fallback, or first-time
    switch to PG), the missing rows are migrated automatically.

    Only runs when PostgreSQL is the active backend.
    Returns a dict with per-table results.
    """
    from db.database import _build_dsn, DB_PATH
    from db.migration import migrate_sqlite_to_postgres, MIGRATION_TABLES
    import aiosqlite as _aiosqlite

    if not DB_PATH.exists():
        logger.info("Startup sync: no SQLite file found, skipping")
        return {}

    results = {}
    needs_migration = False

    try:
        import asyncpg
        dsn = _build_dsn()
        pg_conn = await asyncpg.connect(dsn, timeout=10)
        try:
            for table in MIGRATION_TABLES:
                try:
                    pg_row  = await pg_conn.fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}")
                    pg_cnt  = int(pg_row["cnt"] or 0)
                except Exception:
                    pg_cnt = 0

                async with _aiosqlite.connect(DB_PATH, timeout=10) as sl:
                    cur = await sl.execute(f"SELECT COUNT(*) FROM {table}")
                    row = await cur.fetchone()
                    sl_cnt = int(row[0] or 0) if row else 0

                results[table] = {"sqlite": sl_cnt, "postgres": pg_cnt}
                if sl_cnt > pg_cnt:
                    logger.warning(
                        "Startup sync: table '%s' — SQLite has %d rows, PostgreSQL has %d → migration needed",
                        table, sl_cnt, pg_cnt,
                    )
                    needs_migration = True
                else:
                    logger.info(
                        "Startup sync: table '%s' OK (SQLite %d, PostgreSQL %d)",
                        table, sl_cnt, pg_cnt,
                    )
        finally:
            await pg_conn.close()
    except Exception as exc:
        logger.warning("Startup sync: count check failed: %s", exc)
        return {}

    if not needs_migration:
        logger.info("Startup sync: all tables consistent — no migration needed")
        return results

    logger.info("Startup sync: migrating missing data from SQLite to PostgreSQL (force=True)...")
    try:
        dsn = _build_dsn()
        result = await migrate_sqlite_to_postgres(DB_PATH, dsn, force=True, dry_run=False)
        if result.success:
            logger.info("Startup sync migration complete: %s", result.summary())
        else:
            logger.error("Startup sync migration failed: %s", result.error)
    except Exception as exc:
        logger.error("Startup sync migration error: %s", exc)

    return results


async def _reset_stuck_downloads_postgres():
    """Same logic as the SQLite variant, using asyncpg."""
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

    # 0. Validate and sanitise config — fix obvious misconfigurations before anything else
    try:
        from core.config import get_settings, apply_settings, save_settings
        from core.config_validator import validate_and_sanitise
        _raw_cfg = get_settings()
        _clean_cfg = validate_and_sanitise(_raw_cfg)
        if _clean_cfg is not _raw_cfg:
            # Corrections were made — persist them so the user sees clean values
            save_settings(_clean_cfg)
            apply_settings(_clean_cfg)
            logger.info("Config sanitised and saved — check warnings above for details")
    except Exception as _ve:
        logger.warning("Config validation skipped due to error: %s", _ve)

    # 1. PostgreSQL: wait for readiness, fall back to SQLite if needed
    if _is_postgres():
        pg_ok = await _wait_for_postgres()
        if not pg_ok:
            _fallback_to_sqlite()
            # _is_postgres() now returns False

    # 2. Initialise schema (idempotent — safe on restart)
    # Note: init_db() always runs _init_db_sqlite() even in PG mode,
    # for WAL setup and backward compatibility with SQLite fallback operations.
    try:
        await init_db()
        logger.info("Database schema initialised (SQLite + %s)",
                    "PostgreSQL" if _is_postgres() else "SQLite only")
    except Exception as e:
        logger.error("Database initialisation failed: %s", e)
        if _is_postgres():
            # Last resort: fall back to SQLite
            _fallback_to_sqlite()
            await init_db()

    # 3. If PostgreSQL is active: sync missing rows from SQLite → PG on startup
    #    Ensures no data loss when switching backends or after a migration.
    if _is_postgres():
        try:
            synced = await _sync_sqlite_to_pg_on_startup()
            if synced:
                logger.info("Startup sync: %d row(s) copied from SQLite to PostgreSQL", synced)
        except Exception as exc:
            logger.warning("Startup SQLite→PG sync failed (non-fatal): %s", exc)

    # 3. Startup sync: ensure SQLite data is present in PostgreSQL
    if _is_postgres():
        try:
            sync_results = await _startup_sync_sqlite_to_postgres()
            if sync_results:
                for table, counts in sync_results.items():
                    if counts["sqlite"] > counts["postgres"]:
                        logger.warning(
                            "Startup sync result: %s — SQLite %d > PostgreSQL %d (migrated)",
                            table, counts["sqlite"], counts["postgres"],
                        )
        except Exception as e:
            logger.warning("Startup sync failed (non-fatal): %s", e)

    # 4. Reset stuck downloads
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

    # 5. Import existing AllDebrid magnets
    try:
        await manager.import_existing_magnets()
    except Exception as e:
        logger.warning("Initial AllDebrid import skipped: %s", e)

    # 6. Reconcile aria2 state on startup
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
    version=read_version(),
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
        "Fix your Docker build or set STATIC_DIR."
    )

logger.info("Serving static files from: %s", _static)
app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")

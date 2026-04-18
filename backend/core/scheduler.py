import asyncio
import logging
from core.config import get_settings
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.scheduler")
_tasks = []


async def watch_folder_loop():
    while True:
        try:
            await manager.scan_watch_folder()
        except Exception as e:
            logger.error(f"Watch folder error: {e}")
        await asyncio.sleep(get_settings().watch_interval_seconds)


async def sync_status_loop():
    """
    Regular AllDebrid poll: syncs active (non-terminal) torrents every poll_interval_seconds.
    Also runs cleanup tasks each cycle.
    """
    while True:
        try:
            await manager.sync_alldebrid_status()
        except Exception as e:
            logger.error(f"Status sync error: {e}")
        try:
            await manager.cleanup_no_peer_errors()
        except Exception as e:
            logger.error(f"No-peer cleanup error: {e}")
        try:
            await manager.cleanup_stuck_downloads()
        except Exception as e:
            logger.error(f"Stuck download cleanup error: {e}")
        await asyncio.sleep(get_settings().poll_interval_seconds)


async def full_sync_loop():
    """
    Full AllDebrid reconciliation: runs every full_sync_interval_minutes (default 5).
    Catches torrents in 'error'/'queued' that are actually 'ready' on AllDebrid,
    and any status drift between local DB and AllDebrid.
    Also imports new magnets added directly on AllDebrid.
    """
    await asyncio.sleep(10)  # short initial delay after startup
    while True:
        cfg = get_settings()
        interval = max(1, int(getattr(cfg, "full_sync_interval_minutes", 5) or 5))
        try:
            await manager.import_existing_magnets()
        except Exception as e:
            logger.debug(f"Existing magnet import skipped: {e}")
        try:
            await manager.full_alldebrid_sync()
        except Exception as e:
            logger.error(f"Full sync error: {e}")
        await asyncio.sleep(interval * 60)


async def sync_download_clients_loop():
    while True:
        try:
            await manager.sync_download_clients()
        except Exception as e:
            logger.error(f"Download client sync error: {e}")
        await asyncio.sleep(max(2, get_settings().aria2_poll_interval_seconds))


async def deep_sync_loop():
    """
    Periodically runs a filesystem-based deep sync to catch aria2 downloads
    that have completed on disk but whose GID/status is stale or missing.
    Interval configured via aria2_deep_sync_interval_minutes (default 10).
    0 = disabled.
    """
    while True:
        cfg = get_settings()
        interval_min = max(0, int(getattr(cfg, "aria2_deep_sync_interval_minutes", 10) or 0))
        if interval_min <= 0:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            await manager.deep_sync_aria2_finished()
        except Exception as e:
            logger.error(f"Deep aria2 sync error: {e}")


async def backup_loop():
    """Runs periodic backups based on backup_interval_hours setting."""
    await asyncio.sleep(60)  # Initial delay
    while True:
        try:
            from services.backup import run_backup
            await run_backup()
        except Exception as e:
            logger.error(f"Backup error: {e}")
        cfg = get_settings()
        interval_h = max(1, getattr(cfg, "backup_interval_hours", 24))
        await asyncio.sleep(interval_h * 3600)


async def start_scheduler():
    _tasks.append(asyncio.create_task(watch_folder_loop()))
    _tasks.append(asyncio.create_task(sync_status_loop()))
    _tasks.append(asyncio.create_task(full_sync_loop()))
    _tasks.append(asyncio.create_task(sync_download_clients_loop()))
    _tasks.append(asyncio.create_task(deep_sync_loop()))
    _tasks.append(asyncio.create_task(backup_loop()))
    _tasks.append(asyncio.create_task(flexget_loop()))
    _tasks.append(asyncio.create_task(stats_snapshot_loop()))
    logger.info("Scheduler started")


async def stop_scheduler():
    for t in _tasks:
        t.cancel()
    _tasks.clear()


async def flexget_loop():
    """
    Runs FlexGet tasks on a configurable interval with jitter.
    Jitter prevents multiple instances from hammering FlexGet simultaneously.
    Interval: flexget_schedule_minutes (0 = disabled).
    Jitter:   ±10% of interval, max 60s.
    """
    import random
    await asyncio.sleep(30)  # initial delay
    while True:
        cfg = get_settings()
        interval_min = max(0, int(getattr(cfg, "flexget_schedule_minutes", 0) or 0))
        if interval_min <= 0:
            await asyncio.sleep(60)
            continue
        if not getattr(cfg, "flexget_enabled", False):
            await asyncio.sleep(60)
            continue

        interval_sec = interval_min * 60
        jitter       = min(interval_sec * 0.1, 60)  # ±10%, max 60s
        sleep_sec    = interval_sec + random.uniform(-jitter, jitter)
        await asyncio.sleep(max(10, sleep_sec))

        try:
            from services.flexget import run_flexget_tasks
            await run_flexget_tasks(triggered_by="schedule")
        except Exception as e:
            logger.error(f"FlexGet scheduled run error: {e}")


async def stats_snapshot_loop():
    """Periodically takes a stats snapshot."""
    await asyncio.sleep(120)  # initial delay
    while True:
        cfg = get_settings()
        interval_min = max(0, int(getattr(cfg, "stats_snapshot_interval_minutes", 60) or 60))
        if interval_min <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            from services.stats import take_stats_snapshot
            await take_stats_snapshot()
        except Exception as e:
            logger.error(f"Stats snapshot error: {e}")

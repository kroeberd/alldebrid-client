import asyncio
import logging
from core.config import get_settings
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.scheduler")
_tasks = []


def _coerce_int_setting(value, default: int) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _has_reporting_webhook(cfg) -> bool:
    """Return True when reporting can send to either the dedicated or Discord webhook."""
    stats_webhook = (getattr(cfg, "stats_report_webhook_url", "") or "").strip()
    discord_webhook = (getattr(cfg, "discord_webhook_url", "") or "").strip()
    return bool(stats_webhook or discord_webhook)


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
        interval = max(0, _coerce_int_setting(getattr(cfg, "full_sync_interval_minutes", 5), 5))
        if interval <= 0:
            await asyncio.sleep(60)
            continue
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
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "aria2_deep_sync_interval_minutes", 10), 10))
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
    _tasks.append(asyncio.create_task(stats_report_loop()))
    logger.info("Scheduler started")


async def stop_scheduler():
    for t in _tasks:
        t.cancel()
    _tasks.clear()


async def flexget_loop():
    """
    Runs scheduled FlexGet tasks individually with per-task intervals and jitter.
    """
    from services.flexget import get_task_schedules, next_delay_seconds, run_flexget_tasks, schedule_signature

    next_runs: dict[str, float] = {}
    last_signature: tuple | None = None
    await asyncio.sleep(30)  # initial delay
    while True:
        cfg = get_settings()
        if not getattr(cfg, "flexget_enabled", False):
            next_runs.clear()
            last_signature = None
            await asyncio.sleep(60)
            continue

        schedules = [
            s for s in get_task_schedules()
            if bool(s.get("enabled", True)) and int(s.get("interval_minutes", 0) or 0) > 0
        ]
        if not schedules:
            next_runs.clear()
            last_signature = None
            await asyncio.sleep(60)
            continue

        signature = schedule_signature(schedules)
        now = asyncio.get_running_loop().time()
        if signature != last_signature:
            valid_tasks = {str(s["task"]) for s in schedules}
            next_runs = {task: due_at for task, due_at in next_runs.items() if task in valid_tasks}
            for schedule in schedules:
                task_name = str(schedule["task"])
                if task_name not in next_runs:
                    next_runs[task_name] = now + next_delay_seconds(schedule)
            last_signature = signature

        due_schedules = [s for s in schedules if now >= next_runs.get(str(s["task"]), float("inf"))]
        if not due_schedules:
            await asyncio.sleep(15)
            continue

        for schedule in due_schedules:
            task_name = str(schedule["task"])
            try:
                await run_flexget_tasks(
                    tasks=None if task_name == "*" else [task_name],
                    triggered_by="schedule",
                )
            except Exception as e:
                logger.error(f"FlexGet scheduled run error ({task_name}): {e}")
            finally:
                next_runs[task_name] = asyncio.get_running_loop().time() + next_delay_seconds(schedule)

        await asyncio.sleep(5)


async def stats_snapshot_loop():
    """Periodically takes a stats snapshot."""
    await asyncio.sleep(120)  # initial delay
    while True:
        cfg = get_settings()
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "stats_snapshot_interval_minutes", 60), 60))
        if interval_min <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            from services.stats import take_stats_snapshot
            await take_stats_snapshot()
        except Exception as e:
            logger.error(f"Stats snapshot error: {e}")


async def stats_report_loop():
    """Periodically sends a reporting webhook for the configured time window."""
    await asyncio.sleep(180)
    while True:
        cfg = get_settings()
        interval_h = max(0, _coerce_int_setting(getattr(cfg, "stats_report_interval_hours", 0), 0))
        if interval_h <= 0 or not _has_reporting_webhook(cfg):
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(max(300, interval_h * 3600))
        try:
            from services.stats import send_stats_report
            await send_stats_report(hours=interval_h, triggered_by="schedule")
        except Exception as e:
            logger.error(f"Stats report error: {e}")

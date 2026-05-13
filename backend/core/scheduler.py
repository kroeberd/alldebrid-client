import asyncio
import random
import time
import logging
from core.config import get_settings
from core.logging_utils import sanitize_exception
from services.manager_v2 import manager

logger = logging.getLogger("alldebrid.scheduler")
_tasks = []


async def _jitter_sleep(base_seconds: float, jitter_fraction: float = 0.25) -> None:
    """Sleep for base_seconds ± jitter_fraction*base_seconds.

    Spreads startup spikes across the configured interval so all loops
    don't fire simultaneously on container start, reducing burst API load.
    Minimum sleep: 1 second.
    """
    jitter = base_seconds * jitter_fraction * (2 * random.random() - 1)
    await asyncio.sleep(max(1.0, base_seconds + jitter))


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


def _stats_report_window_hours(cfg) -> int:
    """Return the configured report window in hours for webhook reporting."""
    return max(1, _coerce_int_setting(getattr(cfg, "stats_report_window_hours", 24), 24))


async def watch_folder_loop():
    await _jitter_sleep(get_settings().watch_interval_seconds)
    while True:
        try:
            await manager.scan_watch_folder()
        except Exception as e:
            logger.error(f"Watch folder error: {e}")
        await asyncio.sleep(get_settings().watch_interval_seconds)


async def sync_status_loop():
    """
    Regular AllDebrid poll: syncs active (non-terminal) torrents every poll_interval_seconds.
    Also runs cleanup tasks each cycle and enforces Smart Scheduler night-mode limits.
    """
    await _jitter_sleep(get_settings().poll_interval_seconds)
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
        try:
            await _enforce_smart_scheduler()
        except Exception as e:
            logger.debug(f"Smart scheduler enforcement error: {e}")
        await asyncio.sleep(get_settings().poll_interval_seconds)


async def _enforce_smart_scheduler():
    """
    Apply Smart Scheduler night-mode limits when outside the configured day window.

    If `bandwidth_day_window` is set (e.g. "08:00-23:00") and the current local
    time is outside that window, push reduced limits to aria2:
      - max concurrent downloads = bandwidth_night_max_dl
      - max speed = bandwidth_night_speed_mbps * 1_000_000 / 8 bytes/s

    When inside the day window the normal aria2 settings are restored.
    Does nothing if `bandwidth_day_window` is empty.
    """
    cfg = get_settings()
    window = (getattr(cfg, "bandwidth_day_window", "") or "").strip()
    if not window:
        return

    import datetime as _dt
    now = _dt.datetime.now().time()

    def _parse_t(s: str) -> _dt.time:
        h, m = s.strip().split(":")
        return _dt.time(int(h), int(m))

    try:
        start_str, end_str = window.split("-")
        day_start = _parse_t(start_str)
        day_end   = _parse_t(end_str)
    except Exception:
        return  # invalid format — skip silently

    in_day = day_start <= now < day_end

    night_max_dl = max(0, int(getattr(cfg, "bandwidth_night_max_dl",   1) or 1))
    night_mbps   = float(getattr(cfg, "bandwidth_night_speed_mbps", 0.0) or 0.0)
    night_bps    = int(night_mbps * 1_000_000 / 8) if night_mbps > 0 else 0

    day_max_dl   = int(getattr(cfg, "aria2_max_active_downloads", 3) or 3)
    day_bps      = int(getattr(cfg, "aria2_max_download_limit",   0) or 0)

    try:
        target_max_dl = day_max_dl if in_day else night_max_dl
        target_bps    = day_bps    if in_day else night_bps

        options: dict = {}
        if target_max_dl >= 0:
            options["max-concurrent-downloads"] = str(target_max_dl)
        if target_bps >= 0:
            options["max-overall-download-limit"] = str(target_bps)

        if options:
            await manager.aria2().change_global_options(options)
            logger.debug(
                "smart_scheduler: %s mode — max_dl=%s bps=%s",
                "day" if in_day else "night", target_max_dl, target_bps,
            )
    except Exception as exc:
        logger.debug("smart_scheduler: aria2 apply failed: %s", exc)


async def full_sync_loop():
    """
    Full AllDebrid reconciliation: runs every full_sync_interval_minutes (default 5).
    Catches torrents in 'error'/'queued' that are actually 'ready' on AllDebrid,
    and any status drift between local DB and AllDebrid.
    Also imports new magnets added directly on AllDebrid.
    """
    cfg = get_settings()
    interval = max(1, _coerce_int_setting(getattr(cfg, "full_sync_interval_minutes", 5), 5))
    await _jitter_sleep(interval * 60)  # spread startup across the full interval
    while True:
        cfg = get_settings()
        interval = max(0, _coerce_int_setting(getattr(cfg, "full_sync_interval_minutes", 5), 5))
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        try:
            await manager.import_existing_magnets()
        except Exception as e:
            logger.error("Existing magnet import failed: %s", sanitize_exception(e))
        try:
            await manager.full_alldebrid_sync()
        except Exception as e:
            logger.error(f"Full sync error: {e}")
        await asyncio.sleep(interval * 60)


async def sync_download_clients_loop():
    await _jitter_sleep(max(2, get_settings().aria2_poll_interval_seconds))
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


async def aria2_housekeeping_loop():
    """Periodically purges aria2 stopped results and reapplies memory-relevant global options."""
    await asyncio.sleep(90)
    while True:
        cfg = get_settings()
        interval_min = max(0, _coerce_int_setting(getattr(cfg, "aria2_purge_interval_minutes", 60), 60))
        if interval_min <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval_min * 60)
        try:
            await manager.run_aria2_housekeeping()
        except Exception as e:
            logger.error(f"aria2 housekeeping error: {e}")


async def aria2_log_rotation_loop():
    """Rotate the built-in aria2 log file before it grows without bound."""
    from services.aria2_runtime import runtime, is_builtin_mode

    await asyncio.sleep(180)
    while True:
        try:
            cfg = get_settings()
            if is_builtin_mode(cfg):
                result = await runtime.ensure_log_rotation()
                if result.get("rotated"):
                    logger.info("aria2 log rotation completed")
        except Exception as e:
            logger.error("aria2 log rotation error: %s", e)
        await asyncio.sleep(900)



async def aria2_restart_loop():
    """
    Periodically restarts the built-in aria2 process to reclaim memory.

    aria2 uses glibc malloc. Even with MALLOC_ARENA_MAX=1 the process heap
    grows over time as malloc retains pages after freeing them. A full process
    restart is the only guaranteed way to return that memory to the OS.

    The restart is deferred until aria2 has no active downloads to avoid
    interrupting in-progress transfers. After restart, _dispatch re-queues
    all pending files from the DB within one poll cycle (≤1 second).

    Controlled by aria2_restart_interval_hours (0 = disabled).
    """
    from services.aria2_runtime import runtime, is_builtin_mode

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            cfg = get_settings()
            if not is_builtin_mode(cfg):
                continue
            interval_h = float(getattr(cfg, "aria2_restart_interval_hours", 0) or 0)
            if interval_h <= 0:
                continue
            uptime_s = runtime._started_at
            if uptime_s <= 0:
                continue
            age_h = (time.time() - uptime_s) / 3600
            if age_h < interval_h:
                continue

            # Wait until no active downloads to avoid interruption
            try:
                from services.aria2 import Aria2Service
                from services.aria2_runtime import effective_rpc_config
                url, secret = effective_rpc_config(cfg)
                svc = Aria2Service(url, secret, 10)
                all_dl = await svc.get_all()
                active = [d for d in all_dl if d.status == "active"]
                if active:
                    logger.debug(
                        "aria2 restart deferred: %d active downloads", len(active)
                    )
                    continue
            except Exception:
                continue

            logger.info(
                "aria2 periodic restart after %.1f hours (memory reclaim)", age_h
            )
            await runtime.restart()
            logger.info("aria2 restarted successfully")
        except Exception as e:
            logger.error("aria2_restart_loop error: %s", e)

async def update_check_loop() -> None:
    """Check GitHub for new releases every N hours and send a Discord webhook if enabled."""
    await asyncio.sleep(300)  # 5 min initial delay
    _last_notified: str = ""
    while True:
        try:
            cfg = get_settings()
            interval_h = max(0, _coerce_int_setting(
                getattr(cfg, "update_check_interval_hours", 12), 12
            ))
            if interval_h <= 0:
                await asyncio.sleep(3600)
                continue

            from core.version import read_version
            import aiohttp as _aiohttp

            current = read_version()
            timeout = _aiohttp.ClientTimeout(total=10)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://api.github.com/repos/kroeberd/alldebrid-client/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json"},
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"GitHub API returned {resp.status}")
                    rel = await resp.json()

            latest = (rel.get("tag_name") or "").lstrip("v")

            def _v(s: str):
                try:
                    return tuple(int(x) for x in s.split("."))
                except ValueError:
                    return (0, 0, 0)

            if latest and _v(latest) > _v(current) and latest != _last_notified:
                logger.info("Update available: %s → %s", current, latest)
                from services.notifications import notifier
                await notifier.send_update(
                    current_version=current,
                    latest_version=latest,
                    release_url=rel.get("html_url", ""),
                    release_notes=(rel.get("body") or "").strip(),
                )
                _last_notified = latest

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("update_check_loop error: %s", exc)

        await asyncio.sleep(max(3600, interval_h * 3600))


async def events_ttl_loop() -> None:
    """Prune old event log entries once per day.

    Only the ``events`` table is pruned — torrents and download_files are never
    touched, so duplicate-download prevention (based on the torrent hash and
    status columns) is not affected.
    """
    await asyncio.sleep(3600)  # 1-hour initial delay so startup isn't noisy
    while True:
        try:
            cfg = get_settings()
            keep_days = int(getattr(cfg, "events_keep_days", 30) or 30)
            if keep_days > 0:
                from services.db_maintenance import cleanup_old_events
                result = await cleanup_old_events(keep_days=keep_days)
                if result.get("deleted", 0) > 0:
                    logger.info(
                        "events_ttl_loop: pruned %d event(s) older than %d days",
                        result["deleted"], keep_days,
                    )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("events_ttl_loop error: %s", exc)
        await asyncio.sleep(86400)  # run once every 24 hours


async def saved_searches_loop():
    """Periodically run all enabled saved searches."""
    cfg = get_settings()
    interval = max(5, int(getattr(cfg, "saved_searches_interval_minutes", 60) or 60)) * 60
    await _jitter_sleep(interval)
    while True:
        try:
            cfg = get_settings()
            interval = max(5, int(getattr(cfg, "saved_searches_interval_minutes", 60) or 60)) * 60
            if interval > 0:
                from db.database import get_db
                from api.routes import _execute_saved_search
                async with get_db() as db:
                    searches = await db.fetchall(
                        "SELECT * FROM saved_searches WHERE enabled=1"
                    )
                for search in (searches or []):
                    try:
                        cfg_now = get_settings()
                        intvl = int(search.get("interval_minutes") or 60)
                        last_run = search.get("last_run_at")
                        if last_run:
                            import datetime as _dt
                            last = _dt.datetime.fromisoformat(str(last_run).replace('Z',''))
                            due = last + _dt.timedelta(minutes=intvl)
                            if _dt.datetime.utcnow() < due:
                                continue
                        await _execute_saved_search(dict(search))
                    except Exception as e:
                        logger.debug("saved_search run error: %s", e)
        except Exception as e:
            logger.error("saved_searches_loop error: %s", e)
        await asyncio.sleep(interval)


async def priority_aging_loop():
    """
    Starvation prevention: periodically bump priority of long-waiting torrents.

    Config keys (all with safe defaults):
      priority_aging_interval_minutes  (int, default 15)
      priority_aging_threshold_minutes (int, default 60)
      priority_aging_step              (int, default 1)
    """
    await asyncio.sleep(90)          # let startup settle
    while True:
        try:
            cfg      = get_settings()
            interval  = max(1, int(getattr(cfg, "priority_aging_interval_minutes",  15) or 15))
            threshold = max(1, int(getattr(cfg, "priority_aging_threshold_minutes", 60) or 60))
            step      = max(1, int(getattr(cfg, "priority_aging_step",              1)  or 1))
            if interval > 0:
                from db.database import get_db
                async with get_db() as db:
                    result = await db.execute(
                        """UPDATE torrents
                              SET priority   = MIN(priority + ?, 900),
                                  updated_at = CURRENT_TIMESTAMP
                            WHERE status IN ('ready','uploading','processing')
                              AND priority < 900
                              AND (JULIANDAY('now') - JULIANDAY(created_at)) * 1440 > ?""",
                        (step, threshold),
                    )
                    aged = getattr(result, "rowcount", 0) or 0
                    if aged:
                        await db.commit()
                        logger.debug("priority_aging: bumped %d torrent(s) by +%d", aged, step)
        except Exception as exc:
            logger.debug("priority_aging_loop error: %s", exc)
        await asyncio.sleep(
            max(1, int(getattr(get_settings(), "priority_aging_interval_minutes", 15) or 15)) * 60
        )


async def recovery_loop():
    """Auto-recovery: detect and heal stuck states every 5 minutes."""
    await asyncio.sleep(120)         # wait for full startup before first check
    while True:
        try:
            from services.recovery import run_recovery_checks
            result = await run_recovery_checks()
            if any([result["orphaned_queued_files"],
                    result["missed_completions"],
                    result["deadlock_reset"]]):
                logger.info("recovery: %s", result)
        except Exception as exc:
            logger.debug("recovery_loop error: %s", exc)
        await asyncio.sleep(300)


async def start_scheduler():
    _tasks.append(asyncio.create_task(watch_folder_loop()))
    _tasks.append(asyncio.create_task(sync_status_loop()))
    _tasks.append(asyncio.create_task(full_sync_loop()))
    _tasks.append(asyncio.create_task(sync_download_clients_loop()))
    _tasks.append(asyncio.create_task(deep_sync_loop()))
    _tasks.append(asyncio.create_task(aria2_housekeeping_loop()))
    _tasks.append(asyncio.create_task(aria2_log_rotation_loop()))
    _tasks.append(asyncio.create_task(backup_loop()))
    _tasks.append(asyncio.create_task(flexget_loop()))
    _tasks.append(asyncio.create_task(stats_snapshot_loop()))
    _tasks.append(asyncio.create_task(stats_report_loop()))
    _tasks.append(asyncio.create_task(aria2_restart_loop()))
    _tasks.append(asyncio.create_task(update_check_loop()))
    _tasks.append(asyncio.create_task(events_ttl_loop()))
    _tasks.append(asyncio.create_task(saved_searches_loop()))
    _tasks.append(asyncio.create_task(priority_aging_loop()))
    _tasks.append(asyncio.create_task(recovery_loop()))
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
        window_h = _stats_report_window_hours(cfg)
        if interval_h <= 0 or not _has_reporting_webhook(cfg):
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(max(300, interval_h * 3600))
        try:
            from services.stats import send_stats_report
            await send_stats_report(hours=window_h, triggered_by="schedule")
        except Exception as e:
            logger.error(f"Stats report error: {e}")

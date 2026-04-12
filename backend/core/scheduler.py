import asyncio
import logging
from core.config import settings
from services.manager import manager

logger = logging.getLogger("alldebrid.scheduler")

_tasks = []


async def watch_folder_loop():
    while True:
        try:
            await manager.scan_watch_folder()
        except Exception as e:
            logger.error(f"Watch folder error: {e}")
        await asyncio.sleep(settings.watch_interval_seconds)


async def sync_status_loop():
    while True:
        try:
            if settings.alldebrid_api_key:
                await manager.sync_alldebrid_status()
        except Exception as e:
            logger.error(f"Status sync error: {e}")
        await asyncio.sleep(settings.poll_interval_seconds)


async def start_scheduler():
    _tasks.append(asyncio.create_task(watch_folder_loop()))
    _tasks.append(asyncio.create_task(sync_status_loop()))
    logger.info("Scheduler started")


async def stop_scheduler():
    for t in _tasks:
        t.cancel()
    _tasks.clear()
    logger.info("Scheduler stopped")

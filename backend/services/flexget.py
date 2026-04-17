"""
FlexGet integration service.

Connects to FlexGet's API or CLI to:
- List available tasks
- Execute tasks (single or all)
- Return structured results and status
- Send webhook events per task lifecycle
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.flexget")


def _cfg():
    from core.config import get_settings
    return get_settings()


# ── FlexGet API client ────────────────────────────────────────────────────────

class FlexGetClient:
    """Talks to FlexGet's REST API (requires flexget web server running)."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Token {self.api_key}"
        return h

    async def list_tasks(self) -> List[str]:
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.get(f"{self.base_url}/api/tasks/", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return [t.get("name", t) if isinstance(t, dict) else str(t) for t in data]
                    return []
        except Exception as exc:
            logger.warning("FlexGet list_tasks failed: %s", exc)
            return []

    async def execute_task(self, task: str) -> Dict[str, Any]:
        """Execute a single task via FlexGet API. Returns result dict."""
        started = time.time()
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(
                    f"{self.base_url}/api/tasks/{task}/execute/",
                    json={},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as r:
                    elapsed = round(time.time() - started, 2)
                    body = {}
                    try:
                        body = await r.json()
                    except Exception:
                        body = {"raw": await r.text()}
                    return {
                        "task":     task,
                        "status":   "ok" if r.status < 300 else "error",
                        "http":     r.status,
                        "elapsed":  elapsed,
                        "result":   body,
                    }
        except asyncio.TimeoutError:
            return {"task": task, "status": "timeout", "elapsed": 300.0, "result": {}}
        except Exception as exc:
            return {"task": task, "status": "error", "error": str(exc), "elapsed": round(time.time()-started,2), "result": {}}

    async def execute_tasks(self, tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Execute multiple (or all) tasks. Returns list of results."""
        if tasks is None:
            tasks = await self.list_tasks()
        results = []
        for t in tasks:
            results.append(await self.execute_task(t))
        return results


# ── FlexGet runner (with webhook events) ──────────────────────────────────────

async def run_flexget_tasks(tasks: Optional[List[str]] = None, triggered_by: str = "manual") -> List[Dict[str, Any]]:
    """
    Run FlexGet tasks and emit webhook events.
    Returns list of task results.
    """
    cfg = _cfg()
    if not getattr(cfg, "flexget_enabled", False):
        return []

    url     = getattr(cfg, "flexget_url", "http://localhost:5050")
    api_key = getattr(cfg, "flexget_api_key", "")
    client  = FlexGetClient(url, api_key)

    # Resolve tasks to run
    run_tasks = tasks or (getattr(cfg, "flexget_tasks", None) or None)

    from services.notifications import NotificationService
    notif = NotificationService()

    results: List[Dict[str, Any]] = []
    ts = datetime.now(timezone.utc).isoformat()

    # Event: started
    await _emit_flexget_webhook("run_started", {
        "triggered_by": triggered_by,
        "tasks":        run_tasks or "all",
        "timestamp":    ts,
    })

    task_results = await client.execute_tasks(run_tasks)
    results = task_results

    # Persist run to DB
    await _persist_run(task_results, triggered_by)

    # Event: per-task
    for r in task_results:
        event = "task_ok" if r.get("status") == "ok" else "task_error"
        await _emit_flexget_webhook(event, r)

    # Event: summary
    ok    = sum(1 for r in task_results if r.get("status") == "ok")
    errs  = len(task_results) - ok
    await _emit_flexget_webhook("run_finished", {
        "triggered_by": triggered_by,
        "tasks_total":  len(task_results),
        "tasks_ok":     ok,
        "tasks_error":  errs,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    logger.info("FlexGet run complete (%s tasks, %d ok, %d error)", len(task_results), ok, errs)
    return results


async def _emit_flexget_webhook(event: str, payload: Dict[str, Any]) -> None:
    """Send FlexGet event to configured webhook URL."""
    cfg = _cfg()
    webhook_url = getattr(cfg, "flexget_webhook_url", "") or ""
    if not webhook_url:
        return
    try:
        import aiohttp as _aio
        body = {"event": event, "source": "flexget", **payload}
        async with _aio.ClientSession() as s:
            await s.post(webhook_url, json=body, timeout=_aio.ClientTimeout(total=10))
    except Exception as exc:
        logger.debug("FlexGet webhook failed (%s): %s", event, exc)


async def _persist_run(results: List[Dict[str, Any]], triggered_by: str) -> None:
    """Write FlexGet run results to the flexget_runs table."""
    try:
        import json
        from db.database import get_db
        async with get_db() as db:
            for r in results:
                await db.execute(
                    """INSERT INTO flexget_runs
                       (task_name, status, elapsed_seconds, result_json, triggered_by, ran_at)
                       VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (
                        r.get("task", "unknown"),
                        r.get("status", "unknown"),
                        r.get("elapsed", 0),
                        json.dumps(r.get("result", {})),
                        triggered_by,
                    ),
                )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to persist FlexGet run: %s", exc)

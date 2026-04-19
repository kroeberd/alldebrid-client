"""
FlexGet integration service — FlexGet v3.x API.

FlexGet v3 REST API endpoints:
  GET  /api/tasks/              → list all configured tasks
  POST /api/tasks/execute/      → execute tasks (body: {tasks: ["name"]} or {} for all)
  GET  /api/tasks/queue/{id}/   → poll execution status

Auth: Authorization: Token {api_key}
"""
from __future__ import annotations

import asyncio
import json
import logging
<<<<<<< HEAD
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
=======
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote
>>>>>>> 5794aeb134b4c2391dba583da78847a0b1460987

import aiohttp

logger = logging.getLogger("alldebrid.flexget")

_TASK_TIMEOUT  = 300   # max seconds per task
_POLL_INTERVAL = 3     # seconds between polls


def _cfg():
    from core.config import get_settings
    return get_settings()


def _client() -> "FlexGetClient":
    cfg = _cfg()
    return FlexGetClient(
        base_url=getattr(cfg, "flexget_url", "http://localhost:5050"),
        api_key=getattr(cfg, "flexget_api_key", ""),
    )


def _configured_tasks() -> Optional[List[str]]:
    """Return task list from flexget_tasks_raw (comma-separated), or None = run all."""
    cfg = _cfg()
    raw = (getattr(cfg, "flexget_tasks_raw", "") or "").strip()
    if not raw:
        return None
    tasks = [t.strip() for t in raw.split(",") if t.strip()]
    return tasks or None


<<<<<<< HEAD
def get_task_schedules() -> List[Dict[str, Any]]:
    """
    Returns normalized FlexGet task schedules.

    Preferred format:
      flexget_task_schedules_json = [
        {"task": "movies", "interval_minutes": 60, "jitter_seconds": 300, "enabled": true}
      ]

    Legacy fallback:
      flexget_schedule_minutes + flexget_jitter_seconds + flexget_tasks_raw
    """
    cfg = _cfg()
    raw = (getattr(cfg, "flexget_task_schedules_json", "") or "").strip()
    schedules: List[Dict[str, Any]] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    task = str(item.get("task", "")).strip()
                    if not task:
                        continue
                    try:
                        interval_minutes = int(item.get("interval_minutes", 0) or 0)
                    except Exception:
                        interval_minutes = 0
                    try:
                        jitter_seconds = int(item.get("jitter_seconds", 0) or 0)
                    except Exception:
                        jitter_seconds = 0
                    enabled = bool(item.get("enabled", True))
                    schedules.append({
                        "task": task,
                        "interval_minutes": max(0, min(interval_minutes, 720)),
                        "jitter_seconds": max(0, min(jitter_seconds, 3600)),
                        "enabled": enabled,
                    })
        except Exception as exc:
            logger.warning("Invalid FlexGet task schedule config ignored: %s", exc)

    if schedules:
        return schedules

    interval_minutes = max(0, min(int(getattr(cfg, "flexget_schedule_minutes", 0) or 0), 720))
    jitter_seconds = max(0, min(int(getattr(cfg, "flexget_jitter_seconds", 0) or 0), 3600))
    tasks = _configured_tasks()
    if interval_minutes <= 0:
        return []
    if tasks:
        return [
            {
                "task": task,
                "interval_minutes": interval_minutes,
                "jitter_seconds": jitter_seconds,
                "enabled": True,
            }
            for task in tasks
        ]
    return [{
        "task": "*",
        "interval_minutes": interval_minutes,
        "jitter_seconds": jitter_seconds,
        "enabled": True,
    }]


def schedule_signature(schedules: List[Dict[str, Any]]) -> tuple:
    return tuple(
        sorted(
            (
                str(item.get("task", "")).strip(),
                int(item.get("interval_minutes", 0) or 0),
                int(item.get("jitter_seconds", 0) or 0),
                bool(item.get("enabled", True)),
            )
            for item in schedules
        )
    )


def next_delay_seconds(schedule: Dict[str, Any]) -> float:
    interval_seconds = max(10, int(schedule.get("interval_minutes", 0) or 0) * 60)
    jitter_seconds = max(0, int(schedule.get("jitter_seconds", 0) or 0))
    if jitter_seconds <= 0:
        return float(interval_seconds)
    return float(max(10, interval_seconds + random.uniform(-jitter_seconds, jitter_seconds)))


=======
>>>>>>> 5794aeb134b4c2391dba583da78847a0b1460987
class FlexGetClient:
    """Client for FlexGet v3 REST API."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key.strip()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Token {self.api_key}"
        return h

    async def list_tasks(self) -> List[str]:
        """GET /api/tasks/ — returns list of task names."""
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.get(
                    f"{self.base_url}/api/tasks/",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning("FlexGet list_tasks HTTP %s", r.status)
                        return []
                    data = await r.json(content_type=None)
                    if isinstance(data, list):
                        return [
                            t.get("name", t) if isinstance(t, dict) else str(t)
                            for t in data
                        ]
                    return []
        except Exception as exc:
            logger.warning("FlexGet list_tasks failed: %s", exc)
            return []

    async def execute_tasks(self, tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        POST /api/tasks/execute/ — trigger one or more tasks.
        tasks=None → execute all configured tasks.
        Returns per-task result dicts.
        """
        if tasks is None:
            tasks = await self.list_tasks()
        if not tasks:
            logger.warning("FlexGet execute_tasks: no tasks to run")
            return []

        started_all = time.monotonic()

        # Single POST for all tasks — FlexGet v3 accepts a list
        body_payload = {"tasks": tasks}
        try:
            async with aiohttp.ClientSession(
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as s:
                url = f"{self.base_url}/api/tasks/execute/"
                logger.debug("FlexGet POST %s body=%s", url, body_payload)

                async with s.post(url, json=body_payload) as r:
                    http_status = r.status
                    try:
                        resp = await r.json(content_type=None)
                    except Exception:
                        resp = {"raw": await r.text()}

                    logger.debug("FlexGet execute → HTTP %s body=%s", http_status, str(resp)[:300])

                    if http_status == 401:
                        err = "Unauthorized — check FlexGet API key"
                        logger.error("FlexGet execute: %s", err)
                        return [{"task": t, "status": "error", "error": err, "elapsed": 0.0, "result": {}} for t in tasks]

                    if http_status not in (200, 201, 202):
                        err = f"HTTP {http_status}: {str(resp)[:200]}"
                        logger.error("FlexGet execute: %s", err)
                        return [{"task": t, "status": "error", "error": err, "elapsed": 0.0, "result": {}} for t in tasks]

                    # Extract execution entries: {"tasks": [{"id": 1, "name": "task"}, ...]}
                    exec_entries = resp.get("tasks") or resp.get("executions") or []
                    if not exec_entries:
                        # Unexpected response — treat as ok if 2xx
                        logger.warning("FlexGet execute: no execution entries in response: %s", str(resp)[:300])
                        return [{"task": t, "status": "ok", "elapsed": 0.0, "result": resp} for t in tasks]

        except asyncio.TimeoutError:
            return [{"task": t, "status": "timeout", "elapsed": 30.0, "result": {}} for t in tasks]
        except Exception as exc:
            logger.error("FlexGet execute exception: %s", exc)
            return [{"task": t, "status": "error", "error": str(exc), "elapsed": 0.0, "result": {}} for t in tasks]

        # Poll each execution until done
        results = []
        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as poll_session:
            for entry in exec_entries:
                exec_id  = str(entry.get("id", ""))
                task_name = entry.get("name", "unknown")
                started  = time.monotonic()

                if not exec_id:
                    results.append({"task": task_name, "status": "ok", "elapsed": 0.0, "result": entry})
                    continue

                result = await self._poll_execution(poll_session, exec_id, task_name, started)
                results.append(result)

        # Add results for tasks that had no matching execution entry
        found_tasks = {r["task"] for r in results}
        for t in tasks:
            if t not in found_tasks:
                results.append({"task": t, "status": "ok", "elapsed": round(time.monotonic()-started_all, 2), "result": {}})

        return results

    async def _poll_execution(
        self,
        session: aiohttp.ClientSession,
        exec_id: str,
        task_name: str,
        started: float,
    ) -> Dict[str, Any]:
        """Poll GET /api/tasks/queue/{id}/ until task finishes."""
        deadline = time.monotonic() + _TASK_TIMEOUT
        # FlexGet v3: /api/tasks/queue/{id}/
        poll_urls = [
            f"{self.base_url}/api/tasks/queue/{exec_id}/",
            f"{self.base_url}/api/tasks/executions/{exec_id}/",
        ]

        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            for url in poll_urls:
                try:
                    async with session.get(url) as r:
                        if r.status == 404:
                            continue
                        data = await r.json(content_type=None)
                        state = str(
                            data.get("status") or data.get("state") or ""
                        ).lower()

                        if state in ("pending", "running", "in_progress", "in progress", ""):
                            break  # still running

                        elapsed = round(time.monotonic() - started, 2)
                        success = state in ("succeeded", "success", "done", "complete", "completed", "finished")
                        logger.info(
                            "FlexGet task %s: state=%s elapsed=%.1fs accepted=%s failed=%s",
                            task_name, state, elapsed,
                            data.get("accepted", "?"), data.get("failed", "?"),
                        )
                        return {
                            "task":    task_name,
                            "status":  "ok" if success else "error",
                            "state":   state,
                            "elapsed": elapsed,
                            "result":  data,
                        }
                except Exception as exc:
                    logger.debug("FlexGet poll %s: %s", url, exc)
                    continue
            else:
                continue
            break  # inner break → retry outer loop

        return {
            "task": task_name, "status": "timeout",
            "elapsed": _TASK_TIMEOUT, "result": {},
        }

    async def execute_task(self, task: str) -> Dict[str, Any]:
        """Execute a single task (convenience wrapper)."""
        results = await self.execute_tasks([task])
        return results[0] if results else {"task": task, "status": "error", "error": "no result", "elapsed": 0.0, "result": {}}


async def run_flexget_tasks(
    tasks: Optional[List[str]] = None,
    triggered_by: str = "manual",
) -> List[Dict[str, Any]]:
    cfg = _cfg()
    if not getattr(cfg, "flexget_enabled", False):
        return []

    client    = _client()
    run_tasks = tasks or _configured_tasks()

    await _emit_flexget_webhook("run_started", {
        "triggered_by": triggered_by,
        "tasks":        run_tasks or "all",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    task_results = await client.execute_tasks(run_tasks)
    await _persist_run(task_results, triggered_by)

    for r in task_results:
        await _emit_flexget_webhook(
            "task_ok" if r.get("status") == "ok" else "task_error", r
        )

    ok   = sum(1 for r in task_results if r.get("status") == "ok")
    errs = len(task_results) - ok
    await _emit_flexget_webhook("run_finished", {
        "triggered_by": triggered_by,
        "tasks_total":  len(task_results),
        "tasks_ok":     ok,
        "tasks_error":  errs,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    logger.info("FlexGet run: %d tasks, %d ok, %d error", len(task_results), ok, errs)
    return task_results


async def _emit_flexget_webhook(event: str, payload: Dict[str, Any]) -> None:
    cfg = _cfg()
    url = (getattr(cfg, "flexget_webhook_url", "") or "").strip()
    if not url:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                url,
                json={"event": event, "source": "flexget", **payload},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as exc:
        logger.debug("FlexGet webhook failed (%s): %s", event, exc)


async def _persist_run(results: List[Dict[str, Any]], triggered_by: str) -> None:
    try:
        from db.database import get_db
        async with get_db() as db:
            for r in results:
                await db.execute(
                    """INSERT INTO flexget_runs
                       (task_name, status, elapsed_seconds, result_json, triggered_by, ran_at)
                       VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (r.get("task","unknown"), r.get("status","unknown"),
                     r.get("elapsed", 0), json.dumps(r.get("result",{})), triggered_by),
                )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to persist FlexGet run: %s", exc)

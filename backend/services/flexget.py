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
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import aiohttp

logger = logging.getLogger("alldebrid.flexget")

_TASK_TIMEOUT  = 300   # max seconds per task
_POLL_INTERVAL = 3     # seconds between polls

# ── Concurrency guards ────────────────────────────────────────────────────────
# Prevents the same task from running more than once simultaneously.
# Key: task name (lowercase), value: asyncio.Lock
_task_locks: Dict[str, asyncio.Lock] = {}
# Tracks which tasks are currently executing (for sidebar indicator)
_running_tasks: Set[str] = set()
# Reachability state — used to send recover/unreachable webhooks only on change
_last_reachable: Optional[bool] = None


def _task_lock(task: str) -> asyncio.Lock:
    key = task.strip().lower()
    if key not in _task_locks:
        _task_locks[key] = asyncio.Lock()
    return _task_locks[key]


def is_task_running(task: str) -> bool:
    return task.strip().lower() in _running_tasks


def running_tasks() -> List[str]:
    return list(_running_tasks)


# ── Config helpers ────────────────────────────────────────────────────────────

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


# ── Schedule helpers ──────────────────────────────────────────────────────────

def get_task_schedules() -> List[Dict[str, Any]]:
    """
    Returns normalized FlexGet task schedules.

    Preferred format (flexget_task_schedules_json):
      [{"task": "movies", "interval_minutes": 60, "jitter_seconds": 300, "enabled": true}]

    Legacy fallback: flexget_schedule_minutes + flexget_jitter_seconds + flexget_tasks_raw
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

    # Legacy fallback
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
    return [{"task": "*", "interval_minutes": interval_minutes, "jitter_seconds": jitter_seconds, "enabled": True}]


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


# ── FlexGet REST client ───────────────────────────────────────────────────────

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

                    exec_entries = resp.get("tasks") or resp.get("executions") or []
                    if not exec_entries:
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
                exec_id   = str(entry.get("id", ""))
                task_name = entry.get("name", "unknown")
                started   = time.monotonic()

                if not exec_id:
                    results.append({"task": task_name, "status": "ok", "elapsed": 0.0, "result": entry})
                    continue

                result = await self._poll_execution(poll_session, exec_id, task_name, started)
                results.append(result)

        # Ensure all requested tasks have a result entry
        found_tasks = {r["task"] for r in results}
        for t in tasks:
            if t not in found_tasks:
                results.append({"task": t, "status": "ok", "elapsed": round(time.monotonic() - started_all, 2), "result": {}})

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
                        state = str(data.get("status") or data.get("state") or "").lower()

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
            break

        return {"task": task_name, "status": "timeout", "elapsed": _TASK_TIMEOUT, "result": {}}

    async def execute_task(self, task: str) -> Dict[str, Any]:
        """Execute a single task (convenience wrapper)."""
        results = await self.execute_tasks([task])
        return results[0] if results else {"task": task, "status": "error", "error": "no result", "elapsed": 0.0, "result": {}}


# ── Reachability ──────────────────────────────────────────────────────────────

async def _check_reachable() -> bool:
    """Returns True if FlexGet API responds within 8s."""
    cfg = _cfg()
    url = (getattr(cfg, "flexget_url", "") or "").rstrip("/")
    if not url:
        return False
    api_key = (getattr(cfg, "flexget_api_key", "") or "").strip()
    headers = {"Authorization": f"Token {api_key}"} if api_key else {}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{url}/api/tasks/", timeout=aiohttp.ClientTimeout(total=8)) as r:
                return r.status < 500
    except Exception:
        return False


# ── Task webhook helpers ──────────────────────────────────────────────────────

async def _emit_task_webhook(task: str, event: str, payload: Dict[str, Any]) -> None:
    """
    Emit a task-specific webhook if a matching URL is configured.

    Config key: flexget_task_webhooks_json
    Format: [{"task": "movies", "url": "https://…", "events": ["task_started", "task_ok", "task_error"]}]

    Falls back to the global FlexGet webhook for unconfigured tasks.
    """
    cfg = _cfg()
    raw = (getattr(cfg, "flexget_task_webhooks_json", "") or "").strip()
    url = ""
    if raw:
        try:
            task_webhooks = json.loads(raw)
            for entry in task_webhooks:
                if not isinstance(entry, dict):
                    continue
                if entry.get("task", "").strip().lower() != task.strip().lower():
                    continue
                # Check if this event is in the entry's event filter (empty = all events)
                allowed = entry.get("events") or []
                if allowed and event not in allowed:
                    continue
                url = (entry.get("url") or "").strip()
                break
        except Exception as exc:
            logger.debug("flexget_task_webhooks_json parse error: %s", exc)

    if not url:
        # Fall back to global FlexGet webhook (which itself falls back to Discord)
        logger.debug("FlexGet task webhook (%s/%s): no task-specific URL — using global", task, event)
        await _emit_flexget_webhook(event, {"task": task, **payload})
        return

    try:
        body = {"event": event, "source": "flexget", "task": task, **payload}
        async with aiohttp.ClientSession() as s:
            resp = await s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10))
            logger.info("FlexGet task webhook sent: task=%s event=%s status=%s", task, event, resp.status)
    except Exception as exc:
        logger.warning("FlexGet task webhook failed (%s/%s): %s", task, event, exc)


# ── Main entry points ─────────────────────────────────────────────────────────

async def run_flexget_tasks(
    tasks: Optional[List[str]] = None,
    triggered_by: str = "manual",
) -> List[Dict[str, Any]]:
    """
    Run FlexGet tasks with:
    - Reachability check + configurable retry delay
    - Per-task mutex (prevents duplicate concurrent execution)
    - Per-task start webhook
    - Global unreachable/recovered webhooks (deduplicated)
    """
    global _last_reachable

    cfg = _cfg()
    if not getattr(cfg, "flexget_enabled", False):
        return []

    # ── Reachability check with one retry ────────────────────────────────────
    reachable = await _check_reachable()
    if not reachable:
        retry_delay = max(1, int(getattr(cfg, "flexget_retry_delay_minutes", 5) or 5)) * 60
        logger.warning(
            "FlexGet unreachable — waiting %ds before retry (triggered_by=%s)",
            retry_delay, triggered_by,
        )
        await asyncio.sleep(retry_delay)
        reachable = await _check_reachable()

    if not reachable:
        logger.error("FlexGet still unreachable after retry — aborting (triggered_by=%s)", triggered_by)
        if _last_reachable is not False:
            await _emit_flexget_webhook("server_unreachable", {
                "triggered_by": triggered_by,
                "message":      "FlexGet did not respond after retry",
                "timestamp":    datetime.now(timezone.utc).isoformat(),
            })
        _last_reachable = False
        return []

    if _last_reachable is False:
        logger.info("FlexGet recovered — sending recovery webhook")
        await _emit_flexget_webhook("server_recovered", {
            "triggered_by": triggered_by,
            "message":      "FlexGet is reachable again",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        })
    _last_reachable = True

    # ── Resolve task list ─────────────────────────────────────────────────────
    client    = _client()
    run_tasks = tasks or _configured_tasks()

    logger.info(
        "FlexGet run starting: tasks=%s triggered_by=%s",
        run_tasks or "all", triggered_by,
    )
    await _emit_flexget_webhook("run_started", {
        "triggered_by": triggered_by,
        "tasks":        run_tasks or "all",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    # ── Execute tasks with per-task mutex ─────────────────────────────────────
    task_results: List[Dict[str, Any]] = []

    if run_tasks:
        for task in run_tasks:
            lock = _task_lock(task)
            if lock.locked():
                logger.info(
                    "FlexGet task '%s' already running — skipping duplicate (triggered_by=%s)",
                    task, triggered_by,
                )
                task_results.append({
                    "task": task, "status": "skipped",
                    "error": "already running", "elapsed": 0.0, "result": {},
                })
                continue

            async with lock:
                _running_tasks.add(task.strip().lower())
                ts = datetime.now(timezone.utc).isoformat()
                await _emit_task_webhook(task, "task_started", {
                    "triggered_by": triggered_by,
                    "timestamp": ts,
                })
                try:
                    result = await client.execute_task(task)
                finally:
                    _running_tasks.discard(task.strip().lower())

                task_results.append(result)
                event = "task_ok" if result.get("status") == "ok" else "task_error"
                await _emit_task_webhook(task, event, result)
    else:
        # Run all tasks — no per-task mutex for "all" mode, use global lock
        task_results = await client.execute_tasks(None)
        for r in task_results:
            event = "task_ok" if r.get("status") == "ok" else "task_error"
            await _emit_flexget_webhook(event, r)

    await _persist_run(task_results, triggered_by)

    ok   = sum(1 for r in task_results if r.get("status") == "ok")
    errs = sum(1 for r in task_results if r.get("status") not in ("ok", "skipped"))
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
    """Send global FlexGet event to configured webhook URL (falls back to Discord)."""
    cfg = _cfg()
    url = (getattr(cfg, "flexget_webhook_url", "") or "").strip()
    if not url:
        url = (getattr(cfg, "discord_webhook_url", "") or "").strip()
    if not url:
        logger.debug("FlexGet webhook (%s): no URL configured — skipping", event)
        return
    try:
        body = {"event": event, "source": "flexget", **payload}
        async with aiohttp.ClientSession() as s:
            resp = await s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10))
            logger.info("FlexGet webhook sent: event=%s status=%s url=%s", event, resp.status, url[:60])
    except Exception as exc:
        logger.warning("FlexGet webhook failed (%s): %s", event, exc)


async def _persist_run(results: List[Dict[str, Any]], triggered_by: str) -> None:
    """Write FlexGet run results to the flexget_runs table."""
    try:
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

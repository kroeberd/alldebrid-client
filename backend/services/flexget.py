"""
FlexGet integration service.

FlexGet REST API is asynchronous:
  POST /api/tasks/{task}/execute/  → 202 Accepted + {execution_id}
  GET  /api/tasks/{task}/executions/{id}/  → poll for result
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.flexget")

# How long to wait for a single task to finish (seconds)
_TASK_TIMEOUT   = 300
# Poll interval when waiting for task completion
_POLL_INTERVAL  = 3


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
    """Return task list from flexget_tasks_raw, or None (= run all)."""
    cfg = _cfg()
    raw = (getattr(cfg, "flexget_tasks_raw", "") or "").strip()
    if not raw:
        return None
    tasks = [t.strip() for t in raw.split(",") if t.strip()]
    return tasks or None


# ── FlexGet API client ────────────────────────────────────────────────────────

class FlexGetClient:
    """Async client for FlexGet REST API v2 (web_server plugin)."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Token {self.api_key}"
        return h

    async def list_tasks(self) -> List[str]:
        """Return list of configured task names."""
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.get(
                    f"{self.base_url}/api/tasks/",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning("FlexGet list_tasks: HTTP %s", r.status)
                        return []
                    data = await r.json(content_type=None)
                    # FlexGet returns list of task objects with 'name' key
                    if isinstance(data, list):
                        return [
                            t.get("name", t) if isinstance(t, dict) else str(t)
                            for t in data
                        ]
                    return []
        except Exception as exc:
            logger.warning("FlexGet list_tasks failed: %s", exc)
            return []

    async def execute_task(self, task: str) -> Dict[str, Any]:
        """
        Execute a single task and wait for completion.

        FlexGet execute API is async:
          POST .../execute/ → 202 + {execution_id}
          GET  .../executions/{id}/ → poll until status != pending/running
        """
        started = time.monotonic()

        # Step 1: trigger execution
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as s:
                async with s.post(
                    f"{self.base_url}/api/tasks/{task}/execute/",
                    json={},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    body = {}
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        body = {"raw": await r.text()}

                    if r.status == 404:
                        return {
                            "task": task, "status": "error",
                            "error": f"Task not found: {task}",
                            "elapsed": round(time.monotonic() - started, 2), "result": body,
                        }
                    if r.status not in (200, 201, 202):
                        return {
                            "task": task, "status": "error",
                            "error": f"HTTP {r.status}", "http": r.status,
                            "elapsed": round(time.monotonic() - started, 2), "result": body,
                        }

                    # 202: async execution — poll for result
                    execution_id = body.get("execution_id") or body.get("id")
                    if not execution_id:
                        # Some versions return result directly on 200
                        elapsed = round(time.monotonic() - started, 2)
                        return {
                            "task": task,
                            "status": "ok" if r.status < 300 else "error",
                            "http": r.status, "elapsed": elapsed, "result": body,
                        }
        except asyncio.TimeoutError:
            return {"task": task, "status": "timeout", "elapsed": 30.0, "result": {}}
        except Exception as exc:
            return {
                "task": task, "status": "error", "error": str(exc),
                "elapsed": round(time.monotonic() - started, 2), "result": {},
            }

        # Step 2: poll for completion
        deadline = time.monotonic() + _TASK_TIMEOUT
        async with aiohttp.ClientSession(headers=self._headers()) as s:
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                try:
                    async with s.get(
                        f"{self.base_url}/api/tasks/{task}/executions/{execution_id}/",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as poll:
                        if poll.status == 404:
                            # Try alternative endpoint
                            async with s.get(
                                f"{self.base_url}/api/tasks/executions/{execution_id}/",
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as poll2:
                                result = await poll2.json(content_type=None)
                        else:
                            result = await poll.json(content_type=None)

                        state = str(result.get("status", result.get("state", ""))).lower()
                        if state in ("pending", "running", ""):
                            continue  # still running

                        elapsed = round(time.monotonic() - started, 2)
                        success = state in ("succeeded", "success", "finished", "complete", "done")
                        return {
                            "task": task,
                            "status": "ok" if success else "error",
                            "execution_id": execution_id,
                            "state": state,
                            "elapsed": elapsed,
                            "result": result,
                        }
                except Exception as exc:
                    logger.debug("FlexGet poll failed for %s/%s: %s", task, execution_id, exc)

        return {
            "task": task, "status": "timeout",
            "execution_id": execution_id,
            "elapsed": _TASK_TIMEOUT, "result": {},
        }

    async def execute_tasks(self, tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Execute multiple tasks sequentially. tasks=None → run all."""
        if tasks is None:
            tasks = await self.list_tasks()
        if not tasks:
            logger.warning("FlexGet execute_tasks: no tasks to run")
            return []
        results = []
        for t in tasks:
            results.append(await self.execute_task(t))
        return results


# ── FlexGet runner ────────────────────────────────────────────────────────────

async def run_flexget_tasks(
    tasks: Optional[List[str]] = None,
    triggered_by: str = "manual",
) -> List[Dict[str, Any]]:
    """
    Run FlexGet tasks and emit webhook events.
    tasks=None → use flexget_tasks_raw config or run all.
    """
    cfg = _cfg()
    if not getattr(cfg, "flexget_enabled", False):
        return []

    client = _client()
    run_tasks = tasks or _configured_tasks()

    ts = datetime.now(timezone.utc).isoformat()
    await _emit_flexget_webhook("run_started", {
        "triggered_by": triggered_by,
        "tasks": run_tasks or "all",
        "timestamp": ts,
    })

    task_results = await client.execute_tasks(run_tasks)

    await _persist_run(task_results, triggered_by)

    for r in task_results:
        event = "task_ok" if r.get("status") == "ok" else "task_error"
        await _emit_flexget_webhook(event, r)

    ok   = sum(1 for r in task_results if r.get("status") == "ok")
    errs = len(task_results) - ok
    await _emit_flexget_webhook("run_finished", {
        "triggered_by": triggered_by,
        "tasks_total":  len(task_results),
        "tasks_ok":     ok,
        "tasks_error":  errs,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    })

    logger.info("FlexGet run complete: %d tasks, %d ok, %d error", len(task_results), ok, errs)
    return task_results


async def _emit_flexget_webhook(event: str, payload: Dict[str, Any]) -> None:
    cfg = _cfg()
    webhook_url = (getattr(cfg, "flexget_webhook_url", "") or "").strip()
    if not webhook_url:
        return
    try:
        body = {"event": event, "source": "flexget", **payload}
        async with aiohttp.ClientSession() as s:
            await s.post(webhook_url, json=body, timeout=aiohttp.ClientTimeout(total=10))
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

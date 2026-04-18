"""
FlexGet integration service.

FlexGet REST API endpoint for task execution:
  POST /api/tasks/{name}/execute/
  Response: 202 + body containing execution info (format varies by version)

Authentication: Authorization: Token {api_key}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("alldebrid.flexget")

_TASK_TIMEOUT  = 300   # max seconds to wait for a task
_POLL_INTERVAL = 3     # seconds between status polls


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


class FlexGetClient:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key.strip()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Token {self.api_key}"
        return h

    async def list_tasks(self) -> List[str]:
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
        Execute a single FlexGet task via REST API.
        Tries execute endpoint and waits for completion via polling.
        """
        started = time.monotonic()
        task_enc = quote(task, safe="")

        try:
            async with aiohttp.ClientSession(
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as s:
                url = f"{self.base_url}/api/tasks/{task_enc}/execute/"
                logger.debug("FlexGet execute POST %s", url)

                async with s.post(url, json={}) as r:
                    http_status = r.status
                    try:
                        body = await r.json(content_type=None)
                    except Exception:
                        body = {"raw": await r.text()}

                    logger.debug(
                        "FlexGet execute %s → HTTP %s body=%s",
                        task, http_status, str(body)[:200],
                    )

                    if http_status == 401:
                        return {
                            "task": task, "status": "error",
                            "error": "Unauthorized — check API key",
                            "http": 401, "elapsed": round(time.monotonic()-started, 2),
                            "result": body,
                        }
                    if http_status == 404:
                        return {
                            "task": task, "status": "error",
                            "error": f"Task or endpoint not found (HTTP 404) for: {task}",
                            "http": 404, "elapsed": round(time.monotonic()-started, 2),
                            "result": body,
                        }
                    if http_status not in (200, 201, 202):
                        return {
                            "task": task, "status": "error",
                            "error": f"HTTP {http_status}",
                            "http": http_status, "elapsed": round(time.monotonic()-started, 2),
                            "result": body,
                        }

                    # Extract execution_id from various FlexGet response formats
                    # FlexGet v3: {"tasks": [{"id": <exec_id>, "name": "..."}]}
                    # FlexGet older: {"execution_id": <id>} or {"id": <id>}
                    execution_id = (
                        body.get("execution_id")
                        or body.get("id")
                        or (body.get("tasks") or [{}])[0].get("id")
                        or (body.get("executions") or [{}])[0].get("id")
                    )

                    if not execution_id:
                        elapsed = round(time.monotonic() - started, 2)
                        logger.warning(
                            "FlexGet %s: no execution_id found in response (HTTP %s, body=%s) "
                            "— check FlexGet URL/auth and ensure task name is correct",
                            task, http_status, str(body)[:300],
                        )
                        # Treat as ok only if 2xx — may be a sync execution
                        return {
                            "task": task,
                            "status": "ok" if http_status < 300 else "error",
                            "http": http_status, "elapsed": elapsed, "result": body,
                        }

        except asyncio.TimeoutError:
            return {"task": task, "status": "timeout", "elapsed": 60.0, "result": {}}
        except Exception as exc:
            logger.error("FlexGet execute %s exception: %s", task, exc)
            return {
                "task": task, "status": "error", "error": str(exc),
                "elapsed": round(time.monotonic() - started, 2), "result": {},
            }

        # Poll for task completion
        deadline = time.monotonic() + _TASK_TIMEOUT
        exec_id_str = str(execution_id)
        poll_urls = [
            f"{self.base_url}/api/tasks/{task_enc}/executions/{exec_id_str}/",
            f"{self.base_url}/api/tasks/executions/{exec_id_str}/",
            # FlexGet v3 alternate: /api/execute/queue/{id}/
            f"{self.base_url}/api/execute/queue/{exec_id_str}/",
        ]
        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as s:
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                for poll_url in poll_urls:
                    try:
                        async with s.get(poll_url) as poll:
                            if poll.status == 404:
                                continue
                            result = await poll.json(content_type=None)
                            state = str(
                                result.get("status")
                                or result.get("state")
                                or result.get("execution_status")
                                or ""
                            ).lower()
                            # FlexGet v3 states: pending, running, succeeded, failed, aborted
                            if state in ("pending", "running", "in_progress", "in progress", ""):
                                break  # still running, try again
                            elapsed = round(time.monotonic() - started, 2)
                            success = state in (
                                "succeeded", "success", "finished",
                                "complete", "done", "completed", "1",
                            )
                            logger.info(
                                "FlexGet task %s finished: state=%s elapsed=%.1fs",
                                task, state, elapsed,
                            )
                            return {
                                "task": task,
                                "status": "ok" if success else "error",
                                "execution_id": execution_id,
                                "state": state,
                                "elapsed": elapsed,
                                "result": result,
                            }
                    except Exception as exc:
                        logger.debug("FlexGet poll %s: %s", poll_url, exc)
                        continue

        return {
            "task": task, "status": "timeout",
            "execution_id": execution_id,
            "elapsed": _TASK_TIMEOUT, "result": {},
        }

    async def execute_tasks(self, tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if tasks is None:
            tasks = await self.list_tasks()
        if not tasks:
            logger.warning("FlexGet execute_tasks: no tasks found")
            return []
        results = []
        for t in tasks:
            results.append(await self.execute_task(t))
        return results


async def run_flexget_tasks(
    tasks: Optional[List[str]] = None,
    triggered_by: str = "manual",
) -> List[Dict[str, Any]]:
    cfg = _cfg()
    if not getattr(cfg, "flexget_enabled", False):
        return []

    client = _client()
    run_tasks = tasks or _configured_tasks()

    await _emit_flexget_webhook("run_started", {
        "triggered_by": triggered_by,
        "tasks": run_tasks or "all",
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        "tasks_total": len(task_results),
        "tasks_ok": ok,
        "tasks_error": errs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

"""
Queue Analytics — backend/services/analytics.py

Provides aggregated metrics about queue performance, error rates, and
throughput.  All queries are read-only and safe to call frequently.

Exposed via GET /api/analytics  (routed in api/routes.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("alldebrid.analytics")


async def get_queue_analytics(window_hours: int = 24) -> dict:
    """
    Return queue performance metrics for the last *window_hours* hours.

    Metrics:
      completed_count        — torrents completed in window
      error_count            — torrents failed in window
      no_peer_count          — no-peer errors in window
      success_rate           — completed / (completed + errors)  [0..1]
      avg_duration_seconds   — mean time from created_at to completed_at
      throughput_gb          — total GB downloaded in window
      total_active           — currently active (downloading/queued)
      total_error            — currently in error state
      top_error_reasons      — [{reason, count}] top 5 error messages
      hourly_completed       — [{hour, count}] completions per hour in window
    """
    try:
        from db.database import get_db
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        async with get_db() as db:
            is_postgres = getattr(db, "backend", "sqlite") == "postgres"
            # Completed in window
            completed_row = await db.fetchone(
                "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes),0) AS total_bytes "
                "FROM torrents WHERE status='completed' AND completed_at >= ?",
                (since,),
            )
            completed_count = int((completed_row or {}).get("c") or 0)
            throughput_bytes = int((completed_row or {}).get("total_bytes") or 0)

            # Errors in window
            error_row = await db.fetchone(
                "SELECT COUNT(*) AS c FROM torrents "
                "WHERE status='error' AND updated_at >= ?",
                (since,),
            )
            error_count = int((error_row or {}).get("c") or 0)

            # No-peer errors in window
            no_peer_row = await db.fetchone(
                "SELECT COUNT(*) AS c FROM torrents "
                "WHERE status='error' AND updated_at >= ? "
                "AND (LOWER(COALESCE(error_message,'')) LIKE '%no peer%' OR provider_status_code=8)",
                (since,),
            )
            no_peer_count = int((no_peer_row or {}).get("c") or 0)

            # Average duration (only for completed torrents with both timestamps)
            duration_row = await db.fetchone(
                "SELECT AVG("
                "  (JULIANDAY(completed_at) - JULIANDAY(created_at)) * 86400"
                ") AS avg_sec "
                "FROM torrents "
                "WHERE status='completed' AND completed_at >= ? "
                "  AND completed_at IS NOT NULL AND created_at IS NOT NULL",
                (since,),
            )
            avg_sec = float((duration_row or {}).get("avg_sec") or 0)

            # Currently active / error
            active_row = await db.fetchone(
                "SELECT "
                "  SUM(CASE WHEN status IN ('downloading','queued','processing','ready','uploading') THEN 1 ELSE 0 END) AS active,"
                "  SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors "
                "FROM torrents"
            )
            total_active = int((active_row or {}).get("active") or 0)
            total_error  = int((active_row or {}).get("errors") or 0)

            # Top error reasons
            error_reasons_rows = await db.fetchall(
                "SELECT COALESCE(error_message,'unknown') AS reason, COUNT(*) AS cnt "
                "FROM torrents "
                "WHERE status='error' AND updated_at >= ? "
                "  AND error_message IS NOT NULL AND error_message != '' "
                "GROUP BY error_message ORDER BY cnt DESC LIMIT 5",
                (since,),
            )
            top_error_reasons = [
                {"reason": str(r["reason"])[:120], "count": int(r["cnt"])}
                for r in (error_reasons_rows or [])
            ]

            # Hourly completed (last 24 h only to keep response small)
            if window_hours <= 48:
                hourly_sql = (
                    "SELECT DATE_TRUNC('hour', completed_at) AS hour, COUNT(*) AS cnt "
                    "FROM torrents "
                    "WHERE status='completed' AND completed_at >= ? "
                    "GROUP BY hour ORDER BY hour ASC"
                ) if is_postgres else (
                    "SELECT STRFTIME('%Y-%m-%dT%H:00:00', completed_at) AS hour, COUNT(*) AS cnt "
                    "FROM torrents "
                    "WHERE status='completed' AND completed_at >= ? "
                    "GROUP BY hour ORDER BY hour ASC"
                )
                hourly_rows = await db.fetchall(
                    hourly_sql,
                    (since,),
                )
                hourly_completed = [
                    {
                        "hour": (
                            r["hour"].isoformat()
                            if hasattr(r.get("hour"), "isoformat")
                            else str(r["hour"])
                        ),
                        "count": int(r["cnt"]),
                    }
                    for r in (hourly_rows or [])
                ]
            else:
                hourly_completed = []

        total_finished = completed_count + error_count
        success_rate = (completed_count / total_finished) if total_finished > 0 else 1.0

        return {
            "window_hours":          window_hours,
            "completed_count":       completed_count,
            "error_count":           error_count,
            "no_peer_count":         no_peer_count,
            "success_rate":          round(success_rate, 4),
            "avg_duration_seconds":  round(avg_sec, 1),
            "throughput_gb":         round(throughput_bytes / (1024 ** 3), 2),
            "total_active":          total_active,
            "total_error":           total_error,
            "top_error_reasons":     top_error_reasons,
            "hourly_completed":      hourly_completed,
        }
    except Exception as exc:
        logger.error("analytics: query failed: %s", exc)
        return {"error": str(exc)}

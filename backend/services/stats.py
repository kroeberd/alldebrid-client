"""
Comprehensive statistics and reporting module.

Captures detailed metrics about all client activities:
- Torrent lifecycle (added, processing, completed, errors)
- Download performance (speed, size, duration)
- Retry rates and failure analysis
- aria2 queue utilization
- Service usage (AllDebrid API calls, Sonarr/Radarr triggers)
- FlexGet run history
- Periodic snapshots for trend analysis
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alldebrid.stats")


def _cfg():
    from core.config import get_settings
    return get_settings()


# ── Snapshot writer (called periodically by scheduler) ───────────────────────

async def take_stats_snapshot() -> None:
    """Capture a point-in-time snapshot of all key metrics."""
    try:
        metrics = await collect_all_metrics(hours=24)
        import json
        from db.database import get_db
        async with get_db() as db:
            await db.execute(
                """INSERT INTO stats_snapshots (snapshot_json, created_at)
                   VALUES (?, CURRENT_TIMESTAMP)""",
                (json.dumps(metrics),),
            )
            await db.commit()
        logger.debug("Stats snapshot taken")
    except Exception as exc:
        logger.warning("Stats snapshot failed: %s", exc)


# ── Core metrics collector ────────────────────────────────────────────────────

async def collect_all_metrics(
    hours: Optional[int] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Collect comprehensive metrics for a given time window.
    hours=None means all-time.
    """
    from db.database import get_db

    # Build time filter
    time_filter = ""
    params_base: tuple = ()
    if since:
        time_filter = "AND created_at >= ?"
        params_base = (since.isoformat(),)
    elif hours:
        time_filter = f"AND created_at >= datetime('now', '-{hours} hours')"

    def _i(v): return int(v or 0)
    def _f(v): return float(v or 0)

    async with get_db() as db:
        # ── Torrent summary ──────────────────────────────────────────────────
        torrent_counts = {
            r["status"]: r["cnt"]
            for r in await db.fetchall(
                f"SELECT status, COUNT(*) as cnt FROM torrents WHERE 1=1 {time_filter} GROUP BY status",
                params_base,
            )
        }
        total_torrents = sum(torrent_counts.values())
        completed      = torrent_counts.get("completed", 0)
        errors         = torrent_counts.get("error", 0)
        terminal       = completed + errors

        # ── Download volume ──────────────────────────────────────────────────
        vol = await db.fetchone(
            f"""SELECT
                COALESCE(SUM(size_bytes), 0)               AS total_bytes,
                COALESCE(AVG(size_bytes), 0)               AS avg_bytes,
                COALESCE(MAX(size_bytes), 0)               AS max_bytes,
                COUNT(*)                                   AS completed_count
               FROM torrents WHERE status='completed' {time_filter}""",
            params_base,
        ) or {}

        # ── Duration metrics ─────────────────────────────────────────────────
        dur = await db.fetchone(
            f"""SELECT
                COALESCE(AVG(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS avg_secs,
                COALESCE(MIN(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS min_secs,
                COALESCE(MAX(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS max_secs
               FROM torrents WHERE completed_at IS NOT NULL AND created_at IS NOT NULL {time_filter}""",
            params_base,
        ) or {}

        # ── File-level stats ─────────────────────────────────────────────────
        file_stats = await db.fetchall(
            f"""SELECT status, COUNT(*) as cnt,
                COALESCE(SUM(size_bytes),0) as total_bytes
               FROM download_files WHERE 1=1 {time_filter} GROUP BY status""",
            params_base,
        )
        files_by_status = {r["status"]: {"count": r["cnt"], "bytes": r["total_bytes"]} for r in file_stats}
        total_files     = sum(r["cnt"] for r in file_stats)
        blocked_files   = await db.fetchone(
            f"SELECT COUNT(*) as c FROM download_files WHERE blocked=1 {time_filter}", params_base
        ) or {}

        # ── Retry stats ──────────────────────────────────────────────────────
        retry = await db.fetchone(
            f"""SELECT
                COALESCE(SUM(retry_count), 0) AS total_retries,
                COALESCE(AVG(retry_count), 0) AS avg_retries,
                COALESCE(MAX(retry_count), 0) AS max_retries,
                COUNT(CASE WHEN retry_count > 0 THEN 1 END) AS files_with_retries
               FROM download_files WHERE 1=1 {time_filter}""",
            params_base,
        ) or {}

        # ── Event counts ─────────────────────────────────────────────────────
        event_counts = {
            r["level"]: r["cnt"]
            for r in await db.fetchall(
                f"SELECT level, COUNT(*) as cnt FROM events WHERE 1=1 {time_filter} GROUP BY level",
                params_base,
            )
        }

        # ── Source distribution ───────────────────────────────────────────────
        sources = await db.fetchall(
            f"SELECT source, COUNT(*) as cnt FROM torrents WHERE 1=1 {time_filter} GROUP BY source",
            params_base,
        )

        # ── Label distribution ────────────────────────────────────────────────
        labels = await db.fetchall(
            f"""SELECT COALESCE(label,'') as label, COUNT(*) as cnt
               FROM torrents WHERE label != '' {time_filter} GROUP BY label""",
            params_base,
        )

        # ── Daily completion trend (last 14 days or within window) ────────────
        trend_days = min(hours // 24, 14) if hours else 14
        daily = await db.fetchall(
            f"""SELECT DATE(completed_at) as date, COUNT(*) as cnt,
                COALESCE(SUM(size_bytes), 0) as bytes
               FROM torrents WHERE completed_at IS NOT NULL
               AND completed_at >= datetime('now', '-{trend_days} days')
               GROUP BY DATE(completed_at) ORDER BY date ASC""",
        )

        # ── FlexGet runs ──────────────────────────────────────────────────────
        flexget = {}
        try:
            fg_rows = await db.fetchall(
                f"""SELECT status, COUNT(*) as cnt,
                    COALESCE(AVG(elapsed_seconds),0) as avg_elapsed
                   FROM flexget_runs WHERE 1=1 {time_filter} GROUP BY status""",
                params_base,
            )
            fg_recent = await db.fetchall(
                """SELECT task_name, status, elapsed_seconds, triggered_by, ran_at
                   FROM flexget_runs ORDER BY ran_at DESC LIMIT 10"""
            )
            flexget = {
                "by_status": {r["status"]: {"count": r["cnt"], "avg_elapsed": round(r["avg_elapsed"], 2)} for r in fg_rows},
                "recent":    fg_recent,
            }
        except Exception:
            pass  # Table may not exist yet

        # ── Assemble result ───────────────────────────────────────────────────
        total_bytes = _i(vol.get("total_bytes"))

        return {
            "window": {
                "hours":  hours,
                "since":  since.isoformat() if since else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "torrents": {
                "total":           total_torrents,
                "by_status":       torrent_counts,
                "completed":       completed,
                "errors":          errors,
                "success_rate_pct": round(completed / terminal * 100, 1) if terminal > 0 else None,
                "sources":         {r["source"]: r["cnt"] for r in sources},
                "labels":          {r["label"]: r["cnt"] for r in labels},
            },
            "downloads": {
                "total_bytes":      total_bytes,
                "total_gb":         round(total_bytes / 1e9, 2),
                "avg_bytes":        _i(vol.get("avg_bytes")),
                "max_bytes":        _i(vol.get("max_bytes")),
                "avg_duration_sec": _i(dur.get("avg_secs")),
                "min_duration_sec": _i(dur.get("min_secs")),
                "max_duration_sec": _i(dur.get("max_secs")),
            },
            "files": {
                "total":           total_files,
                "by_status":       files_by_status,
                "blocked":         _i(blocked_files.get("c")),
                "retry_total":     _i(retry.get("total_retries")),
                "retry_avg":       round(_f(retry.get("avg_retries")), 2),
                "retry_max":       _i(retry.get("max_retries")),
                "files_with_retries": _i(retry.get("files_with_retries")),
            },
            "events": event_counts,
            "daily_trend": daily,
            "flexget": flexget,
        }


async def generate_report(hours: int = 24) -> Dict[str, Any]:
    """Generate a structured report for the given time window."""
    metrics = await collect_all_metrics(hours=hours)
    t = metrics["torrents"]
    d = metrics["downloads"]
    f = metrics["files"]

    def _fmt_bytes(b: int) -> str:
        for u in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {u}"
            b //= 1024
        return f"{b:.1f} TB"

    def _fmt_dur(s: int) -> str:
        if s < 60:   return f"{s}s"
        if s < 3600: return f"{s//60}m {s%60}s"
        return f"{s//3600}h {(s%3600)//60}m"

    return {
        "report": {
            "window_hours":    hours,
            "generated_at":    metrics["window"]["generated_at"],
            "summary": {
                "torrents_processed": t["total"],
                "completed":          t["completed"],
                "errors":             t["errors"],
                "success_rate":       f"{t['success_rate_pct']}%" if t["success_rate_pct"] is not None else "—",
                "total_downloaded":   _fmt_bytes(d["total_bytes"]),
                "avg_torrent_size":   _fmt_bytes(d["avg_bytes"]),
                "avg_duration":       _fmt_dur(d["avg_duration_sec"]),
                "total_files":        f["total"],
                "blocked_files":      f["blocked"],
                "total_retries":      f["retry_total"],
                "error_events":       metrics["events"].get("error", 0),
                "warn_events":        metrics["events"].get("warn", 0),
            },
        },
        "raw": metrics,
    }

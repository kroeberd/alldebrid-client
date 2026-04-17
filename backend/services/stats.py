"""
Comprehensive statistics and reporting module.

SQLite/PostgreSQL compatible — all queries go through _DbConnection._adapt().
Per-table time filters use the correct timestamp column for each table:
  torrents:       created_at
  download_files: updated_at  (has no created_at)
  events:         created_at
  flexget_runs:   ran_at      (has no created_at)
  stats_snapshots: created_at
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("alldebrid.stats")


def _cfg():
    from core.config import get_settings
    return get_settings()


# ── Type-safe numeric helpers (handle int / float / Decimal / None) ───────────

def _i(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _f(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


# ── Snapshot writer ───────────────────────────────────────────────────────────

async def take_stats_snapshot() -> None:
    """Capture a point-in-time snapshot of all key metrics."""
    try:
        metrics = await collect_all_metrics(hours=24)
        from db.database import get_db
        async with get_db() as db:
            await db.execute(
                "INSERT INTO stats_snapshots (snapshot_json, created_at) VALUES (?, CURRENT_TIMESTAMP)",
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
) -> Dict[str, Any]:
    """
    Collect comprehensive metrics for a given time window.
    hours=None → all-time.
    Each table uses its correct timestamp column.
    """
    from db.database import get_db

    # Per-table time filters (each table has a different timestamp column)
    tf_t  = ""   # torrents.created_at
    tf_f  = ""   # download_files.updated_at
    tf_ev = ""   # events.created_at
    tf_fg = ""   # flexget_runs.ran_at
    p: tuple = ()   # query params (only used when since= is given)

    if since:
        iso = since.isoformat()
        tf_t  = "AND created_at >= ?"
        tf_f  = "AND updated_at >= ?"
        tf_ev = "AND created_at >= ?"
        tf_fg = "AND ran_at >= ?"
        p = (iso,)
    elif hours:
        h = int(hours)
        tf_t  = f"AND created_at >= datetime('now', '-{h} hours')"
        tf_f  = f"AND updated_at >= datetime('now', '-{h} hours')"
        tf_ev = f"AND created_at >= datetime('now', '-{h} hours')"
        tf_fg = f"AND ran_at >= datetime('now', '-{h} hours')"
        # no params needed — datetime() is in the SQL string, converted by _adapt()

    trend_days = min(hours // 24, 14) if hours else 14

    async with get_db() as db:

        # ── Torrent summary ──────────────────────────────────────────────────
        torrent_rows = await db.fetchall(
            f"SELECT status, COUNT(*) AS cnt FROM torrents WHERE 1=1 {tf_t} GROUP BY status",
            p,
        )
        torrent_counts = {r["status"]: _i(r["cnt"]) for r in torrent_rows}
        total_torrents = sum(torrent_counts.values())
        completed = torrent_counts.get("completed", 0)
        errors    = torrent_counts.get("error", 0)
        terminal  = completed + errors

        # ── Download volume (torrents.created_at) ────────────────────────────
        vol = await db.fetchone(
            f"""SELECT
                COALESCE(SUM(size_bytes), 0) AS total_bytes,
                COALESCE(AVG(size_bytes), 0) AS avg_bytes,
                COALESCE(MAX(size_bytes), 0) AS max_bytes
               FROM torrents WHERE status = 'completed' {tf_t}""",
            p,
        ) or {}

        # ── Duration (torrents — julianday converted by _adapt for PG) ───────
        dur = await db.fetchone(
            f"""SELECT
                COALESCE(AVG(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS avg_secs,
                COALESCE(MIN(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS min_secs,
                COALESCE(MAX(CAST((julianday(completed_at)-julianday(created_at))*86400 AS INTEGER)), 0) AS max_secs
               FROM torrents
               WHERE completed_at IS NOT NULL AND created_at IS NOT NULL {tf_t}""",
            p,
        ) or {}

        # ── File stats (download_files.updated_at) ───────────────────────────
        file_rows = await db.fetchall(
            f"""SELECT status, COUNT(*) AS cnt,
                COALESCE(SUM(size_bytes), 0) AS total_bytes
               FROM download_files WHERE 1=1 {tf_f} GROUP BY status""",
            p,
        )
        files_by_status = {r["status"]: {"count": _i(r["cnt"]), "bytes": _i(r["total_bytes"])} for r in file_rows}
        total_files = sum(_i(r["cnt"]) for r in file_rows)

        blocked_row = await db.fetchone(
            f"SELECT COUNT(*) AS c FROM download_files WHERE blocked = 1 {tf_f}",
            p,
        ) or {}

        # ── Retry stats (download_files.updated_at) ──────────────────────────
        retry = await db.fetchone(
            f"""SELECT
                COALESCE(SUM(retry_count), 0)                          AS total_retries,
                COALESCE(AVG(retry_count), 0)                          AS avg_retries,
                COALESCE(MAX(retry_count), 0)                          AS max_retries,
                COUNT(CASE WHEN retry_count > 0 THEN 1 END)            AS files_with_retries
               FROM download_files WHERE 1=1 {tf_f}""",
            p,
        ) or {}

        # ── Event counts (events.created_at) ─────────────────────────────────
        event_rows = await db.fetchall(
            f"SELECT level, COUNT(*) AS cnt FROM events WHERE 1=1 {tf_ev} GROUP BY level",
            p,
        )
        event_counts = {r["level"]: _i(r["cnt"]) for r in event_rows}

        # ── Source + label distribution (torrents.created_at) ────────────────
        source_rows = await db.fetchall(
            f"SELECT source, COUNT(*) AS cnt FROM torrents WHERE 1=1 {tf_t} GROUP BY source",
            p,
        )
        label_rows = await db.fetchall(
            f"""SELECT COALESCE(label, '') AS label, COUNT(*) AS cnt
               FROM torrents WHERE label != '' {tf_t} GROUP BY label""",
            p,
        )

        # ── Daily completion trend (no per-window filter needed) ──────────────
        daily = await db.fetchall(
            f"""SELECT DATE(completed_at) AS date, COUNT(*) AS cnt,
                COALESCE(SUM(size_bytes), 0) AS bytes
               FROM torrents
               WHERE completed_at IS NOT NULL
                 AND completed_at >= datetime('now', '-{trend_days} days')
               GROUP BY DATE(completed_at)
               ORDER BY date ASC""",
        )

        # ── FlexGet runs (flexget_runs.ran_at) ───────────────────────────────
        flexget: Dict[str, Any] = {}
        try:
            fg_rows = await db.fetchall(
                f"""SELECT status, COUNT(*) AS cnt,
                    COALESCE(AVG(elapsed_seconds), 0) AS avg_elapsed
                   FROM flexget_runs WHERE 1=1 {tf_fg} GROUP BY status""",
                p,
            )
            fg_recent = await db.fetchall(
                "SELECT task_name, status, elapsed_seconds, triggered_by, ran_at"
                " FROM flexget_runs ORDER BY ran_at DESC LIMIT 10",
            )
            flexget = {
                "by_status": {
                    r["status"]: {"count": _i(r["cnt"]), "avg_elapsed": round(_f(r["avg_elapsed"]), 2)}
                    for r in fg_rows
                },
                "recent": fg_recent,
            }
        except Exception as exc:
            logger.debug("FlexGet stats unavailable: %s", exc)

        # ── Assemble ─────────────────────────────────────────────────────────
        total_bytes = _i(vol.get("total_bytes"))
        return {
            "window": {
                "hours": hours,
                "since": since.isoformat() if since else None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "torrents": {
                "total":            total_torrents,
                "by_status":        torrent_counts,
                "completed":        completed,
                "errors":           errors,
                "success_rate_pct": round(completed / terminal * 100, 1) if terminal > 0 else None,
                "sources":          {r["source"]: _i(r["cnt"]) for r in source_rows},
                "labels":           {r["label"]: _i(r["cnt"]) for r in label_rows},
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
                "total":              total_files,
                "by_status":          files_by_status,
                "blocked":            _i(blocked_row.get("c")),
                "retry_total":        _i(retry.get("total_retries")),
                "retry_avg":          round(_f(retry.get("avg_retries")), 2),
                "retry_max":          _i(retry.get("max_retries")),
                "files_with_retries": _i(retry.get("files_with_retries")),
            },
            "events":      event_counts,
            "daily_trend": daily,
            "flexget":     flexget,
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
        if s < 3600: return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    return {
        "report": {
            "window_hours":  hours,
            "generated_at":  metrics["window"]["generated_at"],
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

"""
Historical Learning - backend/services/learning.py

Derives success metrics from the local DB to score indexers and release groups.
All queries are read-only and safe to call from any context.

Exposed via GET /api/stats/learning
Used by Jackett search to annotate results with a trust score (0.0-1.0).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("alldebrid.learning")

# Score weights
_W_SUCCESS = 0.6
_W_SPEED = 0.2
_WINDOW_DAYS = 90


# Simple in-process TTL cache for learning stats — these change slowly (O(minutes)).
_learning_stats_cache: dict[str, Any] = {}
_learning_stats_cache_ts: float = 0.0
_LEARNING_STATS_TTL_S: float = 60.0  # seconds

async def get_learning_stats() -> dict[str, Any]:
    """
    Return aggregated learning stats:
      indexers - [{indexer, total, completed, errors, success_rate, score}]
      release_groups - [{group, total, completed, success_rate}]
      no_peer_rate - float (fraction of uploads that got no-peer errors)
      top_labels - [{label, count}]

    Results are cached for 60 seconds to avoid hammering the DB on every Jackett search.
    """
    import time as _time
    global _learning_stats_cache, _learning_stats_cache_ts
    now = _time.monotonic()
    if _learning_stats_cache and (now - _learning_stats_cache_ts) < _LEARNING_STATS_TTL_S:
        return _learning_stats_cache

    try:
        from db.database import get_db

        since = datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)

        async with get_db() as db:
            is_postgres = getattr(db, "backend", "sqlite") == "postgres"

            indexer_rows = await db.fetchall(
                """SELECT
                      source,
                      COUNT(*) AS total,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                      SUM(CASE WHEN provider_status_code=8 THEN 1 ELSE 0 END) AS no_peer
                    FROM torrents
                    WHERE created_at >= ?
                      AND source IS NOT NULL AND source != ''
                    GROUP BY source
                    ORDER BY completed DESC""",
                (since,),
            )

            group_expr = (
                "UPPER(SUBSTR(name, STRPOS(name, '-') + 1, 10))"
                if is_postgres
                else "UPPER(SUBSTR(name, INSTR(name, '-') + 1, 10))"
            )
            group_rows = await db.fetchall(
                f"""SELECT
                      {group_expr} AS grp,
                      COUNT(*) AS total,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                    FROM torrents
                    WHERE created_at >= ?
                      AND name LIKE '%-%'
                      AND LENGTH(name) > 5
                    GROUP BY grp
                    HAVING COUNT(*) >= 3
                    ORDER BY completed DESC
                    LIMIT 20""",
                (since,),
            )

            no_peer_row = await db.fetchone(
                """SELECT
                      COUNT(*) AS total,
                      SUM(CASE WHEN provider_status_code=8 THEN 1 ELSE 0 END) AS no_peer
                    FROM torrents
                    WHERE created_at >= ?""",
                (since,),
            )

            label_rows = await db.fetchall(
                """SELECT label, COUNT(*) AS cnt FROM torrents
                   WHERE label IS NOT NULL AND label != ''
                   GROUP BY label ORDER BY cnt DESC LIMIT 10"""
            )

        indexers = []
        for row in indexer_rows or []:
            total = int(row["total"] or 0)
            completed = int(row["completed"] or 0)
            errors = int(row["errors"] or 0)
            if total == 0:
                continue
            success_rate = completed / total
            error_rate = errors / total
            volume_bonus = min(1.0, total / 20) * 0.1
            score = round(
                _W_SUCCESS * success_rate
                + _W_SPEED * (1 - error_rate)
                + volume_bonus,
                3,
            )
            indexers.append(
                {
                    "indexer": str(row["source"]),
                    "total": total,
                    "completed": completed,
                    "errors": errors,
                    "no_peer": int(row["no_peer"] or 0),
                    "success_rate": round(success_rate, 3),
                    "score": min(1.0, score),
                }
            )

        release_groups = []
        for row in group_rows or []:
            grp = (row["grp"] or "").strip()
            total = int(row["total"] or 0)
            comp = int(row["completed"] or 0)
            if total < 3 or not grp or len(grp) > 10:
                continue
            release_groups.append(
                {
                    "group": grp,
                    "total": total,
                    "completed": comp,
                    "success_rate": round(comp / total, 3),
                }
            )

        np_total = int((no_peer_row or {}).get("total", 0) or 0)
        np_count = int((no_peer_row or {}).get("no_peer", 0) or 0)
        no_peer_rate = round(np_count / np_total, 3) if np_total > 0 else 0.0

        top_labels = [
            {"label": str(r["label"]), "count": int(r["cnt"])}
            for r in label_rows or []
        ]

        result_stats = {
            "window_days": _WINDOW_DAYS,
            "indexers": indexers,
            "release_groups": release_groups,
            "no_peer_rate": no_peer_rate,
            "top_labels": top_labels,
        }
        _learning_stats_cache = result_stats
        _learning_stats_cache_ts = now
        return result_stats
    except Exception as exc:
        logger.debug("get_learning_stats: %s", exc)
        return {"error": str(exc), "indexers": [], "release_groups": [], "top_labels": []}


def score_result(result: dict, indexer_scores: dict[str, float]) -> float:
    """
    Return a 0.0-1.0 quality score for a search result dict.

    Factors:
      - Indexer trust score (from historical data)
      - Seeder count (logarithmic)
      - Size plausibility (not too small, not gigantic)
    Useful for sorting Jackett/Prowlarr results by expected reliability.
    """
    import math

    indexer = str(result.get("indexer") or result.get("tracker") or "")
    seeders = int(result.get("seeders") or result.get("Seeders") or 0)
    size = int(result.get("size_bytes") or result.get("Size") or 0)

    idx_score = indexer_scores.get(indexer.lower(), 0.5)
    seeder_score = min(1.0, math.log1p(seeders) / math.log1p(50))

    size_gb = size / (1024**3) if size > 0 else 0
    if size_gb <= 0:
        size_score = 0.3
    elif size_gb < 0.1:
        size_score = 0.2
    elif size_gb <= 50:
        size_score = 1.0
    else:
        size_score = max(0.3, 1.0 - (size_gb - 50) / 100)

    return round(
        0.4 * idx_score + 0.4 * seeder_score + 0.2 * size_score,
        3,
    )

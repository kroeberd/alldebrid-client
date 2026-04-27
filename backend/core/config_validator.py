"""
Config validation and sanitisation — runs at startup.

Checks the loaded AppSettings for common misconfigurations, type errors,
and stale / dangerous values. Logs warnings for every issue found and
returns a sanitised copy of the settings.  Never raises — startup must
not be blocked by a bad config value.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("alldebrid.config")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_valid_url(v: str, require_https: bool = False) -> bool:
    if not v:
        return True  # empty = not configured, not invalid
    pattern = r"^https?://.+" if not require_https else r"^https://.+"
    return bool(re.match(pattern, v.strip()))


def _is_valid_json_array(v: str) -> bool:
    if not v or v.strip() == "[]":
        return True
    try:
        parsed = json.loads(v)
        return isinstance(parsed, list)
    except Exception:
        return False


# ── Validation rules ──────────────────────────────────────────────────────────

def _validate(cfg) -> List[Tuple[str, str, Any, Any]]:
    """
    Returns a list of (field, issue, bad_value, fixed_value) tuples.
    fixed_value=None means the field is logged as a warning but not changed.
    """
    issues: List[Tuple[str, str, Any, Any]] = []

    def warn(field: str, msg: str, bad, fixed=None):
        issues.append((field, msg, bad, fixed))

    # ── AllDebrid ─────────────────────────────────────────────────────────────
    if cfg.alldebrid_api_key and len(cfg.alldebrid_api_key.strip()) < 10:
        warn("alldebrid_api_key", "looks too short to be valid", cfg.alldebrid_api_key)

    # ── URLs ──────────────────────────────────────────────────────────────────
    for field in ("aria2_url", "sonarr_url", "radarr_url", "flexget_url", "jackett_url"):
        val = getattr(cfg, field, "")
        if val and not _is_valid_url(val):
            warn(field, "not a valid HTTP(S) URL", val)

    for field in ("discord_webhook_url", "discord_webhook_added",
                  "flexget_webhook_url", "stats_report_webhook_url",
                  "jackett_webhook_url"):
        val = getattr(cfg, field, "")
        if val and not _is_valid_url(val):
            warn(field, "not a valid HTTP(S) URL — webhook will not fire", val)

    # Discord avatar must be a real HTTP URL, not a data URI or SVG
    avatar = cfg.discord_avatar_url or ""
    if avatar.startswith("data:"):
        warn("discord_avatar_url", "data URI not accepted by Discord — cleared",
             avatar[:60] + "…", "")
    elif avatar.lower().endswith(".svg"):
        warn("discord_avatar_url",
             "SVG not accepted by Discord (use PNG/JPG/WEBP) — cleared",
             avatar, "")

    # ── Numeric ranges ────────────────────────────────────────────────────────
    numeric_bounds = {
        "max_concurrent_downloads":       (1, 20),
        "aria2_max_active_downloads":     (1, 20),
        "aria2_poll_interval_seconds":    (1, 300),
        "aria2_operation_timeout_seconds":(5, 300),
        "aria2_purge_interval_minutes":   (0, 1440),
        "aria2_max_download_result":      (10, 5000),
        "aria2_waiting_window":           (10, 1000),
        "aria2_stopped_window":           (10, 1000),
        "aria2_error_retry_count":        (0, 20),
        "aria2_error_retry_delay_seconds":(1, 3600),
        "aria2_deep_sync_interval_minutes":(0, 1440),
        "poll_interval_seconds":          (5, 3600),
        "watch_interval_seconds":         (1, 3600),
        "alldebrid_rate_limit_per_minute":(0, 600),
        "stuck_download_timeout_hours":   (0, 168),
        "full_sync_interval_minutes":     (1, 1440),
        "backup_keep_days":               (1, 365),
        "backup_interval_hours":          (1, 168),
        "db_backup_keep_days":            (1, 365),
        "flexget_retry_delay_minutes":    (0, 60),
        "flexget_task_timeout_seconds":   (0, 86400),
        "stats_snapshot_interval_minutes":(0, 1440),
        "stats_snapshot_keep_days":       (1, 365),
        "stats_report_interval_hours":    (0, 168),
        "stats_report_window_hours":      (1, 8760),
        "min_file_size_mb":               (0, 100_000),
        "postgres_port":                  (1, 65535),
    }
    for field, (lo, hi) in numeric_bounds.items():
        val = getattr(cfg, field, None)
        if val is None:
            continue
        if not isinstance(val, (int, float)):
            warn(field, f"expected number, got {type(val).__name__}", val, lo)
        elif lo > 0 and val < lo:
            warn(field, f"value {val} below minimum {lo} — clamped", val, lo)
        elif val > hi:
            warn(field, f"value {val} above maximum {hi} — clamped", val, hi)

    # ── JSON fields ───────────────────────────────────────────────────────────
    for field in ("flexget_task_schedules_json",):
        val = getattr(cfg, field, "")
        if not _is_valid_json_array(val):
            warn(field, "invalid JSON array — reset to empty", val, "[]")

    # ── String sanity ─────────────────────────────────────────────────────────
    if cfg.db_type not in ("sqlite", "postgres"):
        warn("db_type", f"unknown value '{cfg.db_type}' — reset to sqlite",
             cfg.db_type, "sqlite")

    if cfg.download_client not in ("aria2",):
        warn("download_client", f"unknown value '{cfg.download_client}' — reset to aria2",
             cfg.download_client, "aria2")

    # ── List fields ───────────────────────────────────────────────────────────
    for field in ("blocked_extensions", "blocked_keywords", "torrent_labels"):
        val = getattr(cfg, field, None)
        if val is not None and not isinstance(val, list):
            warn(field, f"expected list, got {type(val).__name__} — reset to []", val, [])

    return issues


# ── Public API ────────────────────────────────────────────────────────────────

def validate_and_sanitise(cfg) -> Any:
    """
    Validate cfg (AppSettings instance), log all issues, and return a sanitised copy.
    Fields with a fixed_value are corrected; fields without are only warned about.
    """
    from core.config import AppSettings  # avoid circular import

    issues = _validate(cfg)
    if not issues:
        logger.info("Config validation: OK — no issues found")
        return cfg

    fixes: Dict[str, Any] = {}
    for field, msg, bad, fixed in issues:
        if fixed is not None:
            logger.warning("Config [%s]: %s  (was: %r → now: %r)", field, msg, bad, fixed)
            fixes[field] = fixed
        else:
            logger.warning("Config [%s]: %s  (value: %r)", field, msg, bad)

    if not fixes:
        return cfg

    # Apply fixes
    data = cfg.model_dump()
    data.update(fixes)
    sanitised = AppSettings(**{k: v for k, v in data.items() if k in AppSettings.model_fields})
    logger.info("Config validation: %d issue(s) found, %d field(s) corrected",
                len(issues), len(fixes))
    return sanitised

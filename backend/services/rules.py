"""
Rule Engine — backend/services/rules.py

Evaluates a set of configurable rules against a torrent candidate and applies
automatic actions before the download starts.

Rules are stored in config.rules_list as a JSON-serialised list:

    [
      {"if": {"title_contains": "REMUX"},      "then": {"priority": -10}},
      {"if": {"size_gb_gt": 80},               "then": {"pause": true}},
      {"if": {"label_contains": "anime"},      "then": {"download_path": "/anime"}},
      {"if": {"title_matches": ".*4K.*REMUX"}, "then": {"priority": 10}}
    ]

Supported conditions (all optional, combined with AND):
  title_contains  str   — case-insensitive substring match
  title_matches   str   — regex match (re.search)
  size_gb_gt      float — torrent size > N GB
  size_gb_lt      float — torrent size < N GB
  label_contains  str   — label/category substring match
  source_is       str   — exact source match (manual, jackett, watch, qbit…)

Supported actions:
  priority        int   — add to current priority (positive = higher)
  pause           bool  — set status to 'paused' after queuing
  download_path   str   — override download folder for this torrent
  label           str   — set/override label
  block           bool  — skip this torrent entirely (log + skip upload)

Design:
  - Rules are evaluated in order; ALL matching rules are applied.
  - Rule evaluation never raises — errors are logged and the torrent proceeds.
  - Rules are loaded fresh on each evaluation (respects live config updates).
  - No external dependencies.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("alldebrid.rules")

_GB = 1024 ** 3


def _load_rules() -> list[dict]:
    """Load rules from config.  Returns [] on any error."""
    try:
        from core.config import get_settings
        raw = getattr(get_settings(), "rules_list", None) or "[]"
        rules = json.loads(raw) if isinstance(raw, str) else raw
        return rules if isinstance(rules, list) else []
    except Exception as exc:
        logger.debug("rules: could not load rules: %s", exc)
        return []


def _matches(condition: dict, ctx: dict) -> bool:
    """Return True if ALL conditions in *condition* match *ctx*."""
    title = str(ctx.get("name") or ctx.get("title") or "").lower()
    label = str(ctx.get("label") or "").lower()
    source = str(ctx.get("source") or "").lower()
    size_bytes = int(ctx.get("size_bytes") or 0)

    for key, val in condition.items():
        if key == "title_contains":
            if str(val).lower() not in title:
                return False
        elif key == "title_matches":
            try:
                if not re.search(str(val), title, re.IGNORECASE):
                    return False
            except re.error:
                return False
        elif key == "size_gb_gt":
            if size_bytes <= float(val) * _GB:
                return False
        elif key == "size_gb_lt":
            if size_bytes >= float(val) * _GB:
                return False
        elif key == "label_contains":
            if str(val).lower() not in label:
                return False
        elif key == "source_is":
            if str(val).lower() != source:
                return False
        # Unknown condition keys are silently ignored (forward-compatible)
    return True


def evaluate(ctx: dict) -> dict:
    """
    Evaluate all rules against *ctx* and return an aggregated actions dict.

    *ctx* keys (all optional):
      name, title, label, source, size_bytes, priority

    Returned keys (only those affected by matching rules):
      priority      int    — adjusted priority value
      pause         bool
      download_path str
      label         str
      block         bool  — if True, do not upload to AllDebrid
    """
    rules = _load_rules()
    if not rules:
        return {}

    result: dict[str, Any] = {}
    base_priority = int(ctx.get("priority") or 0)

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        condition = rule.get("if") or {}
        actions   = rule.get("then") or {}
        if not isinstance(condition, dict) or not isinstance(actions, dict):
            continue

        try:
            matched = _matches(condition, ctx)
        except Exception as exc:
            logger.debug("rules: condition eval error: %s", exc)
            continue

        if not matched:
            continue

        logger.debug("rules: matched condition=%s → actions=%s for '%s'",
                     condition, actions, sanitize_log_value(ctx.get("name", "?")[:40]))

        # Apply actions
        for action_key, action_val in actions.items():
            if action_key == "priority":
                # Accumulate priority adjustments across all matching rules
                base_priority += int(action_val)
                result["priority"] = base_priority
            elif action_key == "pause" and action_val:
                result["pause"] = True
            elif action_key == "download_path" and action_val:
                result["download_path"] = str(action_val)
            elif action_key == "label" and action_val:
                result["label"] = str(action_val)
            elif action_key == "block" and action_val:
                result["block"] = True

    if result:
        logger.info("rules: applied %d action(s) to '%s': %s",
                    len(result), sanitize_log_value(ctx.get("name", "?")[:40]), result)
    return result

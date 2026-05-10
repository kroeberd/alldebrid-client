"""
Torrent lifecycle state machine.

Centralises all torrent status constants and valid state transitions so that
illegal DB writes are caught early rather than silently corrupting state.

Usage:
    from services.torrent_state import TorrentStatus, assert_transition

    # In _start_download:
    assert_transition(current_status, TorrentStatus.DOWNLOADING)

    # In cleanup_stuck_downloads:
    new = TorrentStatus.READY
    if is_valid_transition(stuck_status, new):
        await db.execute("UPDATE torrents SET status=? ...", (new,))
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class TorrentStatus(str, Enum):
    """All valid values for the ``torrents.status`` column."""
    PENDING     = "pending"
    UPLOADING   = "uploading"
    PROCESSING  = "processing"
    READY       = "ready"
    DOWNLOADING = "downloading"
    QUEUED      = "queued"
    PAUSED      = "paused"
    COMPLETED   = "completed"
    ERROR       = "error"
    DELETED     = "deleted"


# Terminal statuses — no automatic transitions away from these.
TERMINAL: frozenset[str] = frozenset({
    TorrentStatus.COMPLETED,
    TorrentStatus.DELETED,
})

# Statuses that indicate "actively being downloaded by aria2".
ACTIVE_DOWNLOAD: frozenset[str] = frozenset({
    TorrentStatus.DOWNLOADING,
    TorrentStatus.QUEUED,
    TorrentStatus.PAUSED,
})

# Statuses that sync_alldebrid_status should *not* poll.
# (aria2 tracks these; no need to ask AllDebrid.)
POLL_EXCLUDED: frozenset[str] = frozenset({
    TorrentStatus.COMPLETED,
    TorrentStatus.DELETED,
    TorrentStatus.QUEUED,
    TorrentStatus.DOWNLOADING,
    TorrentStatus.PAUSED,
})

# ── Valid transition graph ─────────────────────────────────────────────────────
# Key   = current status
# Value = set of statuses that key may transition TO
#
# Rule: if a transition is NOT listed here it should not happen in production.
# The validator below logs a warning (not exception) to avoid hard crashes in
# edge cases, but future hardening can turn these into errors.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    TorrentStatus.PENDING: frozenset({
        TorrentStatus.UPLOADING,
        TorrentStatus.PROCESSING,
        TorrentStatus.READY,
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.UPLOADING: frozenset({
        TorrentStatus.PROCESSING,
        TorrentStatus.READY,
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.PROCESSING: frozenset({
        TorrentStatus.READY,
        TorrentStatus.UPLOADING,   # re-upload after upload failure
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.READY: frozenset({
        TorrentStatus.DOWNLOADING,
        TorrentStatus.QUEUED,      # direct aria2 dispatch
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.DOWNLOADING: frozenset({
        TorrentStatus.QUEUED,
        TorrentStatus.PAUSED,
        TorrentStatus.READY,       # reset after stuck cleanup
        TorrentStatus.COMPLETED,
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.QUEUED: frozenset({
        TorrentStatus.DOWNLOADING,
        TorrentStatus.PAUSED,
        TorrentStatus.READY,       # reset
        TorrentStatus.COMPLETED,
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.PAUSED: frozenset({
        TorrentStatus.QUEUED,
        TorrentStatus.DOWNLOADING,
        TorrentStatus.READY,
        TorrentStatus.ERROR,
        TorrentStatus.DELETED,
    }),
    TorrentStatus.ERROR: frozenset({
        TorrentStatus.READY,       # manual retry or import_existing_magnets recovery
        TorrentStatus.UPLOADING,   # re-upload retry
        TorrentStatus.DELETED,
    }),
    TorrentStatus.COMPLETED: frozenset({
        TorrentStatus.DELETED,
    }),
    TorrentStatus.DELETED: frozenset(),  # terminal
}


def is_valid_transition(from_status: Optional[str], to_status: str) -> bool:
    """Return True if moving from *from_status* to *to_status* is allowed."""
    if from_status is None:
        # New row — any non-deleted status is fine
        return to_status != TorrentStatus.DELETED
    allowed = VALID_TRANSITIONS.get(str(from_status), frozenset())
    return str(to_status) in allowed


def assert_transition(
    from_status: Optional[str],
    to_status: str,
    torrent_id: int = 0,
    context: str = "",
) -> bool:
    """Validate a status transition and log a warning if invalid.

    Returns True when valid, False when invalid.  Does NOT raise — callers
    decide whether to abort or proceed with an invalid transition.
    """
    if is_valid_transition(from_status, to_status):
        return True

    import logging
    _log = logging.getLogger("alldebrid.state_machine")
    _log.warning(
        "Invalid status transition: %s → %s (torrent_id=%s%s)",
        from_status, to_status, torrent_id,
        f", context={context}" if context else "",
    )
    return False


def is_terminal(status: Optional[str]) -> bool:
    return str(status) in TERMINAL


def is_active_download(status: Optional[str]) -> bool:
    return str(status) in ACTIVE_DOWNLOAD


def should_poll_alldebrid(status: Optional[str]) -> bool:
    return str(status) not in POLL_EXCLUDED

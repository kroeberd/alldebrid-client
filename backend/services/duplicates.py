"""
Duplicate Intelligence Service — backend/services/duplicates.py

Central gatekeeper: called before EVERY AllDebrid upload.
Never performs AllDebrid operations itself — purely read-only against the local DB.

Design principles:
  - All add-flows must call check_before_add() before any AllDebrid contact.
  - Search/preview is always read-only; this service is safe to call during search.
  - Decisions are graduated: allow / warn / skip.
  - Conservative defaults — only hard block when confidence == 1.0 (exact hash).
  - No heavy dependencies; no external HTTP calls.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from db.database import get_db
logger = logging.getLogger("alldebrid.duplicates")

# ── Sentinel status values that indicate an active or completed entry ──────────
_ACTIVE_STATUSES = frozenset({
    "uploading", "processing", "ready", "queued",
    "downloading", "paused", "completed",
})
_ALL_NON_DELETED = frozenset({
    "uploading", "processing", "ready", "queued",
    "downloading", "paused", "completed", "error", "pending",
})


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class DuplicateCandidate:
    """Everything known about a torrent that is about to be added."""
    source:        str            = "manual"
    title:         str            = ""
    magnet:        str            = ""
    torrent_url:   str            = ""
    infohash:      str            = ""       # normalised lowercase hex
    size_bytes:    int            = 0
    indexer:       str            = ""
    category:      str            = ""
    imdb_id:       str            = ""
    tmdb_id:       str            = ""
    season:        Optional[int]  = None
    episode:       Optional[int]  = None
    quality:       str            = ""
    release_group: str            = ""


@dataclass
class DuplicateMatch:
    """A single match found in the local database."""
    torrent_id:  int
    name:        str
    status:      str
    hash:        str
    reason:      str            # human-readable reason key
    confidence:  float          # 0.0 – 1.0


@dataclass
class DuplicateDecision:
    """Result of check_before_add()."""
    is_duplicate: bool
    confidence:   float
    action:       str                     # "allow" | "warn" | "skip"
    reason:       str
    matches:      list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "is_duplicate": self.is_duplicate,
            "confidence":   self.confidence,
            "action":       self.action,
            "reason":       self.reason,
            "matches": [
                {
                    "torrent_id": m.torrent_id,
                    "name":       m.name,
                    "status":     m.status,
                    "hash":       m.hash,
                    "reason":     m.reason,
                    "confidence": m.confidence,
                }
                for m in self.matches
            ],
        }


# ── Normalisation helpers ──────────────────────────────────────────────────────

_BTIH_RE = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[a-zA-Z2-7]{32})", re.I)


def extract_btih(magnet: str) -> str:
    """Return the lowercased BTIH hash from a magnet URI, or ''."""
    m = _BTIH_RE.search(magnet or "")
    if not m:
        return ""
    h = m.group(1)
    # Base32 → hex conversion
    if len(h) == 32:
        try:
            import base64, binascii
            h = binascii.hexlify(base64.b32decode(h.upper())).decode()
        except Exception:
            pass
    return h.lower()


def normalize_title(title: str) -> str:
    """
    Reduce a release title to a canonical lowercase string for fuzzy comparison.

    Example:
        "Movie.Name.2024.1080p.WEB-DL-GROUP" → "movie name 2024"
        "Movie Name (2024) 1080p WEB DL GROUP" → "movie name 2024"
    """
    t = (title or "").lower()
    # Replace common separators
    t = re.sub(r"[._\-]", " ", t)
    # Remove brackets and their contents (e.g. "(2024)" → "2024")
    t = re.sub(r"[\[\](){}]", " ", t)
    # Strip known release tags (quality, codecs, groups, source)
    _TAGS = (
        r"\b(1080[pi]|720[pi]|480[pi]|2160[pi]|4k|uhd|hdr|hdr10|hdr10\+|"
        r"dv|dolby\.?vision|"
        r"web[\. ]?dl|webrip|bluray|blu[\. ]?ray|bdrip|dvdrip|hdtv|"
        r"h\.?264|h\.?265|hevc|x264|x265|avc|"
        r"aac|ac3|dts|truehd|atmos|dd\+?5\.1|"
        r"remux|proper|repack|extended|theatrical|"
        r"multi|dual|english|german|french|spanish|"
        r"nf|amzn|hulu|dsnp|max|atvp|pcok|"
        r"yts|rarbg|ettv|eztv|glhf|flux|cmrg|"
        r"s\d{2}e\d{2,3}|season\s*\d+|episode\s*\d+)\b"
    )
    t = re.sub(_TAGS, "", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_release_tokens(title: str) -> dict:
    """
    Parse season, episode, year, quality and release group from a release title.
    Returns a dict with keys: season, episode, year, quality, release_group.
    """
    t = title or ""
    tokens: dict = {
        "season":        None,
        "episode":       None,
        "year":          None,
        "quality":       "",
        "release_group": "",
    }

    # Season + episode  (S01E02 or 1x02)
    m_se = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", t)
    if m_se:
        tokens["season"]  = int(m_se.group(1))
        tokens["episode"] = int(m_se.group(2))
    else:
        m_xe = re.search(r"\b(\d{1,2})x(\d{1,3})\b", t)
        if m_xe:
            tokens["season"]  = int(m_xe.group(1))
            tokens["episode"] = int(m_xe.group(2))

    # Year (1900–2099)
    m_yr = re.search(r"\b(19|20)\d{2}\b", t)
    if m_yr:
        tokens["year"] = int(m_yr.group())

    # Quality
    for q in ("2160p", "4K", "UHD", "1080p", "1080i", "720p", "720i", "480p", "REMUX"):
        if re.search(re.escape(q), t, re.I):
            tokens["quality"] = q.lower()
            break

    # Release group — last hyphen-separated token if it looks like a group name
    # Pattern: ends with -GROUPNAME (all caps, 2–8 chars)
    m_grp = re.search(r"-([A-Z]{2,8})(?:\[|$|\s)", t + " ")
    if m_grp:
        tokens["release_group"] = m_grp.group(1).lower()

    return tokens


def _size_similar(a: int, b: int) -> bool:
    """True when two byte-counts are within ±2 % or ±100 MB."""
    if a <= 0 or b <= 0:
        return False
    diff = abs(a - b)
    avg  = (a + b) / 2
    return diff / avg < 0.02 or diff < 100 * 1024 * 1024


# ── Core check functions ───────────────────────────────────────────────────────

async def find_hash_duplicate(infohash: str) -> Optional[DuplicateMatch]:
    """
    Stage 1: exact hash lookup against torrents.hash.
    Returns the first matching row (any non-deleted status), or None.
    """
    if not infohash:
        return None
    try:
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT id, name, status, hash FROM torrents WHERE hash = ? LIMIT 1",
                (infohash.lower(),),
            )
        if row and row["status"] in _ALL_NON_DELETED:
            return DuplicateMatch(
                torrent_id=row["id"],
                name=row["name"] or "",
                status=row["status"],
                hash=row["hash"] or "",
                reason="same_infohash",
                confidence=1.0,
            )
    except Exception as exc:
        logger.debug("find_hash_duplicate error: %s", exc)
    return None


async def find_alldebrid_id_duplicate(alldebrid_id: str) -> Optional[DuplicateMatch]:
    """
    Stage 2: check by AllDebrid ID (prevents re-uploading something already on AD).
    """
    if not alldebrid_id:
        return None
    try:
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT id, name, status, hash FROM torrents WHERE alldebrid_id = ? LIMIT 1",
                (str(alldebrid_id),),
            )
        if row and row["status"] in _ALL_NON_DELETED:
            return DuplicateMatch(
                torrent_id=row["id"],
                name=row["name"] or "",
                status=row["status"],
                hash=row["hash"] or "",
                reason="same_alldebrid_id",
                confidence=1.0,
            )
    except Exception as exc:
        logger.debug("find_alldebrid_id_duplicate error: %s", exc)
    return None


async def find_semantic_duplicates(candidate: DuplicateCandidate) -> list[DuplicateMatch]:
    """
    Stages 4–6: fuzzy title + episode + size matching.
    Returns a list of likely duplicates with confidence < 1.0.
    Never blocks on its own — findings are advisory only.
    """
    matches: list[DuplicateMatch] = []
    if not candidate.title:
        return matches

    norm_candidate = normalize_title(candidate.title)
    cand_tokens    = extract_release_tokens(candidate.title)

    # Populate from candidate if not already set
    if candidate.season  is None: candidate.season  = cand_tokens["season"]
    if candidate.episode is None: candidate.episode = cand_tokens["episode"]
    if not candidate.quality:     candidate.quality  = cand_tokens["quality"]

    try:
        async with get_db() as db:
            # Pull active/completed torrents with a name for semantic comparison.
            # Limit to recent 2000 to keep the hot path cheap.
            rows = await db.fetchall(
                """SELECT id, name, status, hash, size_bytes
                   FROM torrents
                   WHERE status IN ('uploading','processing','ready','queued',
                                    'downloading','paused','completed','error')
                     AND name IS NOT NULL AND name != ''
                   ORDER BY id DESC LIMIT 500""",
            )
    except Exception as exc:
        logger.debug("find_semantic_duplicates DB error: %s", exc)
        return matches

    for row in rows:
        norm_row = normalize_title(row["name"])
        if not norm_row:
            continue

        # Title similarity — simple word-overlap ratio
        words_c = set(norm_candidate.split())
        words_r = set(norm_row.split())
        if not words_c or not words_r:
            continue
        overlap = len(words_c & words_r) / max(len(words_c), len(words_r))
        if overlap < 0.60:
            continue

        row_tokens = extract_release_tokens(row["name"])
        confidence = overlap * 0.6  # base

        # Same episode?
        if (candidate.season is not None and row_tokens["season"] == candidate.season
                and candidate.episode is not None and row_tokens["episode"] == candidate.episode):
            confidence += 0.3
        # Same year for films?
        elif (candidate.season is None and row_tokens["year"] is not None
              and cand_tokens["year"] == row_tokens["year"]):
            confidence += 0.2

        # Similar size?
        if _size_similar(candidate.size_bytes, row["size_bytes"] or 0):
            confidence += 0.1

        confidence = min(confidence, 0.99)  # never 1.0 from semantics alone
        if confidence < 0.65:
            continue

        matches.append(DuplicateMatch(
            torrent_id=row["id"],
            name=row["name"],
            status=row["status"],
            hash=row["hash"] or "",
            reason="similar_title",
            confidence=round(confidence, 2),
        ))

    # Sort by confidence descending, limit to top 5
    matches.sort(key=lambda m: -m.confidence)
    return matches[:5]


# ── Main entry point ───────────────────────────────────────────────────────────

async def check_before_add(candidate: DuplicateCandidate) -> DuplicateDecision:
    """
    Central duplicate gate.  Call this before ANY AllDebrid upload.

    Returns a DuplicateDecision with action:
      "allow"  — no duplicate found, proceed normally
      "warn"   — possible duplicate, proceed but surface warning to user
      "skip"   — confident duplicate, do not upload to AllDebrid

    Decision logic:
      - Exact hash match in active/completed status → skip (confidence 1.0)
      - Exact hash match in error/pending status → warn (allow retry)
      - AllDebrid-ID match → skip
      - Semantic match (confidence ≥ 0.85) → warn
      - No match → allow
    """
    # -- Stage 1: exact hash --------------------------------------------------
    hash_val = candidate.infohash or extract_btih(candidate.magnet)
    if hash_val:
        candidate.infohash = hash_val
        match = await find_hash_duplicate(hash_val)
        if match:
            if match.status in _ACTIVE_STATUSES:
                logger.info(
                    "Duplicate skip [hash]: '%s' matches existing torrent #%s (%s)",
                    (candidate.title or hash_val)[:60], match.torrent_id, match.status,
                )
                return DuplicateDecision(
                    is_duplicate=True,
                    confidence=1.0,
                    action="skip",
                    reason="same_infohash",
                    matches=[match],
                )
            # In error/pending state → warn but allow retry
            logger.debug(
                "Possible duplicate [hash/error]: '%s' → torrent #%s (%s)",
                (candidate.title or hash_val)[:60], match.torrent_id, match.status,
            )
            return DuplicateDecision(
                is_duplicate=True,
                confidence=0.95,
                action="warn",
                reason="same_infohash_error_state",
                matches=[match],
            )

    # -- Stage 3: semantic matching -------------------------------------------
    sem_matches = await find_semantic_duplicates(candidate)
    if sem_matches:
        top = sem_matches[0]
        if top.confidence >= 0.85 and top.status in _ACTIVE_STATUSES:
            logger.info(
                "Duplicate warn [semantic %.0f%%]: '%s' similar to #%s '%s'",
                top.confidence * 100, (candidate.title or "?")[:60],
                top.torrent_id, top.name[:50],
            )
            return DuplicateDecision(
                is_duplicate=True,
                confidence=top.confidence,
                action="warn",
                reason="similar_title",
                matches=sem_matches,
            )

    return DuplicateDecision(
        is_duplicate=False,
        confidence=0.0,
        action="allow",
        reason="no_duplicate_found",
        matches=[],
    )

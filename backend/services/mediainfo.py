"""
MediaInfo Integration — backend/services/mediainfo.py

Extracts technical metadata from downloaded media files using ffprobe (bundled)
or pymediainfo (optional). Results are cached per file path to avoid repeated
ffprobe calls on the same file.

Exposed via GET /api/mediainfo?path=<local_path>
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("alldebrid.mediainfo")

# Simple in-process LRU-style cache: path → info dict
_cache: dict[str, dict] = {}
_CACHE_MAX = 500


def _cache_set(path: str, info: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        # Evict oldest entry
        try:
            _cache.pop(next(iter(_cache)))
        except StopIteration:
            pass
    _cache[path] = info


async def get_mediainfo(file_path: str) -> dict:
    """
    Return technical metadata for *file_path*.

    Tries ffprobe first (nearly always available in Docker images that include
    aria2).  Falls back to pymediainfo if ffprobe is not on PATH.

    Returns a dict with keys:
      format       — container format (e.g. "matroska", "mp4")
      duration_s   — duration in seconds (float)
      size_bytes   — file size in bytes
      video        — list of video stream dicts
      audio        — list of audio stream dicts
      subtitles    — list of subtitle stream dicts
      hdr          — bool: any HDR/Dolby Vision video stream detected
      dolby_vision — bool
      codec_video  — first video codec (e.g. "hevc", "h264")
      codec_audio  — first audio codec (e.g. "aac", "eac3", "truehd")
      resolution   — "3840x2160" / "1920x1080" / …
      error        — error message if extraction failed

    Result is cached in-process (up to 500 entries).
    """
    norm = str(Path(file_path).resolve())
    if norm in _cache:
        return _cache[norm]

    result = await _probe_ffprobe(norm)
    if result.get("error") and not result.get("format"):
        result = await _probe_pymediainfo(norm)

    _cache_set(norm, result)
    return result


async def _probe_ffprobe(path: str) -> dict:
    """Run ffprobe and parse its JSON output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        raw = json.loads(stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return {"error": "ffprobe not found"}
    except asyncio.TimeoutError:
        return {"error": "ffprobe timed out"}
    except Exception as exc:
        return {"error": str(exc)}

    return _parse_ffprobe(raw, path)


def _parse_ffprobe(raw: dict, path: str) -> dict:
    fmt    = raw.get("format", {})
    streams = raw.get("streams", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    sub_streams   = [s for s in streams if s.get("codec_type") == "subtitle"]

    def _stream_info(s: dict) -> dict:
        tags   = s.get("tags", {}) or {}
        return {
            "codec":    s.get("codec_name", ""),
            "profile":  s.get("profile", ""),
            "width":    s.get("width"),
            "height":   s.get("height"),
            "language": tags.get("language") or tags.get("LANGUAGE"),
            "title":    tags.get("title"),
            "channels": s.get("channels"),
            "bit_rate": int(s.get("bit_rate") or 0) or None,
        }

    # HDR detection
    hdr = False
    dv  = False
    for s in video_streams:
        ct = (s.get("color_transfer") or "").lower()
        cs = (s.get("color_space") or "").lower()
        cp = (s.get("color_primaries") or "").lower()
        pix = (s.get("pix_fmt") or "").lower()
        if "smpte2084" in ct or "arib-std-b67" in ct or "bt2020" in cs or "bt2020" in cp:
            hdr = True
        tags_str = str(s.get("tags", {})).lower()
        if "dovi" in pix or "dolby" in tags_str or "dvhe" in (s.get("codec_tag_string") or "").lower():
            dv = True; hdr = True

    first_video = video_streams[0] if video_streams else {}
    first_audio = audio_streams[0] if audio_streams else {}

    return {
        "format":       fmt.get("format_name", "").split(",")[0],
        "duration_s":   float(fmt.get("duration") or 0),
        "size_bytes":   int(fmt.get("size") or 0),
        "video":        [_stream_info(s) for s in video_streams],
        "audio":        [_stream_info(s) for s in audio_streams],
        "subtitles":    [_stream_info(s) for s in sub_streams],
        "hdr":          hdr,
        "dolby_vision": dv,
        "codec_video":  first_video.get("codec_name", ""),
        "codec_audio":  first_audio.get("codec_name", ""),
        "resolution":   (
            f"{first_video['width']}x{first_video['height']}"
            if first_video.get("width") and first_video.get("height")
            else ""
        ),
    }


async def _probe_pymediainfo(path: str) -> dict:
    """Fallback to pymediainfo if available."""
    try:
        import pymediainfo  # type: ignore
        info = pymediainfo.MediaInfo.parse(path)
        tracks = info.tracks
        video = next((t for t in tracks if t.track_type == "Video"), None)
        audio = next((t for t in tracks if t.track_type == "Audio"), None)
        gen   = next((t for t in tracks if t.track_type == "General"), None)
        hdr   = False
        dv    = False
        if video:
            hdr_str = str(getattr(video, "hdr_format", "") or "").lower()
            if "hdr" in hdr_str or "bt.2020" in str(getattr(video, "colour_primaries", "") or "").lower():
                hdr = True
            if "dolby vision" in hdr_str:
                dv = True; hdr = True
        return {
            "format":       str(getattr(gen, "format", "") or ""),
            "duration_s":   float(getattr(gen, "duration", 0) or 0) / 1000,
            "size_bytes":   int(getattr(gen, "file_size", 0) or 0),
            "video":        [{"codec": str(getattr(video, "format", "") or ""),
                              "width": getattr(video, "width", None),
                              "height": getattr(video, "height", None)}] if video else [],
            "audio":        [{"codec": str(getattr(audio, "format", "") or ""),
                              "channels": getattr(audio, "channel_s", None)}] if audio else [],
            "subtitles":    [],
            "hdr":          hdr,
            "dolby_vision": dv,
            "codec_video":  str(getattr(video, "format", "") or "") if video else "",
            "codec_audio":  str(getattr(audio, "format", "") or "") if audio else "",
            "resolution": (
                f"{video.width}x{video.height}"
                if video and getattr(video, "width", None)
                else ""
            ),
        }
    except ImportError:
        return {"error": "neither ffprobe nor pymediainfo available"}
    except Exception as exc:
        return {"error": str(exc)}

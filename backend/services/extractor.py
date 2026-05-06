"""
services/extractor.py — Post-download archive extraction service.

Supports: .zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz, .gz,
          .bz2, .xz, .7z, .rar, multi-part .rar (*.part1.rar / *.r00),
          .tar.zst / .tar.lzma (via 7z binary)

Strategy:
  1. Python-native for zip / tar / gz / bz2 / xz (zero extra deps)
  2. System binary `7z` (from p7zip-full) for .7z, .tar.zst, .tar.lzma, and RAR
  3. System binary `unrar-free` as last-resort RAR fallback

After successful extraction the source archive is deleted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("alldebrid.extractor")

# ---------------------------------------------------------------------------
# Archive detection
# ---------------------------------------------------------------------------

# Extension groups in priority order
_ZIP_EXTS  = {".zip"}
_TAR_EXTS  = {".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
               ".tar.xz", ".txz"}
# .tar.zst and .tar.lzma need 7z — keep separate so _extract_sync routes correctly
_TAR_7Z_EXTS = {".tar.zst", ".tzst", ".tar.lzma"}
_GZ_EXTS   = {".gz"}   # single-file gzip (not .tar.gz — that's handled by tar)
_BZ2_EXTS  = {".bz2"}
_XZ_EXTS   = {".xz"}
_7Z_EXTS   = {".7z"}
_RAR_EXTS  = {".rar", ".r00", ".r01", ".r02"}

# Multi-part RAR detection: file.part1.rar, file.part01.rar, file.r00
_MULTIPART_RAR = re.compile(
    r"\.part\d+\.rar$|\.r\d{2}$", re.IGNORECASE
)
# Only the first part should be extracted; subsequent parts are auto-read
_MULTIPART_FIRST = re.compile(
    r"\.part0*1\.rar$|\.r00$", re.IGNORECASE
)


def _suffix(path: Path) -> str:
    """Normalised lower-case compound suffix, e.g. '.tar.gz'."""
    name = path.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lzma",
                ".tgz", ".tbz2", ".txz", ".tzst"):
        if name.endswith(ext):
            return ext
    return path.suffix.lower()


def is_archive(path: Path) -> bool:
    """Return True if *path* looks like an extractable archive."""
    s = _suffix(path)
    if s in _ZIP_EXTS | _TAR_EXTS | _TAR_7Z_EXTS | _GZ_EXTS | _BZ2_EXTS | _XZ_EXTS | _7Z_EXTS:
        return True
    if s in _RAR_EXTS:
        # Skip non-first parts of multi-part RAR sets
        if _MULTIPART_RAR.search(path.name):
            return _MULTIPART_FIRST.search(path.name) is not None
        return True
    return False


def find_archives(folder: Path) -> List[Path]:
    """Walk *folder* recursively and return all extractable archives."""
    archives: List[Path] = []
    try:
        for root, _dirs, files in os.walk(folder):
            for f in sorted(files):
                p = Path(root) / f
                if is_archive(p):
                    archives.append(p)
    except OSError as exc:
        logger.warning("find_archives: cannot walk %s: %s", folder, exc)
    return archives


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _run_tool(cmd: List[str], timeout: int = 3600) -> Tuple[int, str]:
    """Run an external command synchronously (called from asyncio via executor)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, f"Timeout after {timeout}s"
    except FileNotFoundError as exc:
        return -1, str(exc)


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(dest)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(dest, filter="data")


def _extract_gz_single(archive: Path, dest: Path) -> None:
    """Single-file .gz (not .tar.gz)."""
    import gzip
    out_name = archive.stem  # strip .gz
    out_path = dest / out_name
    with gzip.open(archive, "rb") as gz_in, open(out_path, "wb") as f_out:
        shutil.copyfileobj(gz_in, f_out)


def _extract_bz2_single(archive: Path, dest: Path) -> None:
    import bz2
    out_name = archive.stem
    out_path = dest / out_name
    with bz2.open(archive, "rb") as bz_in, open(out_path, "wb") as f_out:
        shutil.copyfileobj(bz_in, f_out)


def _extract_xz_single(archive: Path, dest: Path) -> None:
    import lzma
    out_name = archive.stem
    out_path = dest / out_name
    with lzma.open(archive, "rb") as xz_in, open(out_path, "wb") as f_out:
        shutil.copyfileobj(xz_in, f_out)


def _extract_7z(archive: Path, dest: Path) -> None:
    """Use system `7z` binary (p7zip-full)."""
    for binary in ("7z", "7za", "7zz"):
        if _tool_available(binary):
            rc, out = _run_tool([binary, "x", str(archive), f"-o{dest}", "-y"])
            if rc == 0:
                return
            raise RuntimeError(f"{binary} exited {rc}: {out}")
    raise RuntimeError("No 7z binary found (install p7zip-full in the container)")


def _extract_rar(archive: Path, dest: Path) -> None:
    """Extract RAR archives using 7z (primary) or unrar-free/unrar (fallback).

    7z from p7zip-full handles both RAR3 and RAR5 and is always present in
    the Docker image.  unrar-free (LGPL) is a last-resort fallback for RAR3.
    Note: unrar-free uses '-x' flag, not 'x' like the non-free unrar.
    """
    # Primary: 7z handles RAR3 and RAR5
    for binary in ("7z", "7za", "7zz"):
        if _tool_available(binary):
            rc, out = _run_tool([binary, "x", str(archive), f"-o{dest}", "-y"])
            if rc == 0:
                return
            # 7z present but failed — try unrar tools before giving up
            break

    # Fallback: unrar (non-free, 'x' subcommand)
    if _tool_available("unrar"):
        rc, out = _run_tool(["unrar", "x", "-y", str(archive), str(dest) + "/"])
        if rc == 0:
            return

    # Last resort: unrar-free (LGPL, uses '-x' flag — different from non-free unrar)
    if _tool_available("unrar-free"):
        rc, out = _run_tool(["unrar-free", "-x", str(archive), str(dest) + "/"])
        if rc == 0:
            return

    raise RuntimeError("No RAR extraction tool available (p7zip-full or unrar-free required)")


def _extract_sync(archive: Path, dest: Path) -> None:
    """Synchronous extraction dispatcher."""
    dest.mkdir(parents=True, exist_ok=True)
    s = _suffix(archive)

    if s in _ZIP_EXTS:
        _extract_zip(archive, dest)
    elif s in _TAR_EXTS:
        _extract_tar(archive, dest)
    elif s in _TAR_7Z_EXTS:
        # tar.zst and tar.lzma: Python tarfile cannot decompress these natively;
        # route through 7z which handles them correctly.
        _extract_7z(archive, dest)
    elif s in _GZ_EXTS:
        # Could be a .gz that is NOT a tar — check magic bytes via tarfile
        if tarfile.is_tarfile(str(archive)):
            _extract_tar(archive, dest)
        else:
            _extract_gz_single(archive, dest)
    elif s in _BZ2_EXTS:
        if tarfile.is_tarfile(str(archive)):
            _extract_tar(archive, dest)
        else:
            _extract_bz2_single(archive, dest)
    elif s in _XZ_EXTS:
        if tarfile.is_tarfile(str(archive)):
            _extract_tar(archive, dest)
        else:
            _extract_xz_single(archive, dest)
    elif s in _7Z_EXTS:
        _extract_7z(archive, dest)
    elif s in _RAR_EXTS:
        _extract_rar(archive, dest)
    else:
        raise ValueError(f"Unsupported archive format: {archive.name}")


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

class Extractor:
    """Async extraction service with concurrency limit."""

    def __init__(self, max_concurrent: int = 2) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)

    def update_max_concurrent(self, n: int) -> None:
        self._sem = asyncio.Semaphore(max(1, n))

    async def extract_archive(
        self,
        archive: Path,
        dest: Path,
        *,
        delete_after: bool = True,
    ) -> Tuple[bool, str]:
        """
        Extract *archive* into *dest* (async, respects concurrency semaphore).

        Returns (success, message).
        """
        async with self._sem:
            loop = asyncio.get_running_loop()  # get_event_loop() is deprecated in 3.10+
            try:
                logger.info("Extracting %s → %s", archive, dest)
                await loop.run_in_executor(None, _extract_sync, archive, dest)
                if delete_after and archive.exists():
                    archive.unlink()
                    logger.debug("Deleted archive: %s", archive)
                return True, f"Extracted {archive.name}"
            except Exception as exc:
                msg = f"Extraction failed for {archive.name}: {exc}"
                logger.error(msg)
                return False, msg

    async def extract_folder(
        self,
        folder: Path,
        *,
        delete_after: bool = True,
    ) -> List[Tuple[Path, bool, str]]:
        """
        Find and extract all archives inside *folder*.

        Each archive is extracted into its parent directory (= the torrent
        download folder).  Extractions run concurrently up to *max_concurrent*
        (controlled by the internal semaphore).

        Returns list of (archive_path, success, message).
        """
        loop = asyncio.get_running_loop()
        archives = await loop.run_in_executor(None, find_archives, folder)
        if not archives:
            logger.debug("No archives found in %s", folder)
            return []

        # Create real Tasks so they run concurrently and the semaphore has effect.
        # Previously coroutines were awaited serially, making max_concurrent useless.
        tasks = [
            asyncio.create_task(
                self.extract_archive(archive, archive.parent, delete_after=delete_after)
            )
            for archive in archives
        ]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[Tuple[Path, bool, str]] = []
        for archive, raw in zip(archives, results_raw):
            if isinstance(raw, Exception):
                results.append((archive, False, f"Extraction failed for {archive.name}: {raw}"))
            else:
                ok, msg = raw
                results.append((archive, ok, msg))
        return results


# Module-level singleton — replaced by manager on startup
_extractor: Optional[Extractor] = None


def get_extractor() -> Extractor:
    global _extractor
    if _extractor is None:
        _extractor = Extractor(max_concurrent=2)
    return _extractor


def init_extractor(max_concurrent: int) -> Extractor:
    global _extractor
    _extractor = Extractor(max_concurrent=max_concurrent)
    return _extractor

"""
page_cache.py — helpers to release the Linux kernel page cache for
downloaded files after they are complete.

When aria2 writes a file, the data passes through the kernel page cache.
On systems with large RAM (or slow disks like spinning arrays in Unraid)
this cache can occupy many GB.  The kernel only reclaims it when another
process needs memory, which may never happen on a dedicated server.

posix_fadvise(POSIX_FADV_DONTNEED) tells the kernel that the application
no longer needs the cached pages for a file, allowing immediate reclaim.
This does not affect the file on disk — it only releases the in-RAM copy.

Works on Linux.  On other platforms the call is a no-op.
"""
import ctypes
import ctypes.util
import logging
import os
import sys
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# POSIX_FADV_DONTNEED = 4 on Linux (all architectures)
_POSIX_FADV_DONTNEED = 4
_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL | None:
    global _libc
    if _libc is not None:
        return _libc
    if sys.platform != "linux":
        return None
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        _libc = ctypes.CDLL(name, use_errno=True)
        return _libc
    except OSError:
        return None


def drop_page_cache_for_file(path: Union[str, Path]) -> bool:
    """
    Release the kernel page-cache pages for *path* using
    posix_fadvise(POSIX_FADV_DONTNEED).

    Call this after a file has been fully downloaded and no longer needs
    to be read by aria2. The kernel is free to reclaim the RAM immediately.

    Returns True on success, False if the call is not supported or failed.
    """
    libc = _get_libc()
    if libc is None:
        return False
    path = Path(path)
    if not path.exists():
        return False
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            # offset=0, len=0 means "entire file"
            ret = libc.posix_fadvise(
                ctypes.c_int(fd),
                ctypes.c_long(0),
                ctypes.c_long(0),
                ctypes.c_int(_POSIX_FADV_DONTNEED),
            )
            if ret != 0:
                errno = ctypes.get_errno()
                logger.debug(
                    "posix_fadvise(DONTNEED) failed for %s: errno=%d", path, errno
                )
                return False
            logger.debug("Page cache released for %s", path.name)
            return True
        finally:
            os.close(fd)
    except OSError as exc:
        logger.debug("drop_page_cache_for_file(%s): %s", path, exc)
        return False


def drop_page_cache_for_dir(directory: Union[str, Path]) -> int:
    """
    Recursively call drop_page_cache_for_file() for every file under
    *directory*.  Returns the number of files processed.
    """
    count = 0
    for entry in Path(directory).rglob("*"):
        if entry.is_file():
            drop_page_cache_for_file(entry)
            count += 1
    return count

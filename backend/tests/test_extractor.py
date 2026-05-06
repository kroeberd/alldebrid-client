"""Tests for services/extractor.py — archive detection and extraction logic."""
import asyncio
import gzip
import io
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from services.extractor import (
    Extractor,
    find_archives,
    is_archive,
    _suffix,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_zip(path: Path, content: bytes = b"hello") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("file.txt", content)


def make_tar_gz(path: Path, content: bytes = b"hello") -> None:
    buf = io.BytesIO(content)
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name="file.txt")
        info.size = len(content)
        buf.seek(0)
        tf.addfile(info, buf)


def make_single_gz(path: Path, content: bytes = b"hello") -> None:
    with gzip.open(path, "wb") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# _suffix
# ---------------------------------------------------------------------------

def test_suffix_simple():
    assert _suffix(Path("archive.zip")) == ".zip"
    assert _suffix(Path("archive.tar.gz")) == ".tar.gz"
    assert _suffix(Path("archive.tar.bz2")) == ".tar.bz2"
    assert _suffix(Path("archive.tar.xz")) == ".tar.xz"
    assert _suffix(Path("archive.tgz")) == ".tgz"
    assert _suffix(Path("archive.7z")) == ".7z"
    assert _suffix(Path("archive.rar")) == ".rar"
    assert _suffix(Path("movie.mkv")) == ".mkv"


# ---------------------------------------------------------------------------
# is_archive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("archive.zip", True),
    ("archive.tar.gz", True),
    ("archive.tgz", True),
    ("archive.tar.bz2", True),
    ("archive.tar.xz", True),
    ("archive.7z", True),
    ("archive.rar", True),
    ("archive.r00", True),   # first multi-part
    ("archive.r01", False),  # subsequent part — skip, only r00 is first
    ("archive.part1.rar", True),   # first part
    ("archive.part01.rar", True),  # first part (zero-padded)
    ("archive.part2.rar", False),  # non-first part → skip
    ("archive.part02.rar", False),
    ("movie.mkv", False),
    ("image.jpg", False),
    ("document.pdf", False),
    ("file.txt", False),
])
def test_is_archive(name, expected):
    assert is_archive(Path(name)) is expected


# ---------------------------------------------------------------------------
# find_archives
# ---------------------------------------------------------------------------

def test_find_archives_finds_zip(tmp_path):
    archive = tmp_path / "test.zip"
    make_zip(archive)
    (tmp_path / "not_an_archive.txt").write_text("hello")
    result = find_archives(tmp_path)
    assert archive in result
    assert len(result) == 1


def test_find_archives_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    make_zip(sub / "nested.zip")
    result = find_archives(tmp_path)
    assert len(result) == 1
    assert result[0].name == "nested.zip"


def test_find_archives_empty(tmp_path):
    assert find_archives(tmp_path) == []


def test_find_archives_skips_nonexistent(tmp_path):
    result = find_archives(tmp_path / "does_not_exist")
    assert result == []


# ---------------------------------------------------------------------------
# Extractor.extract_archive — zip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_zip(tmp_path):
    archive = tmp_path / "test.zip"
    make_zip(archive, content=b"zip content")
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=False)
    assert ok, msg
    assert (dest / "file.txt").read_bytes() == b"zip content"


@pytest.mark.asyncio
async def test_extract_zip_deletes_archive(tmp_path):
    archive = tmp_path / "test.zip"
    make_zip(archive)
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=True)
    assert ok, msg
    assert not archive.exists(), "Archive should be deleted after extraction"


@pytest.mark.asyncio
async def test_extract_zip_keeps_archive_when_disabled(tmp_path):
    archive = tmp_path / "test.zip"
    make_zip(archive)
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=False)
    assert ok, msg
    assert archive.exists(), "Archive should be kept when delete_after=False"


# ---------------------------------------------------------------------------
# Extractor.extract_archive — tar.gz
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_tar_gz(tmp_path):
    archive = tmp_path / "test.tar.gz"
    make_tar_gz(archive, content=b"tar content")
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=False)
    assert ok, msg
    assert (dest / "file.txt").read_bytes() == b"tar content"


# ---------------------------------------------------------------------------
# Extractor.extract_archive — single .gz
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_single_gz(tmp_path):
    archive = tmp_path / "data.bin.gz"
    make_single_gz(archive, content=b"gz single content")
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=False)
    assert ok, msg
    assert (dest / "data.bin").read_bytes() == b"gz single content"


# ---------------------------------------------------------------------------
# Extractor.extract_folder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_folder_multiple(tmp_path):
    make_zip(tmp_path / "a.zip", b"aaa")
    make_zip(tmp_path / "b.zip", b"bbb")
    extractor = Extractor(max_concurrent=2)
    results = await extractor.extract_folder(tmp_path, delete_after=True)
    assert len(results) == 2
    assert all(ok for _, ok, _ in results)
    # Both archives deleted
    assert not (tmp_path / "a.zip").exists()
    assert not (tmp_path / "b.zip").exists()
    # Extracted file present (both wrote file.txt into tmp_path)
    assert (tmp_path / "file.txt").exists()


@pytest.mark.asyncio
async def test_extract_folder_no_archives(tmp_path):
    (tmp_path / "movie.mkv").write_bytes(b"video")
    extractor = Extractor(max_concurrent=1)
    results = await extractor.extract_folder(tmp_path, delete_after=True)
    assert results == []


# ---------------------------------------------------------------------------
# Concurrency limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_semaphore_respected(tmp_path):
    """Extractor with max_concurrent=1 still extracts all archives serially."""
    for i in range(3):
        make_zip(tmp_path / f"archive{i}.zip", f"content{i}".encode())
    extractor = Extractor(max_concurrent=1)
    results = await extractor.extract_folder(tmp_path, delete_after=True)
    assert len(results) == 3
    assert all(ok for _, ok, _ in results)


# ---------------------------------------------------------------------------
# update_max_concurrent
# ---------------------------------------------------------------------------

def test_update_max_concurrent():
    extractor = Extractor(max_concurrent=2)
    extractor.update_max_concurrent(5)
    # Semaphore value should reflect new limit
    # asyncio.Semaphore doesn't expose its initial value but we can check _value
    assert extractor._sem._value == 5


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_nonexistent_archive(tmp_path):
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(
        tmp_path / "nope.zip", tmp_path / "out", delete_after=False
    )
    assert not ok
    assert "nope.zip" in msg


@pytest.mark.asyncio
async def test_extract_corrupt_zip(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"this is not a zip file at all")
    dest = tmp_path / "out"
    extractor = Extractor(max_concurrent=1)
    ok, msg = await extractor.extract_archive(archive, dest, delete_after=False)
    assert not ok
    assert "bad.zip" in msg

"""Tests for FilesystemService Implementation"""
import os
import tempfile
from pathlib import Path

import pytest

from core.results import ActionStatus
from system.filesystem.filesystem_service import WindowsFilesystemService


@pytest.fixture
def tmp_root():
    """Create a temporary directory tree for tests."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.txt").write_text("hello", encoding="utf-8")
        (root / "b.txt").write_text("world", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "c.txt").write_text("sub", encoding="utf-8")
        (root / "sub" / "d.py").write_text("print(1)", encoding="utf-8")
        yield root


def test_service_instantiates():
    service = WindowsFilesystemService()
    assert service is not None


def test_read_text(tmp_root):
    service = WindowsFilesystemService()
    text = service.read_text(str(tmp_root / "a.txt"))
    assert text == "hello"


def test_read_text_nonexistent(tmp_root):
    service = WindowsFilesystemService()
    assert service.read_text(str(tmp_root / "nope.txt")) == ""


def test_read_text_directory(tmp_root):
    service = WindowsFilesystemService()
    assert service.read_text(str(tmp_root)) == ""


def test_write_text(tmp_root):
    service = WindowsFilesystemService()
    target = tmp_root / "new.txt"
    res = service.write_text(str(target), "fresh content")
    assert res.status == ActionStatus.EXECUTED
    assert target.read_text(encoding="utf-8") == "fresh content"


def test_write_text_creates_parent_dirs(tmp_root):
    service = WindowsFilesystemService()
    target = tmp_root / "deep" / "nested" / "file.txt"
    res = service.write_text(str(target), "deep")
    assert res.status == ActionStatus.EXECUTED
    assert target.exists()


def test_write_non_string_content(tmp_root):
    service = WindowsFilesystemService()
    res = service.write_text(str(tmp_root / "x"), 12345)
    assert res.status == ActionStatus.FAILED


def test_exists(tmp_root):
    service = WindowsFilesystemService()
    assert service.exists(str(tmp_root / "a.txt"))
    assert not service.exists(str(tmp_root / "nope.txt"))


def test_list_dir(tmp_root):
    service = WindowsFilesystemService()
    listing = service.list_dir(str(tmp_root))
    assert "a.txt" in listing
    assert "sub" in listing


def test_list_dir_nonexistent(tmp_root):
    service = WindowsFilesystemService()
    assert service.list_dir(str(tmp_root / "nope")) == []


def test_search_recursive(tmp_root):
    service = WindowsFilesystemService()
    results = service.search(str(tmp_root), "*.txt", recursive=True)
    assert any(r.endswith("a.txt") for r in results)
    assert any(r.endswith("c.txt") for r in results)


def test_search_non_recursive(tmp_root):
    service = WindowsFilesystemService()
    results = service.search(str(tmp_root), "*.txt", recursive=False)
    names = [Path(r).name for r in results]
    assert "a.txt" in names
    assert "b.txt" in names
    # Should NOT find files in sub/
    assert "c.txt" not in names


def test_sandbox_allowed(tmp_root):
    service = WindowsFilesystemService(
        allowed_roots=[str(tmp_root)],
        enable_sandbox=True,
    )
    assert service.read_text(str(tmp_root / "a.txt")) == "hello"


def test_sandbox_denied(tmp_root):
    outside = Path(tempfile.gettempdir()) / "outside.txt"
    if outside.exists():
        outside.unlink()
    service = WindowsFilesystemService(
        allowed_roots=[str(tmp_root)],
        enable_sandbox=True,
    )
    res = service.write_text(str(outside), "should fail")
    assert res.status == ActionStatus.FAILED
    assert "outside" in res.details.get("reason", "")


def test_read_only_root(tmp_root):
    service = WindowsFilesystemService(
        read_only_roots=[str(tmp_root)],
        enable_sandbox=False,
    )
    res = service.write_text(str(tmp_root / "new.txt"), "blocked")
    assert res.status == ActionStatus.FAILED
    assert "read-only" in res.details.get("reason", "")

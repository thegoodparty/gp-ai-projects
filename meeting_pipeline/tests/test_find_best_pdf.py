"""Tests for find_best_pdf() in extract_and_normalize.py"""
import sys
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from meeting_pipeline.scripts.extract_and_normalize import find_best_pdf
from meeting_pipeline.collection_agent.storage import LocalStorageBackend


def make_storage(base: Path) -> LocalStorageBackend:
    return LocalStorageBackend(base)


def test_find_best_pdf_prefers_packet(tmp_path):
    """Packet should be preferred over agenda when both exist."""
    pdfs_dir = tmp_path / "meeting_pipeline" / "sources" / "test-city-OH" / "data" / "civicclerk" / "pdfs"
    pdfs_dir.mkdir(parents=True)
    (pdfs_dir / "2026-04-07_agenda.pdf").write_bytes(b"x" * 60_000)
    (pdfs_dir / "2026-04-07_packet.pdf").write_bytes(b"x" * 60_000)

    storage = make_storage(tmp_path)
    key, label = find_best_pdf("test-city-OH", "2026-04-07", "civicclerk", storage, "meeting_pipeline/sources")
    assert key is not None
    assert "packet" in key
    assert label == "packet"


def test_find_best_pdf_scans_all_platforms(tmp_path):
    """Should find PDFs in any platform subdir, not just the primary one."""
    pdfs_dir = tmp_path / "meeting_pipeline" / "sources" / "test-city-NC" / "data" / "civicplus" / "pdfs"
    pdfs_dir.mkdir(parents=True)
    (pdfs_dir / "2026-04-07_agenda.pdf").write_bytes(b"x" * 60_000)

    storage = make_storage(tmp_path)
    key, label = find_best_pdf("test-city-NC", "2026-04-07", "granicus", storage, "meeting_pipeline/sources")
    assert key is not None


def test_find_best_pdf_returns_none_when_missing(tmp_path):
    storage = make_storage(tmp_path)
    key, label = find_best_pdf("nonexistent-city-TX", "2026-04-07", "civicplus", storage, "meeting_pipeline/sources")
    assert key is None
    assert label is None


def test_find_best_pdf_ignores_small_files(tmp_path):
    """Files under 5KB (stubs) should be skipped."""
    pdfs_dir = tmp_path / "meeting_pipeline" / "sources" / "stub-city-TX" / "data" / "civicplus" / "pdfs"
    pdfs_dir.mkdir(parents=True)
    (pdfs_dir / "2026-04-07_agenda.pdf").write_bytes(b"x" * 100)  # 100 bytes — stub

    storage = make_storage(tmp_path)
    key, label = find_best_pdf("stub-city-TX", "2026-04-07", "civicplus", storage, "meeting_pipeline/sources")
    assert key is None

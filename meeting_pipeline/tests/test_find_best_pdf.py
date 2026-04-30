"""Tests for find_best_pdf()"""
import json
from pathlib import Path

from meeting_pipeline.stages.extract.normalize import find_best_pdf


class _FilesystemStorageBackend:
    """Minimal filesystem-backed StorageBackend for use in tests only."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()

    def _path(self, key: str) -> Path:
        p = (self.base_dir / key).resolve()
        if not str(p).startswith(str(self.base_dir)):
            raise ValueError(f"Key '{key}' escapes base_dir")
        return p

    def read_json(self, key: str) -> dict:
        with open(self._path(key)) as f:
            return json.load(f)

    def write_json(self, key: str, data: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f, indent=2)

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def get_size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return [str(p.relative_to(self.base_dir)) for p in base.rglob("*") if p.is_file()]

    def append_line(self, key: str, line: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")


def make_storage(base: Path) -> _FilesystemStorageBackend:
    return _FilesystemStorageBackend(base)


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

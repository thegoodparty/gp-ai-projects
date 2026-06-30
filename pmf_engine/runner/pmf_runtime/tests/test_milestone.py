import json

from pmf_engine.runner.pmf_runtime import milestone


def _read_markers(tmp_path):
    log = tmp_path / "logs" / "milestones.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


class TestMilestone:
    def test_appends_record_with_name_and_ts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))
        milestone("gather_inputs")
        markers = _read_markers(tmp_path)
        assert len(markers) == 1
        assert markers[0]["name"] == "gather_inputs"
        assert markers[0]["ts"].endswith("+00:00")

    def test_appends_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))
        milestone("step_one")
        milestone("step_two")
        markers = _read_markers(tmp_path)
        assert [m["name"] for m in markers] == ["step_one", "step_two"]

    def test_creates_logs_dir_if_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PMF_WORKSPACE", str(tmp_path))
        assert not (tmp_path / "logs").exists()
        milestone("first")
        assert (tmp_path / "logs" / "milestones.jsonl").exists()

    def test_never_raises_on_unwritable_workspace(self, tmp_path, monkeypatch):
        # workspace points at a path whose parent is a file, so makedirs fails.
        bad = tmp_path / "afile"
        bad.write_text("x")
        monkeypatch.setenv("PMF_WORKSPACE", str(bad / "nested"))
        milestone("should_not_crash")

    def test_exported_from_package_root(self):
        import pmf_engine.runner.pmf_runtime as rt

        assert hasattr(rt, "milestone")

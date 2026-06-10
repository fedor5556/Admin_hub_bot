"""Tests for runner.py - the kill-matching safety rules and marker protocol."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runner


# ---------------------------------------------------------------------------
# Path matching (the prefix-collision guard)
# ---------------------------------------------------------------------------
def test_norm_dir_has_trailing_separator():
    assert runner.norm_dir("D:\\Bus").endswith(os.sep)
    assert runner.norm_dir("D:\\Bus\\") == runner.norm_dir("D:\\Bus")


def test_belongs_to_matches_own_project():
    proj = runner.norm_dir("D:\\Bus")
    cmd = "D:\\Bus\\venv\\Scripts\\python.exe -u D:\\Bus\\src\\monitor.py"
    assert runner.belongs_to(cmd, "", proj, ["monitor.py"]) == "monitor.py"


def test_belongs_to_rejects_prefix_collision():
    """D:\\Bus must never match a process living in D:\\Bus2."""
    proj = runner.norm_dir("D:\\Bus")
    cmd = "D:\\Bus2\\venv\\Scripts\\python.exe -u D:\\Bus2\\src\\monitor.py"
    assert runner.belongs_to(cmd, "", proj, ["monitor.py"]) is None


def test_belongs_to_requires_script_name_too():
    """Folder match alone is not enough - an unknown script in the project
    folder must not be matched (kill only what the registry declares)."""
    proj = runner.norm_dir("D:\\Bus")
    cmd = "D:\\Bus\\venv\\Scripts\\python.exe D:\\Bus\\scratch_experiment.py"
    assert runner.belongs_to(cmd, "", proj, ["monitor.py"]) is None


def test_belongs_to_matches_via_exe_path():
    """A relative-path launch keeps the project dir in the exe path."""
    proj = runner.norm_dir("D:\\Bus")
    exe = "D:\\Bus\\venv\\Scripts\\python.exe"
    cmd = ".\\venv\\Scripts\\python.exe -u src\\monitor.py"
    assert runner.belongs_to(cmd, exe, proj, ["monitor.py"]) == "monitor.py"


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------
def _registry_entry(path, scripts):
    return {"name": os.path.basename(path), "path": path,
            "processes": [{"script": s} for s in scripts]}


def test_validate_registry_skips_missing_folders(tmp_path):
    raw = {"ghost": _registry_entry(str(tmp_path / "does_not_exist"), ["a.py"])}
    assert runner.validate_registry(raw, str(tmp_path)) == {}


def test_validate_registry_rejects_prefix_collision(tmp_path):
    (tmp_path / "Bus").mkdir()
    (tmp_path / "Bus" / "sub").mkdir()
    raw = {
        "a": _registry_entry(str(tmp_path / "Bus"), ["a.py"]),
        "b": _registry_entry(str(tmp_path / "Bus" / "sub"), ["b.py"]),
    }
    with pytest.raises(ValueError, match="prefix"):
        runner.validate_registry(raw, str(tmp_path))


def test_validate_registry_allows_similar_sibling_names(tmp_path):
    (tmp_path / "Bus").mkdir()
    (tmp_path / "Bus2").mkdir()
    raw = {
        "a": _registry_entry(str(tmp_path / "Bus"), ["a.py"]),
        "b": _registry_entry(str(tmp_path / "Bus2"), ["b.py"]),
    }
    assert len(runner.validate_registry(raw, str(tmp_path))) == 2


def test_validate_registry_rejects_duplicate_basenames(tmp_path):
    (tmp_path / "P").mkdir()
    raw = {"p": _registry_entry(str(tmp_path / "P"), ["x/main.py", "y/main.py"])}
    with pytest.raises(ValueError, match="duplicate"):
        runner.validate_registry(raw, str(tmp_path))


# ---------------------------------------------------------------------------
# Marker protocol
# ---------------------------------------------------------------------------
def test_decide_marker_none_when_absent(tmp_path):
    assert runner.decide_marker(str(tmp_path / "s"), str(tmp_path / "t")) is None


def test_decide_marker_newer_wins(tmp_path):
    start = tmp_path / "runner.start"
    stop = tmp_path / "runner.stop"
    start.write_text("x")
    stop.write_text("x")
    now = time.time()
    os.utime(start, (now - 100, now - 100))
    os.utime(stop, (now, now))
    assert runner.decide_marker(str(start), str(stop)) == "stop"
    os.utime(start, (now + 100, now + 100))
    assert runner.decide_marker(str(start), str(stop)) == "start"


def _make_runner(tmp_path, monkeypatch):
    proj_dir = tmp_path / "Proj"
    (proj_dir / "logs").mkdir(parents=True)
    projects = {
        "proj": {
            "name": "Proj",
            "dir": str(proj_dir),
            "dir_norm": runner.norm_dir(str(proj_dir)),
            "processes": [{"script": "main.py", "base": "main.py", "args": []}],
        }
    }
    monkeypatch.setattr(runner, "STATE_FILE", str(tmp_path / "state.json"))
    calls = {"killed": 0, "spawned": 0}
    monkeypatch.setattr(runner, "kill_project_processes",
                        lambda proj: calls.__setitem__("killed", calls["killed"] + 1))

    class FakeChild:
        pid = 4242
        def poll(self):
            return None

    monkeypatch.setattr(runner, "spawn_process",
                        lambda proj, spec: (calls.__setitem__("spawned", calls["spawned"] + 1),
                                            FakeChild())[1])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return runner.Runner(projects), proj_dir, calls


def test_stop_marker_stops_and_sticks(tmp_path, monkeypatch):
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    (proj_dir / "logs" / "runner.stop").write_text("stop")
    r.process_markers()
    assert r.desired["proj"] is False
    assert calls["killed"] == 1
    assert not (proj_dir / "logs" / "runner.stop").exists()  # consumed
    # Reconcile must NOT resurrect a stopped project
    r.reconcile()
    assert calls["spawned"] == 0


def test_start_marker_kills_fresh_then_spawns(tmp_path, monkeypatch):
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    r.desired["proj"] = False
    (proj_dir / "logs" / "runner.start").write_text("start")
    r.process_markers()
    assert r.desired["proj"] is True
    assert calls["killed"] == 1   # fresh slate before spawning
    assert calls["spawned"] == 1
    assert not (proj_dir / "logs" / "runner.start").exists()


def test_desired_state_survives_restart(tmp_path, monkeypatch):
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    (proj_dir / "logs" / "runner.stop").write_text("stop")
    r.process_markers()
    # A brand-new Runner (same state file) must remember the stop
    r2 = runner.Runner(r.projects)
    assert r2.desired["proj"] is False


# ---------------------------------------------------------------------------
# Real spawn smoke test (hidden child, then kill it)
# ---------------------------------------------------------------------------
def test_spawn_process_hidden(tmp_path):
    proj_dir = tmp_path / "P"
    proj_dir.mkdir()
    (proj_dir / "sleeper.py").write_text("import time; time.sleep(30)")
    proj = {"name": "P", "dir": str(proj_dir), "dir_norm": runner.norm_dir(str(proj_dir)),
            "processes": []}
    child = runner.spawn_process(proj, {"script": "sleeper.py", "base": "sleeper.py", "args": []})
    try:
        time.sleep(1)
        assert child.poll() is None  # alive, hidden
    finally:
        child.kill()

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


def test_start_marker_resumes_without_killing(tmp_path, monkeypatch):
    # A start/resume marker re-enables supervision but must NOT kill or
    # respawn: the Hub launches the process itself, so the runner only adopts
    # the live one (here: nothing running) and leaves any started process be.
    monkeypatch.setattr(runner, "list_python_processes", lambda: [])
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    r.desired["proj"] = False
    (proj_dir / "logs" / "runner.start").write_text("start")
    r.process_markers()
    assert r.desired["proj"] is True            # supervision resumed
    assert calls["killed"] == 0                 # never nukes a freshly-launched process
    assert calls["spawned"] == 0                # process_markers does not spawn; reconcile does
    assert not (proj_dir / "logs" / "runner.start").exists()  # consumed


def test_start_marker_then_reconcile_starts_if_dead(tmp_path, monkeypatch):
    # If nothing is running when supervision resumes, the normal reconcile loop
    # starts the project on its next pass.
    monkeypatch.setattr(runner, "list_python_processes", lambda: [])
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    r.desired["proj"] = False
    (proj_dir / "logs" / "runner.start").write_text("start")
    r.process_markers()
    r.reconcile()
    assert calls["spawned"] == 1


def test_desired_state_survives_restart(tmp_path, monkeypatch):
    r, proj_dir, calls = _make_runner(tmp_path, monkeypatch)
    (proj_dir / "logs" / "runner.stop").write_text("stop")
    r.process_markers()
    # A brand-new Runner (same state file) must remember the stop
    r2 = runner.Runner(r.projects)
    assert r2.desired["proj"] is False


# ---------------------------------------------------------------------------
# Registry hot-reload (new projects picked up without a runner restart)
# ---------------------------------------------------------------------------
def _write_registry(path, keys):
    import json
    path.write_text(json.dumps({
        k: {"name": k.upper(), "path": k.upper(),
            "processes": [{"script": "main.py", "args": []}]}
        for k in keys
    }), encoding="utf-8")


def _make_reloading_runner(tmp_path, monkeypatch, keys=("a",)):
    """A Runner wired to a real registry file in tmp_path, network-safe."""
    for k in keys:
        (tmp_path / k.upper() / "logs").mkdir(parents=True)
    reg = tmp_path / "runner_projects.json"
    _write_registry(reg, keys)
    monkeypatch.setattr(runner, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(runner, "REGISTRY_FILE", str(reg))
    monkeypatch.setattr(runner, "PROJECTS_FILE", str(tmp_path / "projects.json"))
    monkeypatch.setattr(runner, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(runner, "send_alert", lambda text: None)
    monkeypatch.setattr(runner, "list_python_processes", lambda: [])
    projects, pending = runner.load_registry()
    return runner.Runner(projects, pending), reg


def _touch_newer(path):
    """Bump mtime past filesystem timestamp granularity."""
    now = time.time() + 10
    os.utime(path, (now, now))


def test_registry_hot_reload_adds_project(tmp_path, monkeypatch):
    r, reg = _make_reloading_runner(tmp_path, monkeypatch, keys=("a",))
    assert set(r.projects) == {"a"}
    (tmp_path / "B" / "logs").mkdir(parents=True)
    _write_registry(reg, ("a", "b"))
    _touch_newer(reg)
    r.check_registry()
    assert set(r.projects) == {"a", "b"}
    assert r.desired["b"] is True  # autostarts like any boot-registered project


def test_registry_hot_reload_removes_project(tmp_path, monkeypatch):
    r, reg = _make_reloading_runner(tmp_path, monkeypatch, keys=("a", "b"))
    _write_registry(reg, ("a",))
    _touch_newer(reg)
    r.check_registry()
    assert set(r.projects) == {"a"}
    assert "b" not in r.desired


def test_registry_hot_reload_keeps_old_on_broken_edit(tmp_path, monkeypatch):
    r, reg = _make_reloading_runner(tmp_path, monkeypatch, keys=("a",))
    reg.write_text("{ this is not json", encoding="utf-8")
    _touch_newer(reg)
    r.check_registry()
    assert set(r.projects) == {"a"}  # previous registry survives a bad edit


def test_registry_pending_folder_appears(tmp_path, monkeypatch):
    """A project registered before its folder exists (first-time deploy) is
    picked up the moment the Hub's Update bootstraps the folder - with NO
    registry mtime change and no runner restart."""
    r, reg = _make_reloading_runner(tmp_path, monkeypatch, keys=("a",))
    _write_registry(reg, ("a", "b"))  # b's folder does not exist yet
    _touch_newer(reg)
    r.check_registry()
    assert set(r.projects) == {"a"}
    assert set(r.pending_dirs) == {"b"}
    (tmp_path / "B" / "logs").mkdir(parents=True)  # Update bootstraps the folder
    r.check_registry()
    assert set(r.projects) == {"a", "b"}
    assert r.pending_dirs == {}


def test_unsupervised_projects_flags_the_gap(tmp_path, monkeypatch):
    import json
    hub = tmp_path / "projects.json"
    hub.write_text(json.dumps({"a": {}, "b": {}}), encoding="utf-8")
    monkeypatch.setattr(runner, "PROJECTS_FILE", str(hub))
    assert runner.unsupervised_projects({"a"}) == ["b"]
    assert runner.unsupervised_projects({"a", "b"}) == []


def test_unsupervised_projects_tolerates_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECTS_FILE", str(tmp_path / "nope.json"))
    assert runner.unsupervised_projects(set()) == []


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

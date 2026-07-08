"""The two-registry consistency gate.

A worker must be registered in BOTH projects.json (Hub menus / kill-matching)
AND runner_projects.json (supervision). Warm Ship was once added only to
projects.json: the Hub could start it, but nothing restarted it after a crash
or a reboot and the heartbeat never mentioned it. These tests make that
mistake impossible to commit again.
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(BASE_DIR, name), "r", encoding="utf-8-sig") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def test_every_hub_project_is_runner_supervised():
    hub = _load("projects.json")
    supervised = _load("runner_projects.json")
    missing = sorted(k for k in hub if k not in supervised)
    assert missing == [], (
        "Registered in projects.json but NOT in runner_projects.json - these "
        "get no crash-restart and no reboot autostart: {}".format(missing))


def test_registry_paths_agree():
    hub = _load("projects.json")
    supervised = _load("runner_projects.json")
    for key in hub.keys() & supervised.keys():
        assert hub[key]["path"] == supervised[key]["path"], (
            "Project '{}' points at different folders in the two registries: "
            "{} vs {}".format(key, hub[key]["path"], supervised[key]["path"]))


def test_runner_scripts_covered_by_hub_kill_list():
    """Every process the runner spawns must be kill-matchable by the Hub,
    or Stop/Restart/Update leaves orphans behind."""
    hub = _load("projects.json")
    supervised = _load("runner_projects.json")
    for key in hub.keys() & supervised.keys():
        hub_scripts = {s.lower() for s in hub[key].get("scripts", [])}
        for proc in supervised[key].get("processes", []):
            base = os.path.basename(proc["script"]).lower()
            assert base in hub_scripts, (
                "Runner spawns {}/{} but projects.json scripts {} cannot "
                "kill-match it".format(key, proc["script"], sorted(hub_scripts)))

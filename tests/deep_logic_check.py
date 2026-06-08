"""
Deep logic verification for the Admin Hub.
Tests the RISKY parts that the pytest suite does not cover:
  - dynamic path resolution
  - real python-process detection
  - kill TARGETING + safety guards (never kill self / admin_bot)
  - the generic-script-name ("main.py") over-match risk
  - read-only helpers (git commit, db size, log tail) against real folders

Run:  venv\Scripts\python.exe tests\deep_logic_check.py
It does NOT kill any real production process; taskkill is intercepted.
"""
import os
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import admin_bot

PASS, FAIL = "[PASS]", "[FAIL]"
results = []

def check(name, cond, detail=""):
    tag = PASS if cond else FAIL
    results.append(cond)
    print(f"{tag} {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. Dynamic path resolution (the "zero-config" claim)
# ---------------------------------------------------------------------------
print("\n=== 1. Path resolution ===")
admin_bot.load_projects()
projs = admin_bot.projects
check("projects.json loaded 2 projects", len(projs) == 2, str(list(projs.keys())))
for key, p in projs.items():
    abs_ok = os.path.isabs(p["path"])
    check(f"  '{key}' path is absolute", abs_ok, p["path"])


# ---------------------------------------------------------------------------
# 2. Real python-process detection
# ---------------------------------------------------------------------------
print("\n=== 2. Process detection (real) ===")
tmpdir = tempfile.mkdtemp()
dummy = os.path.join(tmpdir, "_dummy_monitor.py")
with open(dummy, "w") as f:
    f.write("import time\ntime.sleep(45)\n")

proc = subprocess.Popen([sys.executable, dummy])
time.sleep(2.0)  # let WMI see it
try:
    found = admin_bot.get_running_python_processes()
    mine = [x for x in found if str(proc.pid) == str(x["pid"])]
    check("dummy process detected by WMI", len(mine) == 1, f"pid={proc.pid}, total procs seen={len(found)}")
    if mine:
        check("  command line captured", "_dummy_monitor.py" in mine[0]["cmd"].lower(), mine[0]["cmd"][:80])
finally:
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# 3. Kill TARGETING + safety guards (synthetic, deterministic, NO real kills)
# ---------------------------------------------------------------------------
print("\n=== 3. Kill targeting + safety guards ===")
killed_pids = []
real_run = subprocess.run

def fake_run(args, *a, **k):
    # Intercept taskkill so nothing real dies; record the PID it targeted.
    if isinstance(args, (list, tuple)) and len(args) and str(args[0]).lower() == "taskkill":
        killed_pids.append(int(args[args.index("/PID") + 1]))
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    return real_run(args, *a, **k)

my_pid = os.getpid()
synthetic = [
    {"pid": my_pid,  "exe": r"C:\Py\python.exe", "cmd": r"python admin_bot.py"},          # self + admin -> skip
    {"pid": 900001,  "exe": r"C:\Projects\Admin_hub\venv\Scripts\python.exe", "cmd": r"...\python.exe admin_bot.py"},  # admin -> skip
    {"pid": 900002,  "exe": r"C:\Projects\Cyp_Bus_Bot\venv\Scripts\python.exe", "cmd": r"C:\Projects\Cyp_Bus_Bot\venv\Scripts\python.exe -u src\monitor.py"},  # bus abs -> KILL
    {"pid": 900003,  "exe": r"C:\Projects\Cyp_Bus_Bot\venv\Scripts\python.exe", "cmd": r".\venv\Scripts\python.exe -u src\predict_eta.py"},  # bus RELATIVE cmd -> KILL via exe
    {"pid": 900004,  "exe": r"C:\SomeOtherApp\venv\Scripts\python.exe", "cmd": r".\venv\Scripts\python.exe app.py"},  # unrelated -> skip
]

admin_bot.subprocess.run = fake_run
admin_bot.get_running_python_processes = lambda: synthetic
try:
    bus = {"path": r"C:\Projects\Cyp_Bus_Bot", "scripts": ["monitor.py", "predict_eta.py"]}
    killed_pids.clear()
    admin_bot.kill_project_processes("bus", bus)
    check("kills both bus scripts (incl. RELATIVE-path launch)", set(killed_pids) == {900002, 900003}, f"targeted={sorted(killed_pids)}")
    check("  matches relative-cmd process via ExecutablePath", 900003 in killed_pids)
    check("  never kills self", my_pid not in killed_pids)
    check("  never kills admin_bot", 900001 not in killed_pids)
    check("  never kills unrelated app", 900004 not in killed_pids)
finally:
    admin_bot.subprocess.run = real_run


# ---------------------------------------------------------------------------
# 4. The generic 'main.py' over-match RISK (transcriber scripts=["main.py"])
# ---------------------------------------------------------------------------
print("\n=== 4. Generic 'main.py' collision risk ===")
risk_procs = [
    {"pid": 800001, "exe": r"C:\Projects\Constan_transcriber_telegram_bot\venv\Scripts\python.exe", "cmd": r".\venv\Scripts\python.exe -u src\main.py"},  # intended (relative cmd)
    {"pid": 800002, "exe": r"C:\Users\Admin\SomeUnrelatedProject\venv\Scripts\python.exe", "cmd": r"python main.py"},                                       # COLLATERAL
]
admin_bot.subprocess.run = fake_run
admin_bot.get_running_python_processes = lambda: risk_procs
try:
    killed_pids.clear()
    admin_bot.kill_project_processes(
        "transcriber",
        {"path": r"C:\Projects\Constan_transcriber_telegram_bot", "scripts": ["main.py"]},
    )
    intended_only = killed_pids == [800001]
    check("FIX: only the transcriber's own main.py is killed", intended_only,
          f"targeted={killed_pids} (unrelated 800002 must NOT appear)")
    check("  unrelated main.py is now protected", 800002 not in killed_pids)
finally:
    admin_bot.subprocess.run = real_run


# ---------------------------------------------------------------------------
# 5. Read-only helpers against real folders
# ---------------------------------------------------------------------------
print("\n=== 5. Read-only helpers (real folders) ===")
admin_bot.load_projects()  # restore real get_running_python_processes? no - it was lambda'd
for key, p in admin_bot.projects.items():
    path = p["path"]
    exists = os.path.isdir(path)
    commit = admin_bot.get_last_commit(path) if exists else "N/A (folder missing locally)"
    disk = admin_bot.get_disk_free(path) if exists else "N/A"
    db = admin_bot.get_db_total_size(path) if exists else "N/A"
    print(f"  [{key}] exists={exists}")
    print(f"        commit: {commit}")
    print(f"        disk:   {disk}")
    print(f"        db:     {db}")
    bat = os.path.join(path, "COMPLETE_LAUNCH.bat")
    check(f"  '{key}' COMPLETE_LAUNCH.bat present", os.path.isfile(bat) if exists else True,
          bat if exists else "skipped (folder not local)")


# ---------------------------------------------------------------------------
print("\n=== SUMMARY ===")
total = len(results)
passed = sum(results)
print(f"{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)

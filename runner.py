"""
runner.py - hidden process supervisor for the worker projects on the server.

Runs as ONE hidden background process and keeps every registered project's
processes alive without a console window each. Projects are defined in
runner_projects.json (paths relative to this folder, same sibling-folder
convention as projects.json). Every managed process writes its own rotating
UTF-8 log (see TELEGRAM_BOT_NOTE.md), so hiding the consoles loses nothing.

Control protocol (file-based, no open ports - matching the server's
no-inbound-network rule). Markers live in each project's logs/ folder
because logs/ is gitignored everywhere:

    <project>/logs/runner.start  ->  kill the project's processes, then start
                                     them all hidden ("restart fresh")
    <project>/logs/runner.stop   ->  kill the project's processes and leave
                                     them stopped (no auto-restart)

The markers are written by each project's COMPLETE_LAUNCH.bat / STOP_ALL.bat
and by the Admin Hub's /stop. The runner consumes (deletes) them. If both
exist, the newer one wins.

Restart policy: a managed process that dies WITHOUT a stop marker is treated
as crashed and restarted after RESTART_DELAY seconds, at most MAX_RESTARTS
times per RESTART_WINDOW; beyond that it goes into a cooldown so a broken bot
cannot crash-loop forever. Desired state survives runner restarts via
runner_state.json, and already-running processes are adopted, never duplicated.

Kill safety: the runner only ever terminates python processes whose command
line or exe path contains BOTH the project's folder path WITH a trailing
backslash (so a folder named Bus can never match a sibling named Bus2) AND
one of that project's registered script names. It never touches itself, the
Admin Hub, or anything outside the registry.
"""

import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(BASE_DIR, "runner_projects.json")
STATE_FILE = os.path.join(BASE_DIR, "runner_state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

POLL_SECONDS = 5            # reconcile loop interval
ADOPT_SCAN_SECONDS = 60     # how often to rescan the OS for adopted processes
RESTART_DELAY = 15          # seconds a process must stay dead before restart
MAX_RESTARTS = 5            # per process, per RESTART_WINDOW
RESTART_WINDOW = 1800
COOLDOWN_SECONDS = 1800     # crash-loop cooldown
SINGLE_INSTANCE_PORT = 47631
CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# Logging (sole writer of logs/runner.log - see TELEGRAM_BOT_NOTE.md)
# ---------------------------------------------------------------------------
os.makedirs(LOG_DIR, exist_ok=True)
_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "runner.log"), maxBytes=2_000_000,
        backupCount=3, encoding="utf-8", delay=True))
except OSError:
    pass
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=_handlers)
logger = logging.getLogger("Runner")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_runner.py)
# ---------------------------------------------------------------------------
def norm_dir(path):
    """Absolute, lowercase folder path WITH trailing backslash.

    The trailing separator is the prefix-collision guard: 'd:\\bus\\' is not a
    substring of 'd:\\bus2\\...', while plain 'd:\\bus' would be.
    """
    return os.path.abspath(path).lower().rstrip("\\/") + os.sep


def belongs_to(cmd, exe, proj_dir_norm, script_basenames):
    """Return the matching script basename if this process belongs to the
    project, else None. Requires BOTH the project folder and a script name."""
    cmd = (cmd or "").lower()
    exe = (exe or "").lower()
    if proj_dir_norm not in cmd and proj_dir_norm not in exe:
        return None
    for name in script_basenames:
        if name.lower() in cmd:
            return name
    return None


def validate_registry(raw, base_dir):
    """Resolve paths and reject unsafe configurations.

    Returns {key: {"name", "dir" (abs), "dir_norm", "processes": [
        {"script" (relative), "base" (file name), "args": [...]}, ...]}}.
    Projects whose folder does not exist are skipped with a warning (the
    registry ships server folder names; on a dev machine they may be absent).
    Prefix-colliding project paths are a hard error: kill-matching could
    cross projects.
    """
    projects = {}
    for key, spec in raw.items():
        if key.startswith("_"):
            continue  # comment entries
        proj_dir = os.path.abspath(os.path.join(base_dir, spec["path"]))
        if not os.path.isdir(proj_dir):
            logger.warning("Project '%s': folder %s does not exist - skipped",
                           key, proj_dir)
            continue
        processes = []
        basenames = set()
        for proc in spec.get("processes", []):
            base = os.path.basename(proc["script"]).lower()
            if base in basenames:
                raise ValueError(
                    "Project '{}': duplicate script file name '{}' - kill and "
                    "adopt matching needs unique names within a project".format(key, base))
            basenames.add(base)
            processes.append({
                "script": proc["script"],
                "base": base,
                "args": list(proc.get("args", [])),
            })
        projects[key] = {
            "name": spec.get("name", key),
            "dir": proj_dir,
            "dir_norm": norm_dir(proj_dir),
            "processes": processes,
        }
    # Prefix-collision check across all registered paths
    norms = sorted(p["dir_norm"] for p in projects.values())
    for a, b in zip(norms, norms[1:]):
        if b.startswith(a) and a != b:
            raise ValueError(
                "Project paths collide: {} is a prefix of {} - rename one "
                "folder, kill-matching cannot tell them apart".format(a, b))
    return projects


def decide_marker(start_path, stop_path):
    """Return 'start', 'stop' or None from the marker files; newer one wins."""
    start_t = os.path.getmtime(start_path) if os.path.exists(start_path) else None
    stop_t = os.path.getmtime(stop_path) if os.path.exists(stop_path) else None
    if start_t is None and stop_t is None:
        return None
    if start_t is None:
        return "stop"
    if stop_t is None:
        return "start"
    return "start" if start_t >= stop_t else "stop"


# ---------------------------------------------------------------------------
# OS interaction
# ---------------------------------------------------------------------------
def list_python_processes():
    """[{pid, exe, cmd}] for every python process (same query the Hub uses)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" "
          "| ForEach-Object { $_.ProcessId.ToString() + '|||' + "
          "[string]$_.ExecutablePath + '|||' + [string]$_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        logger.error("Process scan failed: %s", exc)
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split("|||", 2)
        if len(parts) == 3 and parts[0].strip().isdigit():
            procs.append({"pid": int(parts[0]), "exe": parts[1].strip(),
                          "cmd": parts[2].strip()})
    return procs


def python_for(proj_dir):
    """The project's venv python, falling back to this interpreter."""
    venv_py = os.path.join(proj_dir, "venv", "Scripts", "python.exe")
    return venv_py if os.path.isfile(venv_py) else sys.executable


def spawn_process(proj, proc_spec):
    """Start one project process hidden. Output goes to DEVNULL - every bot
    writes its own rotating log file from inside Python."""
    script_abs = os.path.join(proj["dir"], proc_spec["script"])
    cmdline = [python_for(proj["dir"]), "-u", script_abs] + proc_spec["args"]
    return subprocess.Popen(
        cmdline, cwd=proj["dir"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)


def kill_project_processes(proj):
    """Terminate every python process belonging to this project (and only
    this project). Mirrors the Admin Hub's double-match rule."""
    basenames = [p["base"] for p in proj["processes"]]
    killed = []
    for proc in list_python_processes():
        if proc["pid"] == os.getpid():
            continue
        low = proc["cmd"].lower()
        if "admin_bot" in low or "runner.py" in low:
            continue
        if belongs_to(proc["cmd"], proc["exe"], proj["dir_norm"], basenames):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc["pid"])],
                           capture_output=True, timeout=10)
            killed.append(proc["pid"])
    if killed:
        logger.info("Killed PIDs %s for %s", killed, proj["name"])
    return killed


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, projects):
        self.projects = projects
        self.children = {}        # (key, base) -> Popen
        self.adopted = {}         # (key, base) -> pid (seen in last scan)
        self.death_time = {}      # (key, base) -> monotonic time of death
        self.restarts = {}        # (key, base) -> [monotonic timestamps]
        self.cooldown_until = {}  # (key, base) -> monotonic time
        self.last_scan = 0.0
        self.desired = self._load_state()

    def _load_state(self):
        desired = {key: True for key in self.projects}  # autostart by default
        try:
            with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                for key, want in json.load(f).items():
                    if key in desired:
                        desired[key] = bool(want)
        except (OSError, ValueError):
            pass
        return desired

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.desired, f, indent=2)
        except OSError as exc:
            logger.warning("Could not persist state: %s", exc)

    # -- markers ------------------------------------------------------------
    def process_markers(self):
        for key, proj in self.projects.items():
            marker_dir = os.path.join(proj["dir"], "logs")
            start_m = os.path.join(marker_dir, "runner.start")
            stop_m = os.path.join(marker_dir, "runner.stop")
            action = decide_marker(start_m, stop_m)
            if action is None:
                continue
            for m in (start_m, stop_m):
                try:
                    if os.path.exists(m):
                        os.remove(m)
                except OSError:
                    pass
            logger.info("Marker '%s' for %s", action, proj["name"])
            if action == "stop":
                self.desired[key] = False
                self._save_state()
                kill_project_processes(proj)
                self._forget(key)
            else:  # start = restart fresh with a clean slate
                kill_project_processes(proj)
                self._forget(key)
                self.desired[key] = True
                self._save_state()
                time.sleep(1)
                for spec in proj["processes"]:
                    self._spawn(key, proj, spec)

    def _forget(self, key):
        for d in (self.children, self.adopted, self.death_time,
                  self.restarts, self.cooldown_until):
            for k in [k for k in d if k[0] == key]:
                del d[k]

    # -- spawning & adoption --------------------------------------------------
    def _spawn(self, key, proj, spec):
        try:
            child = spawn_process(proj, spec)
            self.children[(key, spec["base"])] = child
            self.death_time.pop((key, spec["base"]), None)
            logger.info("Started %s / %s (PID %d)", proj["name"], spec["base"], child.pid)
        except Exception as exc:
            logger.error("Failed to start %s / %s: %s", proj["name"], spec["base"], exc)

    def adopt_scan(self):
        """Find managed processes we didn't spawn (legacy launches, pre-restart
        survivors) so we never start duplicates."""
        self.adopted.clear()
        procs = list_python_processes()
        for key, proj in self.projects.items():
            basenames = [p["base"] for p in proj["processes"]]
            for proc in procs:
                base = belongs_to(proc["cmd"], proc["exe"], proj["dir_norm"], basenames)
                if base and (key, base) not in self.children:
                    self.adopted[(key, base)] = proc["pid"]
        self.last_scan = time.monotonic()

    # -- reconcile ------------------------------------------------------------
    def _alive(self, key, base):
        child = self.children.get((key, base))
        if child is not None:
            if child.poll() is None:
                return True
            logger.warning("%s/%s exited with code %s", key, base, child.returncode)
            del self.children[(key, base)]
            self.death_time.setdefault((key, base), time.monotonic())
        return (key, base) in self.adopted

    def _may_restart(self, key, base):
        now = time.monotonic()
        if now < self.cooldown_until.get((key, base), 0):
            return False
        died = self.death_time.get((key, base))
        if died is not None and now - died < RESTART_DELAY:
            return False
        history = [t for t in self.restarts.get((key, base), []) if now - t < RESTART_WINDOW]
        if len(history) >= MAX_RESTARTS:
            self.cooldown_until[(key, base)] = now + COOLDOWN_SECONDS
            self.restarts[(key, base)] = []
            logger.error("%s/%s crash-looping (%d restarts in %d min) - cooling "
                         "down for %d min", key, base, MAX_RESTARTS,
                         RESTART_WINDOW // 60, COOLDOWN_SECONDS // 60)
            return False
        history.append(now)
        self.restarts[(key, base)] = history
        return True

    def reconcile(self):
        self.process_markers()
        if time.monotonic() - self.last_scan > ADOPT_SCAN_SECONDS:
            self.adopt_scan()
        for key, proj in self.projects.items():
            if not self.desired.get(key):
                continue
            for spec in proj["processes"]:
                base = spec["base"]
                if self._alive(key, base):
                    continue
                self.death_time.setdefault((key, base), time.monotonic() - RESTART_DELAY)
                if self._may_restart(key, base):
                    logger.info("Restarting %s / %s", proj["name"], base)
                    self._spawn(key, proj, spec)


def main():
    # Single instance guard: one supervisor per machine.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        logger.info("Another runner instance is already alive - exiting.")
        return 0

    try:
        # utf-8-sig: humans edit this file, and Notepad/PowerShell write a BOM
        with open(REGISTRY_FILE, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        projects = validate_registry(raw, BASE_DIR)
    except Exception as exc:
        logger.critical("Cannot load registry %s: %s", REGISTRY_FILE, exc)
        return 1

    logger.info("Runner starting - managing: %s",
                ", ".join(p["name"] for p in projects.values()) or "(nothing)")
    runner = Runner(projects)
    runner.adopt_scan()

    once = "--once" in sys.argv
    while True:
        try:
            runner.reconcile()
        except KeyboardInterrupt:
            logger.info("Runner stopped by user (managed processes keep running).")
            return 0
        except Exception as exc:
            logger.error("Reconcile error (runner keeps going): %s", exc)
        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())

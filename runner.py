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

    <project>/logs/runner.start  ->  resume supervision (mark the project as
                                     "should be running"). Does NOT kill: the
                                     Hub launches the process itself, so the
                                     runner just adopts the live one and never
                                     spawns a duplicate. If nothing is running
                                     yet, the normal reconcile loop starts it.
    <project>/logs/runner.stop   ->  kill the project's processes and leave
                                     them stopped (no auto-restart)

The markers are written by the Admin Hub: /stop writes runner.stop, while
Start / Restart / Update / Rollback write runner.start so an intentionally
stopped project starts being supervised again. The runner consumes (deletes)
them. If both exist, the newer one wins.

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
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_FILE = os.path.join(BASE_DIR, "runner_projects.json")
PROJECTS_FILE = os.path.join(BASE_DIR, "projects.json")
STATE_FILE = os.path.join(BASE_DIR, "runner_state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
ROLLBACK_FLAG = os.path.join(LOG_DIR, "update_rollback.flag")

POLL_SECONDS = 5            # reconcile loop interval
ADOPT_SCAN_SECONDS = 60     # how often to rescan the OS for adopted processes
RESTART_DELAY = 15          # seconds a process must stay dead before restart
MAX_RESTARTS = 5            # per process, per RESTART_WINDOW
RESTART_WINDOW = 1800
COOLDOWN_SECONDS = 1800     # crash-loop cooldown
SINGLE_INSTANCE_PORT = 47631
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

# Admin Hub supervision: the Hub watches everything else; the runner is the
# safety net for the Hub itself.
HUB_SCRIPT = "admin_bot.py"
HUB_KEY = ("_hub", HUB_SCRIPT)
HUB_STARTUP_GRACE = 120     # let START_SERVER / UPDATE_ADMIN bring the Hub up
HEARTBEAT_SECONDS = 24 * 3600

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
# Telegram alerts (crash notifications + heartbeat)
# ---------------------------------------------------------------------------
def read_env(path):
    """Minimal KEY=VALUE .env parser - no dependencies, BOM-tolerant."""
    vals = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    vals[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


_env = read_env(os.path.join(BASE_DIR, ".env"))
ALERT_TOKEN = _env.get("ADMIN_BOT_TOKEN", "")
# Who gets DM'd. ALERT_TELEGRAM_ID notifies a SUBSET of admins (e.g. just you)
# without touching anyone's admin powers - everyone in ADMIN_TELEGRAM_ID can
# still run every command. When ALERT_TELEGRAM_ID is blank/absent, alerts fall
# back to all admins (the previous behaviour).
_alert_raw = _env.get("ALERT_TELEGRAM_ID", "").strip() or _env.get("ADMIN_TELEGRAM_ID", "")
ALERT_IDS = [x.strip() for x in _alert_raw.split(",") if x.strip().isdigit()]


def send_alert(text):
    """DM every admin via the admin bot (plain HTTPS, no library). Telegram
    only allows DMs to users who have already messaged the bot - true for
    every admin by definition. Best-effort: an alert failure must never
    crash or stall the supervisor."""
    if not ALERT_TOKEN or not ALERT_IDS:
        return
    for chat_id in ALERT_IDS:
        try:
            data = urllib.parse.urlencode(
                {"chat_id": chat_id, "text": text}).encode()
            urllib.request.urlopen(
                "https://api.telegram.org/bot{}/sendMessage".format(ALERT_TOKEN),
                data=data, timeout=10).read()
        except Exception as exc:
            logger.warning("Alert to %s failed: %s", chat_id, exc)


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


def load_registry():
    """Read runner_projects.json and return (projects, pending_dirs).

    projects: validated entries whose folder exists (see validate_registry).
    pending_dirs: {key: abs_dir} for entries whose folder does NOT exist yet -
    a first-time deploy registers the project before the Hub's Update
    bootstraps the folder, so the runner re-checks these and picks the project
    up the moment the folder appears (no runner restart needed).
    Raises on unreadable/invalid JSON - callers decide how to degrade.
    """
    # utf-8-sig: humans edit this file, and Notepad/PowerShell write a BOM
    with open(REGISTRY_FILE, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    projects = validate_registry(raw, BASE_DIR)
    pending = {}
    for key, spec in raw.items():
        if key.startswith("_") or key in projects:
            continue
        pending[key] = os.path.abspath(os.path.join(BASE_DIR, spec.get("path", key)))
    return projects, pending


def unsupervised_projects(managed_keys):
    """Keys registered in the Hub's projects.json but absent from the runner's
    own registry. These are the silent gaps that bit us once: the Hub can
    start/stop such a project, but nothing restarts it after a crash or a
    reboot. Best-effort - an unreadable projects.json returns []."""
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8-sig") as f:
            hub_keys = [k for k in json.load(f) if not k.startswith("_")]
    except (OSError, ValueError):
        return []
    return sorted(k for k in hub_keys if k not in managed_keys)


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
    def __init__(self, projects, pending_dirs=None):
        self.projects = projects
        self.children = {}        # (key, base) -> Popen
        self.adopted = {}         # (key, base) -> pid (seen in last scan)
        self.death_time = {}      # (key, base) -> monotonic time of death
        self.restarts = {}        # (key, base) -> [monotonic timestamps]
        self.cooldown_until = {}  # (key, base) -> monotonic time
        self.started_once = set() # (key, base) that ran at least once (so a
                                  # boot start is not alerted as a crash)
        self.last_scan = 0.0
        self.start_time = time.monotonic()
        self.last_heartbeat = time.monotonic()
        self.hub_alive = True     # optimistic until the first scan says otherwise
        self.hub_dir_norm = norm_dir(BASE_DIR)
        self.desired = self._load_state()
        self.pending_dirs = dict(pending_dirs or {})
        try:
            self.registry_mtime = os.path.getmtime(REGISTRY_FILE)
        except OSError:
            self.registry_mtime = None

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
            else:  # start = resume supervision (the Hub already launched it)
                self.desired[key] = True
                self._save_state()
                # Adopt the process the Hub just launched so reconcile sees it
                # alive and never spawns a duplicate. If nothing is running yet,
                # the normal reconcile loop starts it on the next pass. We never
                # kill here: the Hub owns the launch (Restart/Update kill first
                # themselves), so killing would nuke the freshly-started process.
                self.adopt_scan()

    def _forget(self, key):
        for d in (self.children, self.adopted, self.death_time,
                  self.restarts, self.cooldown_until):
            for k in [k for k in d if k[0] == key]:
                del d[k]

    # -- registry hot-reload --------------------------------------------------
    def check_registry(self):
        """Pick up runner_projects.json edits without a runner restart.

        The registry used to be read once at startup, so a project registered
        while the runner was already up was invisible to the reconcile loop
        AND the heartbeat until the next /hub_update - exactly how a new bot
        ended up unsupervised for weeks. Reload triggers: the file's mtime
        changed, or a previously folder-less entry's folder appeared (first
        deploys register before the Hub bootstraps the folder). A broken edit
        keeps the last good registry."""
        try:
            mtime = os.path.getmtime(REGISTRY_FILE)
        except OSError:
            return
        appeared = any(os.path.isdir(d) for d in self.pending_dirs.values())
        if mtime == self.registry_mtime and not appeared:
            return
        changed = mtime != self.registry_mtime
        self.registry_mtime = mtime
        try:
            projects, pending = load_registry()
        except Exception as exc:
            # Only report on an actual edit, once - not every 5 s poll.
            if changed:
                logger.error("Registry changed but is invalid - keeping the "
                             "previous one: %s", exc)
                send_alert("⚠️ runner_projects.json changed but could not be "
                           "loaded ({}) - the runner kept the previous "
                           "registry.".format(exc))
            return
        added = sorted(k for k in projects if k not in self.projects)
        removed = sorted(k for k in self.projects if k not in projects)
        self.projects = projects
        self.pending_dirs = pending
        if not added and not removed:
            return
        for key in removed:
            self._forget(key)
            self.desired.pop(key, None)
        for key in added:
            self.desired.setdefault(key, True)  # autostart, same as boot
        self._save_state()
        logger.info("Registry reloaded - added: %s, removed: %s, managing: %s",
                    added or "-", removed or "-", sorted(self.projects))
        send_alert("🔄 Runner registry reloaded.\nAdded: {}\nRemoved: {}\n"
                   "Now managing: {}".format(
                       ", ".join(added) or "-", ", ".join(removed) or "-",
                       ", ".join(p["name"] for p in self.projects.values())))
        # Adopt anything already running for the new projects right away so
        # reconcile never double-starts them.
        self.adopt_scan()

    # -- spawning & adoption --------------------------------------------------
    def _spawn(self, key, proj, spec):
        try:
            child = spawn_process(proj, spec)
            self.children[(key, spec["base"])] = child
            self.death_time.pop((key, spec["base"]), None)
            self.started_once.add((key, spec["base"]))
            logger.info("Started %s / %s (PID %d)", proj["name"], spec["base"], child.pid)
        except Exception as exc:
            logger.error("Failed to start %s / %s: %s", proj["name"], spec["base"], exc)

    def adopt_scan(self):
        """Find managed processes we didn't spawn (legacy launches, pre-restart
        survivors) so we never start duplicates. Also notes whether the Admin
        Hub itself is alive (checked on this slower cadence on purpose - a
        process scan spawns a PowerShell)."""
        self.adopted.clear()
        procs = list_python_processes()
        for key, proj in self.projects.items():
            basenames = [p["base"] for p in proj["processes"]]
            for proc in procs:
                base = belongs_to(proc["cmd"], proc["exe"], proj["dir_norm"], basenames)
                if base and (key, base) not in self.children:
                    self.adopted[(key, base)] = proc["pid"]
        self.hub_alive = any(
            belongs_to(p["cmd"], p["exe"], self.hub_dir_norm, [HUB_SCRIPT])
            for p in procs)
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
            send_alert("🔴 {} / {} keeps crashing ({} restarts in {} min) - "
                       "paused for {} min. Check /logs.".format(
                           key, base, MAX_RESTARTS, RESTART_WINDOW // 60,
                           COOLDOWN_SECONDS // 60))
            return False
        history.append(now)
        self.restarts[(key, base)] = history
        return True

    def reconcile(self):
        self.check_registry()
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
                    if (key, base) in self.started_once:
                        send_alert("♻️ {} / {} exited unexpectedly - "
                                   "restarting it.".format(proj["name"], base))
                    self._spawn(key, proj, spec)
        self.supervise_hub()
        self.maybe_heartbeat()

    # -- the Hub's own safety net ---------------------------------------------
    def supervise_hub(self):
        """Relaunch the Admin Hub if it is down. The Hub manages everything
        else; this covers the one process nothing else watches. The startup
        grace period avoids double-launching while START_SERVER.bat or
        UPDATE_ADMIN.bat are still bringing the Hub up themselves."""
        if self.hub_alive:
            return
        if time.monotonic() - self.start_time < HUB_STARTUP_GRACE:
            return
        if not self._may_restart(*HUB_KEY):
            return
        logger.warning("Admin Hub is down - relaunching LAUNCH_ADMIN.bat")
        send_alert("⚠️ The Admin Hub was down - the runner is relaunching it.")
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", os.path.join(BASE_DIR, "LAUNCH_ADMIN.bat")],
                cwd=BASE_DIR, creationflags=CREATE_NEW_CONSOLE)
            self.hub_alive = True  # optimistic; next scan re-checks
        except Exception as exc:
            logger.error("Failed to relaunch the Admin Hub: %s", exc)

    def maybe_heartbeat(self):
        if time.monotonic() - self.last_heartbeat < HEARTBEAT_SECONDS:
            return
        self.last_heartbeat = time.monotonic()
        lines = ["💓 Daily heartbeat ({})".format(socket.gethostname())]
        for key, proj in self.projects.items():
            want = self.desired.get(key)
            alive = sum(1 for spec in proj["processes"]
                        if self._alive(key, spec["base"]))
            total = len(proj["processes"])
            if want:
                mark = "✅" if alive == total else "⚠️"
                lines.append("{} {}: {}/{} running".format(mark, proj["name"], alive, total))
            else:
                lines.append("🛑 {}: stopped (intentional)".format(proj["name"]))
        lines.append("{} Admin Hub: {}".format(
            "✅" if self.hub_alive else "🔴",
            "running" if self.hub_alive else "DOWN"))
        if self.pending_dirs:
            lines.append("⏳ Registered, folder not deployed yet: {}".format(
                ", ".join(sorted(self.pending_dirs))))
        gaps = unsupervised_projects(set(self.projects) | set(self.pending_dirs))
        if gaps:
            lines.append("🚨 In projects.json but NOT supervised (add to "
                         "runner_projects.json!): {}".format(", ".join(gaps)))
        try:
            free_gb = shutil.disk_usage(BASE_DIR).free / 1024 ** 3
            lines.append("💾 Disk free: {:.0f} GB".format(free_gb))
        except OSError:
            pass
        send_alert("\n".join(lines))


def main():
    # Single instance guard: one supervisor per machine.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        logger.info("Another runner instance is already alive - exiting.")
        return 0

    try:
        projects, pending = load_registry()
    except Exception as exc:
        logger.critical("Cannot load registry %s: %s", REGISTRY_FILE, exc)
        return 1

    logger.info("Runner starting - managing: %s",
                ", ".join(p["name"] for p in projects.values()) or "(nothing)")

    # If the last /hub_update failed and was rolled back, UPDATE_ADMIN.bat
    # leaves a flag - report it now that we (the rolled-back version) are up.
    try:
        if os.path.exists(ROLLBACK_FLAG):
            with open(ROLLBACK_FLAG, "r", encoding="utf-8-sig") as f:
                send_alert("🔴 Hub update FAILED and was rolled back:\n"
                           + f.read().strip())
            os.remove(ROLLBACK_FLAG)
    except OSError:
        pass

    start_lines = ["🟢 Runner started on {} - managing: {}".format(
        socket.gethostname(),
        ", ".join(p["name"] for p in projects.values()) or "(nothing)")]
    if pending:
        start_lines.append("⏳ Registered, folder not deployed yet: {}".format(
            ", ".join(sorted(pending))))
    gaps = unsupervised_projects(set(projects) | set(pending))
    if gaps:
        start_lines.append("🚨 In projects.json but NOT supervised (add to "
                           "runner_projects.json!): {}".format(", ".join(gaps)))
    send_alert("\n".join(start_lines))

    runner = Runner(projects, pending)
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

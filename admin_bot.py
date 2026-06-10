"""
Admin Hub Bot - Multi-Project Telegram Management Bot
=====================================================
Manages multiple projects on a Windows server via Telegram inline keyboards.
Each admin selects a project, then issues commands routed to that project's directory.

python-telegram-bot v22.6 | Windows 11 | Python 3.12+
"""

import asyncio
import datetime
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Paths & Environment
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_IDS: set[int] = set()
for _id in ADMIN_IDS_RAW.split(","):
    _id = _id.strip()
    if _id.isdigit():
        ADMIN_IDS.add(int(_id))

BOT_START_TIME = datetime.datetime.now()
PROJECTS_JSON = os.path.join(BASE_DIR, "projects.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# The bot is the ONLY writer to its log file (the launcher must NOT Tee-Object
# into it — two writers on one file is a guaranteed sharing violation on
# Windows). delay=True + the try/except guarantee a locked or unwritable log
# file can never prevent the bot from starting: worst case we run console-only.
LOG_FILE = os.path.join(LOG_DIR, "admin_bot.log")
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    _handlers.append(
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3,
            encoding="utf-8", delay=True,
        )
    )
except OSError as _exc:
    print("[WARN] Cannot open {}: {} - logging to console only".format(LOG_FILE, _exc))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
# httpx logs every Telegram API request at INFO - including the bot token in
# the URL. Keep it out of the log file.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("AdminHub")

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
projects: dict = {}           # loaded from projects.json
active_project: dict = {}    # user_id -> project key


def load_projects() -> dict:
    """Load project registry from projects.json."""
    global projects
    try:
        with open(PROJECTS_JSON, "r", encoding="utf-8") as f:
            raw_projects = json.load(f)
            
        # Resolve all relative paths into absolute paths based on BASE_DIR
        for key, proj in raw_projects.items():
            raw_path = proj.get("path", "")
            if raw_path:
                proj["path"] = os.path.abspath(os.path.join(BASE_DIR, raw_path))
                
        projects = raw_projects
        logger.info("Loaded %d projects from registry", len(projects))
    except FileNotFoundError:
        logger.error("projects.json not found at %s", PROJECTS_JSON)
        projects = {}
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in projects.json: %s", exc)
        projects = {}
    return projects


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def admin_only(func):
    """Decorator: reject non-admin users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Unauthorized", show_alert=True)
            elif update.message:
                await update.message.reply_text("\u26d4 Unauthorized.")
            logger.warning("Rejected user %s", user_id)
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def get_active(user_id: int) -> tuple[str | None, dict | None]:
    """Return (project_key, project_dict) or (None, None)."""
    key = active_project.get(user_id)
    if key and key in projects:
        return key, projects[key]
    return None, None


def run_shell(cmd: str, cwd: str | None = None, timeout: int = 30) -> str:
    """Run a shell command and return combined stdout+stderr."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "[Timed out after {}s]".format(timeout)
    except Exception as exc:
        return "[Error: {}]".format(exc)


def run_ps(script: str, timeout: int = 15) -> str:
    """Run a PowerShell snippet and return output."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "[PowerShell timed out]"
    except Exception as exc:
        return "[PS Error: {}]".format(exc)


def get_running_python_processes() -> list[dict]:
    """Return list of dicts with PID, ExecutablePath and CommandLine for python procs.

    ExecutablePath is always absolute (e.g. ...\\<Project>\\venv\\Scripts\\python.exe),
    so it reliably identifies which project a process belongs to even when the launch
    command line used a relative path like ".\\venv\\Scripts\\python.exe".
    """
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" "
        "| ForEach-Object { $_.ProcessId.ToString() + '|||' + "
        "[string]$_.ExecutablePath + '|||' + [string]$_.CommandLine }"
    )
    raw = run_ps(ps_script, timeout=15)
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if "|||" not in line:
            continue
        parts = line.split("|||", 2)  # limit so '|||' inside a command line is preserved
        if len(parts) < 3:
            continue
        pid_str, exe, cmdline = parts
        try:
            results.append({
                "pid": int(pid_str.strip()),
                "exe": exe.strip(),
                "cmd": cmdline.strip(),
            })
        except ValueError:
            continue
    return results


def kill_project_processes(project_key: str, proj: dict) -> list[int]:
    """Kill Python processes matching the script names for a specific project.

    Returns list of killed PIDs.
    """
    script_names = proj.get("scripts", [])
    if not script_names:
        return []

    # Require the project's own folder path in the command line so a generic
    # script name (e.g. "main.py") can never match an unrelated app elsewhere
    # on the machine. Launch bats always invoke python by absolute path, so the
    # project directory is present in every legitimate command line.
    # The trailing backslash prevents prefix collisions: 'd:\bus\' can never
    # match a process living in 'd:\bus2\'.
    proj_path = (proj.get("path") or "").lower().rstrip("\\/")
    proj_prefix = (proj_path + "\\") if proj_path else ""

    my_pid = os.getpid()
    all_procs = get_running_python_processes()
    killed: list[int] = []

    for proc in all_procs:
        pid = proc["pid"]
        cmd = proc["cmd"].lower()
        exe = proc.get("exe", "").lower()

        # Never kill self
        if pid == my_pid:
            continue
        # Never kill admin_bot or the central runner
        if "admin_bot" in cmd or "runner.py" in cmd:
            continue
        # Only touch processes that actually belong to THIS project's folder.
        # Check both the command line AND the absolute executable path, so a
        # relative-path launch can't slip past and a generic script name
        # (e.g. main.py) elsewhere can't be matched.
        if proj_prefix and proj_prefix not in cmd and proj_prefix not in exe:
            continue

        for script in script_names:
            if script.lower() in cmd:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=10,
                    )
                    killed.append(pid)
                    logger.info("Killed PID %d (%s) with /T for project %s", pid, script, project_key)
                except Exception as exc:
                    logger.error("Failed to kill PID %d: %s", pid, exc)
                break  # don't double-match same PID

    return killed


def launch_project(proj: dict) -> bool:
    """Launch COMPLETE_LAUNCH.bat in the project directory."""
    project_path = proj["path"]
    bat = os.path.join(project_path, "COMPLETE_LAUNCH.bat")
    if not os.path.isfile(bat):
        logger.error("COMPLETE_LAUNCH.bat not found at %s", bat)
        return False
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", proj["name"], bat],
            cwd=project_path,
        )
        logger.info("Launched COMPLETE_LAUNCH.bat for %s", proj["name"])
        return True
    except Exception as exc:
        logger.error("Failed to launch bat: %s", exc)
        return False


def get_last_commit(project_path: str) -> str:
    """Get last git commit hash + message."""
    if not os.path.isdir(os.path.join(project_path, ".git")):
        return "Not a git repo"
    return run_shell('git log -1 --format="%h %s"', cwd=project_path, timeout=10)


def get_disk_free(project_path: str) -> str:
    """Get free disk space for the drive containing the project."""
    try:
        usage = shutil.disk_usage(project_path)
        free_gb = usage.free / (1024 ** 3)
        return "{:.1f} GB".format(free_gb)
    except Exception:
        return "Unknown"


def get_db_total_size(project_path: str) -> str:
    """Sum up all .db files in data/ subdirectory."""
    data_dir = os.path.join(project_path, "data")
    if not os.path.isdir(data_dir):
        return "No data/ directory"
    total = 0
    count = 0
    for f in Path(data_dir).glob("*.db"):
        total += f.stat().st_size
        count += 1
    if count == 0:
        return "No .db files"
    mb = total / (1024 ** 2)
    return "{} file(s), {:.1f} MB".format(count, mb)


def check_scripts_running(proj: dict) -> list[str]:
    """Return list of script names that are currently running.

    Scoped to the project's own folder (trailing backslash, like
    kill_project_processes) so another project's generic 'main.py' is never
    reported as this project's process."""
    script_names = proj.get("scripts", [])
    proj_path = (proj.get("path") or "").lower().rstrip("\\/")
    proj_prefix = (proj_path + "\\") if proj_path else ""
    all_procs = get_running_python_processes()
    running = []
    for script in script_names:
        for proc in all_procs:
            cmd = proc["cmd"].lower()
            exe = proc.get("exe", "").lower()
            if proj_prefix and proj_prefix not in cmd and proj_prefix not in exe:
                continue
            if script.lower() in cmd:
                running.append(script)
                break
    return running


def pip_install(project_path: str) -> str:
    """Run pip install -r requirements.txt using the project's venv."""
    venv_python = os.path.join(project_path, "venv", "Scripts", "python.exe")
    req_file = os.path.join(project_path, "requirements.txt")

    if not os.path.isfile(venv_python):
        return "[No venv found at {}]".format(venv_python)
    if not os.path.isfile(req_file):
        return "[No requirements.txt found]"

    try:
        result = subprocess.run(
            [venv_python, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        lines = (result.stdout + result.stderr).strip().splitlines()
        # Return just the last few lines to keep it readable
        tail = lines[-5:] if len(lines) > 5 else lines
        return "\n".join(tail)
    except subprocess.TimeoutExpired:
        return "[pip install timed out after 120s]"
    except Exception as exc:
        return "[pip error: {}]".format(exc)


def read_log_tail(filepath: str, lines: int = 15) -> str:
    """Read last N lines of a log file.

    Sniffs the BOM to pick the encoding: old Tee-Object logs are UTF-16 LE,
    everything written by Python logging is UTF-8. (Decoding UTF-8 as UTF-16
    does not raise - it silently produces CJK garbage - so trying UTF-16
    first is not safe.) Binary mode also avoids sharing issues with files
    another process holds open for writing.
    """
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            content = raw.decode("utf-16", errors="replace")
        else:
            content = raw.decode("utf-8", errors="replace")

        all_lines = content.splitlines()
        tail = all_lines[-lines:]
        return "\n".join(tail)
    except Exception as exc:
        return "[Error reading {}: {}]".format(os.path.basename(filepath), exc)


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------
def build_projects_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard with a button for each project."""
    buttons = []
    for key, proj in projects.items():
        emoji = proj.get("emoji", "\u2699\ufe0f")
        label = "{} {}".format(emoji, proj["name"])
        buttons.append([InlineKeyboardButton(label, callback_data="select_{}".format(key))])
    buttons.append([InlineKeyboardButton("\U0001f504 Reload Registry", callback_data="reload_registry")])
    return InlineKeyboardMarkup(buttons)


def build_actions_keyboard(project_key: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with action buttons for the selected project."""
    row1 = [
        InlineKeyboardButton("\U0001f4cb Logs", callback_data="action_logs"),
        InlineKeyboardButton("\U0001f4ca Status", callback_data="action_status"),
    ]
    row2 = [
        InlineKeyboardButton("\u25b6\ufe0f Start", callback_data="action_start"),
        InlineKeyboardButton("\U0001f6d1 Stop", callback_data="action_stop"),
        InlineKeyboardButton("\U0001f504 Restart", callback_data="action_restart"),
    ]
    row3 = [
        InlineKeyboardButton("\U0001f680 Update", callback_data="action_update"),
        InlineKeyboardButton("\U0001f4e6 Backup", callback_data="action_backup"),
    ]
    row4 = [
        InlineKeyboardButton("\U0001f4c8 DB Stats", callback_data="action_dbstats"),
        InlineKeyboardButton("\u23ea Rollback", callback_data="action_rollback"),
    ]
    row5 = [
        InlineKeyboardButton("\u25c0\ufe0f Back to Projects", callback_data="back_to_projects"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4, row5])


# ---------------------------------------------------------------------------
# Hub-level commands
# ---------------------------------------------------------------------------
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show project selection keyboard."""
    if not projects:
        load_projects()
    text = "\U0001f3e0 <b>Admin Hub</b>\n\nSelect a project to manage:"
    await update.message.reply_text(text, reply_markup=build_projects_keyboard(), parse_mode="HTML")


@admin_only
async def cmd_hub_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overview status of ALL projects."""
    if not projects:
        load_projects()

    uptime = datetime.datetime.now() - BOT_START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    lines = [
        "\U0001f3e0 <b>Hub Overview</b>",
        "\u23f1 Uptime: {}h {}m {}s".format(hours, minutes, seconds),
        "",
    ]

    for key, proj in projects.items():
        emoji = proj.get("emoji", "\u2699\ufe0f")
        path = proj["path"]
        exists = os.path.isdir(path)
        running = check_scripts_running(proj) if exists else []

        commit = get_last_commit(path) if exists else "N/A"

        status_icon = "\u2705" if running else "\u26a0\ufe0f"
        path_icon = "\u2705" if exists else "\u274c"

        lines.append("{} <b>{}</b>".format(emoji, proj["name"]))
        lines.append("   {} Path: {}".format(path_icon, "exists" if exists else "MISSING"))
        lines.append("   \U0001f4dd Commit: {}".format(commit))
        if running:
            lines.append("   {} Running: {}".format(status_icon, ", ".join(running)))
        else:
            lines.append("   {} No scripts running".format(status_icon))
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@admin_only
async def cmd_hub_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the Admin Hub's own log file."""
    if not os.path.isfile(LOG_FILE):
        await update.message.reply_text("\u26a0\ufe0f No log file found.")
        return

    tail = read_log_tail(LOG_FILE, lines=30)
    text = "\U0001f4cb <b>Hub Log</b> (last 30 lines)\n\n<pre>{}</pre>".format(tail[:3500])
    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def cmd_hub_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Self-update the Admin Hub."""
    update_bat = os.path.join(BASE_DIR, "UPDATE_ADMIN.bat")
    if not os.path.isfile(update_bat):
        await update.message.reply_text("\u274c UPDATE_ADMIN.bat not found.")
        return

    await update.message.reply_text("\U0001f504 <b>Self-Updating Admin Hub...</b>\n\nPulling from GitHub, installing dependencies, and restarting. I will be back online in about 15 seconds.", parse_mode="HTML")
    
    # Launch the detached batch file which will kill us
    subprocess.Popen(["cmd.exe", "/c", "start", "", update_bat], cwd=BASE_DIR)
    
    # We gracefully stop polling (this triggers the retry loop, but the bat script kills the process before we can retry)
    context.application.stop_running()


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    text = (
        "\U0001f4d6 <b>Admin Hub Commands</b>\n\n"
        "<b>Hub-Level (no project needed):</b>\n"
        "/start, /projects \u2014 Select a project\n"
        "/hub_status \u2014 Overview of all projects\n"
        "/hub_logs \u2014 Admin Hub's own log\n"
        "/hub_update \u2014 Self-update Admin Hub via Git\n"
        "/help \u2014 This message\n\n"
        "<b>Project-Level (select a project first):</b>\n"
        "/logs \u2014 Show project log files\n"
        "/status \u2014 Project status details\n"
        "/update \u2014 Git pull + pip install + restart\n"
        "/launch \u2014 Start project (without killing first)\n"
        "/stop \u2014 Kill project processes\n"
        "/restart \u2014 Kill + relaunch (no git)\n"
        "/rollback \u2014 Revert last commit + restart\n"
        "/backup \u2014 SQLite backup + send ZIP\n"
        "/dbstats \u2014 Database table stats\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Project-level command helpers (shared logic for both /cmd and callback)
# ---------------------------------------------------------------------------
async def _require_project(update: Update) -> tuple[str | None, dict | None]:
    """Check active project. Sends error message if none selected. Returns (key, proj)."""
    user_id = update.effective_user.id
    key, proj = get_active(user_id)
    if key is None:
        msg = "\u26a0\ufe0f Please select a project first with /projects"
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        elif update.message:
            await update.message.reply_text(msg)
        return None, None
    return key, proj


async def do_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last 15 lines of each log file in the project's logs/ dir."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    logs_dir = os.path.join(project_path, "logs")

    if not os.path.isdir(logs_dir):
        msg = "\u26a0\ufe0f No logs/ directory in {}".format(proj["name"])
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text(msg)
        return

    log_files = list(Path(logs_dir).glob("*.log"))
    if not log_files:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text("\u26a0\ufe0f No .log files found.")
        return

    parts = ["\U0001f4cb <b>{} Logs</b>\n".format(proj["name"])]
    for lf in sorted(log_files):
        tail = read_log_tail(str(lf), lines=15)
        parts.append("<b>{}</b>\n<pre>{}</pre>\n".format(lf.name, tail[:1500]))

    text = "\n".join(parts)
    # Telegram message limit is ~4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (truncated)"

    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="HTML")


async def do_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed project status."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    emoji = proj.get("emoji", "\u2699\ufe0f")
    exists = os.path.isdir(project_path)

    commit = get_last_commit(project_path) if exists else "N/A"
    running = check_scripts_running(proj) if exists else []
    disk = get_disk_free(project_path) if exists else "N/A"
    db_info = get_db_total_size(project_path) if exists else "N/A"

    lines = [
        "{} <b>{}</b>".format(emoji, proj["name"]),
        "",
        "\U0001f4c1 Path: <code>{}</code>".format(project_path),
        "\u2705 Exists: {}".format("Yes" if exists else "NO"),
        "\U0001f4dd Last commit: {}".format(commit),
        "\U0001f4be Disk free: {}".format(disk),
        "\U0001f5c4 DB files: {}".format(db_info),
        "",
    ]

    if running:
        lines.append("\u2705 Running: {}".format(", ".join(running)))
    else:
        lines.append("\u26a0\ufe0f No scripts currently running")

    text = "\n".join(lines)
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(text, parse_mode="HTML")


async def do_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nuke and pave: git fetch + reset + pip install + kill + relaunch."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    target = update.callback_query.message if update.callback_query else update.message

    await target.reply_text("\U0001f680 <b>Deploying {}...</b>".format(proj["name"]), parse_mode="HTML")
    logger.info("UPDATE started for %s by user %s", key, update.effective_user.id)

    # Step 1: Git fetch + reset
    git_fetch = run_shell("git fetch origin main", cwd=project_path, timeout=30)
    git_reset = run_shell("git reset --hard origin/main", cwd=project_path, timeout=15)

    # Step 2: pip install
    pip_result = pip_install(project_path)

    # Step 3: Kill processes
    killed = kill_project_processes(key, proj)
    await asyncio.sleep(2)

    # Step 4: Relaunch
    launched = launch_project(proj)

    lines = [
        "\u2705 <b>Update Complete: {}</b>".format(proj["name"]),
        "",
        "<b>Git:</b>",
        "<pre>{}\n{}</pre>".format(git_fetch[:500], git_reset[:500]),
        "",
        "<b>pip:</b>",
        "<pre>{}</pre>".format(pip_result[:500]),
        "",
        "\U0001f480 Killed PIDs: {}".format(killed if killed else "none"),
        "\U0001f680 Launched: {}".format("Yes" if launched else "FAILED"),
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await target.reply_text(text, parse_mode="HTML")
    logger.info("UPDATE completed for %s", key)


async def do_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill project processes without restarting."""
    key, proj = await _require_project(update)
    if not proj:
        return

    target = update.callback_query.message if update.callback_query else update.message

    # Tell the central runner this stop is intentional, BEFORE killing, so it
    # never auto-restarts the project. Markers live in logs/ because that
    # folder is gitignored in every project. Best-effort: a marker failure
    # must never block the stop itself.
    try:
        marker_dir = os.path.join(proj["path"], "logs")
        os.makedirs(marker_dir, exist_ok=True)
        start_marker = os.path.join(marker_dir, "runner.start")
        if os.path.exists(start_marker):
            os.remove(start_marker)
        with open(os.path.join(marker_dir, "runner.stop"), "w", encoding="utf-8") as f:
            f.write("stop requested via Admin Hub\n")
    except OSError as exc:
        logger.warning("Could not write runner stop marker for %s: %s", key, exc)

    killed = kill_project_processes(key, proj)
    if killed:
        text = "\U0001f6d1 <b>{}</b> stopped.\nKilled PIDs: {}".format(proj["name"], killed)
    else:
        text = "\u26a0\ufe0f No running processes found for <b>{}</b>.".format(proj["name"])

    await target.reply_text(text, parse_mode="HTML")
    logger.info("STOP: %s, killed %s", key, killed)


async def do_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Launch a project WITHOUT killing first. Warns if it is already running
    (so it can never spin up a duplicate set of processes)."""
    key, proj = await _require_project(update)
    if not proj:
        return

    target = update.callback_query.message if update.callback_query else update.message

    already = check_scripts_running(proj)
    if already:
        await target.reply_text(
            "⚠️ <b>{}</b> is already running ({}).\nUse \U0001f504 Restart to relaunch it.".format(
                proj["name"], ", ".join(already)),
            parse_mode="HTML",
        )
        logger.info("START skipped for %s (already running: %s)", key, already)
        return

    launched = launch_project(proj)
    text = "▶️ <b>{}</b> {}".format(
        proj["name"], "started." if launched else "FAILED to start.")
    await target.reply_text(text, parse_mode="HTML")
    logger.info("START: %s, launched %s", key, launched)


async def do_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill + relaunch (no git pull)."""
    key, proj = await _require_project(update)
    if not proj:
        return

    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("\U0001f504 Restarting <b>{}</b>...".format(proj["name"]), parse_mode="HTML")

    killed = kill_project_processes(key, proj)
    await asyncio.sleep(2)
    launched = launch_project(proj)

    lines = [
        "\U0001f504 <b>Restart Complete: {}</b>".format(proj["name"]),
        "\U0001f480 Killed PIDs: {}".format(killed if killed else "none"),
        "\U0001f680 Launched: {}".format("Yes" if launched else "FAILED"),
    ]
    await target.reply_text("\n".join(lines), parse_mode="HTML")
    logger.info("RESTART: %s, killed %s, launched %s", key, killed, launched)


async def do_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rollback: git reset HEAD~1 + pip install + kill + relaunch."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("\u23ea <b>Rolling back {}...</b>".format(proj["name"]), parse_mode="HTML")
    logger.info("ROLLBACK started for %s by user %s", key, update.effective_user.id)

    git_result = run_shell("git reset --hard HEAD~1", cwd=project_path, timeout=15)
    pip_result = pip_install(project_path)
    killed = kill_project_processes(key, proj)
    await asyncio.sleep(2)
    launched = launch_project(proj)

    commit = get_last_commit(project_path)

    lines = [
        "\u23ea <b>Rollback Complete: {}</b>".format(proj["name"]),
        "",
        "<b>Git:</b>",
        "<pre>{}</pre>".format(git_result[:500]),
        "",
        "<b>Now at:</b> {}".format(commit),
        "",
        "<b>pip:</b>",
        "<pre>{}</pre>".format(pip_result[:500]),
        "",
        "\U0001f480 Killed: {}".format(killed if killed else "none"),
        "\U0001f680 Launched: {}".format("Yes" if launched else "FAILED"),
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await target.reply_text(text, parse_mode="HTML")
    logger.info("ROLLBACK completed for %s", key)


async def do_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SQLite backup: safe snapshot -> ZIP -> send via Telegram."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    data_dir = os.path.join(project_path, "data")
    target = update.callback_query.message if update.callback_query else update.message

    if not os.path.isdir(data_dir):
        await target.reply_text("\u26a0\ufe0f No data/ directory found.")
        return

    db_files = list(Path(data_dir).glob("*.db"))
    if not db_files:
        await target.reply_text("\u26a0\ufe0f No .db files found in data/.")
        return

    await target.reply_text("\U0001f4e6 Creating backup for <b>{}</b>...".format(proj["name"]), parse_mode="HTML")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = "{}_{}.zip".format(key, timestamp)
    zip_path = os.path.join(data_dir, zip_name)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for db_file in db_files:
                # Use SQLite backup API for safety
                backup_name = "{}.backup".format(db_file.name)
                backup_path = os.path.join(data_dir, backup_name)

                src_conn = sqlite3.connect(str(db_file))
                dst_conn = sqlite3.connect(backup_path)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
                    src_conn.close()

                zf.write(backup_path, db_file.name)

                # Clean up temp backup
                try:
                    os.remove(backup_path)
                except OSError:
                    pass

        zip_size_mb = os.path.getsize(zip_path) / (1024 ** 2)
        logger.info("Backup created: %s (%.1f MB)", zip_name, zip_size_mb)

        # Send file (Telegram limit: 2 GB for bots)
        if zip_size_mb > 2000:
            await target.reply_text("\u274c Backup too large ({:.0f} MB). Telegram limit is 2 GB.".format(zip_size_mb))
        else:
            await target.reply_document(
                document=open(zip_path, "rb"),
                filename=zip_name,
                caption="\U0001f4e6 {} backup ({:.1f} MB)".format(proj["name"], zip_size_mb),
            )

        # Clean up ZIP
        try:
            os.remove(zip_path)
        except OSError:
            pass

    except Exception as exc:
        logger.error("Backup failed for %s: %s", key, exc)
        await target.reply_text("\u274c Backup failed: {}".format(exc))


async def do_dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show table row counts for all .db files in the project's data/ dir."""
    key, proj = await _require_project(update)
    if not proj:
        return

    project_path = proj["path"]
    data_dir = os.path.join(project_path, "data")
    target = update.callback_query.message if update.callback_query else update.message

    if not os.path.isdir(data_dir):
        await target.reply_text("\u26a0\ufe0f No data/ directory found.")
        return

    db_files = list(Path(data_dir).glob("*.db"))
    if not db_files:
        await target.reply_text("\u26a0\ufe0f No .db files found in data/.")
        return

    parts = ["\U0001f4c8 <b>{} DB Stats</b>\n".format(proj["name"])]

    for db_file in sorted(db_files):
        db_size_mb = db_file.stat().st_size / (1024 ** 2)
        parts.append("\U0001f5c4 <b>{}</b> ({:.1f} MB)".format(db_file.name, db_size_mb))

        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cursor.fetchall()]

            if not tables:
                parts.append("   (no tables)")
            else:
                for table in tables:
                    try:
                        cursor.execute("SELECT COUNT(*) FROM \"{}\"".format(table))
                        count = cursor.fetchone()[0]
                        parts.append("   {} \u2014 {:,} rows".format(table, count))
                    except Exception:
                        parts.append("   {} \u2014 [error reading]".format(table))
            conn.close()
        except Exception as exc:
            parts.append("   [Error: {}]".format(exc))
        parts.append("")

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    await target.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Slash command wrappers (route to shared do_* functions)
# ---------------------------------------------------------------------------
@admin_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_logs(update, context)


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_status(update, context)


@admin_only
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_update(update, context)


@admin_only
async def cmd_launch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start is reserved for the projects menu, so the launch command is /launch
    await do_start(update, context)


@admin_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_stop(update, context)


@admin_only
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_restart(update, context)


@admin_only
async def cmd_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_rollback(update, context)


@admin_only
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_backup(update, context)


@admin_only
async def cmd_dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_dbstats(update, context)


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------
@admin_only
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    # --- Reload registry ---
    if data == "reload_registry":
        load_projects()
        await query.edit_message_text(
            "\u2705 Registry reloaded. {} project(s) loaded.".format(len(projects)),
            reply_markup=build_projects_keyboard(),
        )
        return

    # --- Back to projects ---
    if data == "back_to_projects":
        active_project.pop(user_id, None)
        await query.edit_message_text(
            "\U0001f3e0 <b>Admin Hub</b>\n\nSelect a project to manage:",
            reply_markup=build_projects_keyboard(),
            parse_mode="HTML",
        )
        return

    # --- Select project ---
    if data.startswith("select_"):
        key = data[len("select_"):]
        if key not in projects:
            await query.edit_message_text("\u274c Project '{}' not found in registry.".format(key))
            return

        active_project[user_id] = key
        proj = projects[key]
        emoji = proj.get("emoji", "\u2699\ufe0f")

        text = (
            "{emoji} <b>{name}</b>\n\n"
            "\U0001f4c1 Path: <code>{path}</code>\n\n"
            "Choose an action:"
        ).format(emoji=emoji, name=proj["name"], path=proj["path"])

        await query.edit_message_text(
            text,
            reply_markup=build_actions_keyboard(key),
            parse_mode="HTML",
        )
        logger.info("User %s selected project: %s", user_id, key)
        return

    # --- Action buttons ---
    if data.startswith("action_"):
        action = data[len("action_"):]

        action_map = {
            "logs": do_logs,
            "status": do_status,
            "update": do_update,
            "start": do_start,
            "stop": do_stop,
            "restart": do_restart,
            "rollback": do_rollback,
            "backup": do_backup,
            "dbstats": do_dbstats,
        }

        handler = action_map.get(action)
        if handler:
            await handler(update, context)
        else:
            await query.message.reply_text("\u274c Unknown action: {}".format(action))
        return


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    """Start the Admin Hub bot with auto-retry on transient errors."""
    if not BOT_TOKEN:
        print("[FATAL] ADMIN_BOT_TOKEN not set in .env")
        sys.exit(1)

    if not ADMIN_IDS:
        print("[FATAL] ADMIN_TELEGRAM_ID not set in .env")
        sys.exit(1)

    load_projects()
    logger.info("Admin Hub Bot starting")
    logger.info("Authorized admin IDs: %s", ADMIN_IDS)
    logger.info("Projects loaded: %s", list(projects.keys()))
    logger.info("BASE_DIR: %s", BASE_DIR)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Hub-level commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_start))
    app.add_handler(CommandHandler("hub_status", cmd_hub_status))
    app.add_handler(CommandHandler("hub_logs", cmd_hub_logs))
    app.add_handler(CommandHandler("hub_update", cmd_hub_update))
    app.add_handler(CommandHandler("help", cmd_help))

    # Project-level commands
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("launch", cmd_launch))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("rollback", cmd_rollback))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("dbstats", cmd_dbstats))

    # Callback handler for inline keyboards
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Retry loop for transient network errors
    max_retries = 0
    retry_delay = 10

    while True:
        try:
            logger.info("Starting polling (attempt %d)...", max_retries + 1)
            print("[AdminHub] Bot is running. Ctrl+C to stop.")
            app.run_polling(drop_pending_updates=True)
            break  # Clean shutdown
        except KeyboardInterrupt:
            logger.info("Shutdown requested by user")
            break
        except Exception as exc:
            max_retries += 1
            logger.error(
                "Polling crashed (attempt %d): %s\n%s",
                max_retries,
                exc,
                traceback.format_exc(),
            )
            if max_retries > 50:
                logger.critical("Too many retries (%d), giving up.", max_retries)
                break
            logger.info("Retrying in %ds...", retry_delay)
            time.sleep(retry_delay)
            # Exponential backoff capped at 5 minutes
            retry_delay = min(retry_delay * 2, 300)


if __name__ == "__main__":
    main()

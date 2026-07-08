# Admin_hub — Telegram Control Hub

The single remote lifeline to the friend's always-on Windows PC: `admin_bot.py` (Telegram bot,
inline-keyboard UI) + `runner.py` (hidden process supervisor) start/stop/update/back up the
sibling worker projects (Cyp_Bus_Bot, Constan_transcriber, German_Bot). Fleet-wide rules live
in the skills: **remote-host-deploy** (architecture, incident playbook), **telegram-bot-conventions**,
**windows-dev-env** — don't restate them here.

## Run
- `START_SERVER.bat` — normal entry: hidden runner + visible Hub window (server keeps a shortcut in shell:startup).
- `LAUNCH_ADMIN.bat` — Hub only; self-heals venv, pip-installs, kills duplicate instances first.
- `UPDATE_ADMIN.bat` — self-update: `git reset --hard origin/main`, 90 s health check, auto-rollback on failure.
- `STOP_ADMIN.bat` / `STOP_RUNNER.bat` (stopping the runner leaves worker bots running). `FIX_VENV.bat` rebuilds a broken venv.
- venv Python 3.14.2 (C:\Python314). Tests: `venv\Scripts\python -m pytest tests\`.

## Stack
- python-telegram-bot==22.6, python-dotenv==1.2.1; pytest + pytest-asyncio for tests.
- `runner.py` is deliberately stdlib-only (own .env parser, urllib for alerts) so it survives a broken pip.

## Secrets & config
- `.env` (gitignored, present locally): ADMIN_BOT_TOKEN, ADMIN_TELEGRAM_ID (comma-separated, includes the friend), optional ALERT_TELEGRAM_ID (alert-only subset).
- `projects.json` (Hub menus + kill-matching) and `runner_projects.json` (supervision) — a worker must be registered in BOTH; paths are relative sibling folders (`../X`).
- `runner_state.json` = runtime desired-state, gitignored — don't hand-edit or commit.

## Landmines
- **SEND-only, minimal, by design.** No MessageHandler / inbound file or document handlers exist —
  never add any. The Hub must never be patchable from Telegram input; code reaches the server
  only via `UPDATE_ADMIN.bat` pulling origin/main (with rollback). Resist feature creep here.
- The bot is the SOLE writer of `logs\admin_bot.log` — never Tee/pipe launcher output into it
  (Windows sharing violation prevents the bot from starting at all).
- Hub↔runner control is file markers `<project>/logs/runner.start|.stop` — no open ports.
  The runner adopts already-running processes; it never spawns duplicates.
- Kill-matching requires project folder path WITH trailing backslash AND a registered script name:
  project folder names must not be prefixes of each other; script names unique within a project.
- Two Hub instances = Telegram 409 Conflict. Launchers kill duplicates; the runner has a
  single-instance guard on localhost port 47631.
- httpx logger is forced to WARNING — at INFO it writes the bot token (in request URLs) to the log.

## State (as of 2026-07-09)
- 4 workers registered in BOTH registries (bus, transcriber, german, warmship); transcriber
  currently marked intentionally stopped in `runner_state.json`.
- Runner hot-reloads `runner_projects.json` (mtime / folder-appears) — no restart needed for new
  projects; heartbeat + startup alert + `/hub_status` flag projects.json↔runner_projects.json gaps,
  and `tests/test_registry_sync.py` fails the suite on them.
- `/env_check` and project Status show the .env mtime/size/sha256[:10] fingerprint; the bus bot's
  .env DM delivery replies with the same fingerprint AND pushes the file to B2 (its 10-min env
  sync treats B2 as canonical — before that push, DM-delivered .envs were silently reverted;
  that's what locked out the transcriber user after the power-outage reboot).
- Remote: https://github.com/fedor5556/Admin_hub_bot.git (deployed on the friend's PC).

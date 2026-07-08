import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, Message, CallbackQuery, User, Chat
from telegram.ext import ContextTypes
import sys
import os

# Add parent dir to path so we can import admin_bot
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import admin_bot

# Mock the environment variables before running tests
admin_bot.BOT_TOKEN = "TEST_TOKEN"
admin_bot.ADMIN_IDS = [12345]

# Setup mock projects registry
admin_bot.projects = {
    "test_proj": {
        "name": "Test Project",
        "path": "C:\\fake\\path",
        "scripts": ["main.py"]
    }
}
admin_bot.active_project = {}

@pytest.fixture
def mock_update():
    """Creates a fake Telegram Update from an authorized admin."""
    update = MagicMock(spec=Update)
    
    user = MagicMock(spec=User)
    user.id = 12345
    update.effective_user = user
    
    chat = MagicMock(spec=Chat)
    chat.id = 12345
    
    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()
    message.chat = chat
    message.from_user = user
    update.message = message
    update.callback_query = None
    
    return update

@pytest.fixture
def mock_context():
    return MagicMock(spec=ContextTypes.DEFAULT_TYPE)

@pytest.mark.asyncio
async def test_cmd_start_shows_projects(mock_update, mock_context):
    """Test that /start returns the inline keyboard with projects."""
    await admin_bot.cmd_start(mock_update, mock_context)
    
    # Assert reply_text was awaited with the welcome message
    mock_update.message.reply_text.assert_called_once()
    args, kwargs = mock_update.message.reply_text.call_args
    assert "Admin Hub" in args[0]
    assert kwargs.get("reply_markup") is not None

@pytest.mark.asyncio
async def test_unauthorized_user_blocked(mock_update, mock_context):
    """Test that non-admins get nothing."""
    mock_update.effective_user.id = 99999  # Unauthorized ID
    
    await admin_bot.cmd_start(mock_update, mock_context)
    
    # Assert bot replied with Unauthorized
    mock_update.message.reply_text.assert_called_once_with("\u26d4 Unauthorized.")

@pytest.mark.asyncio
async def test_project_selection_callback(mock_update, mock_context):
    """Test inline keyboard callback for selecting a project."""
    mock_update.callback_query = MagicMock(spec=CallbackQuery)
    mock_update.callback_query.data = "select_test_proj"
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.edit_message_text = AsyncMock()
    mock_update.callback_query.message = MagicMock()
    
    await admin_bot.callback_handler(mock_update, mock_context)
    
    # Check that state was updated
    assert admin_bot.active_project[12345] == "test_proj"
    
    # Check that it answered the query and edited the message
    mock_update.callback_query.answer.assert_called_once()
    mock_update.callback_query.edit_message_text.assert_called_once()
    args, kwargs = mock_update.callback_query.edit_message_text.call_args
    assert "Test Project" in args[0]


@pytest.mark.asyncio
async def test_start_launches_when_not_running(mock_update, mock_context, monkeypatch):
    """Start should launch the project when nothing is running."""
    admin_bot.active_project[12345] = "test_proj"
    monkeypatch.setattr(admin_bot, "check_scripts_running", lambda proj: [])
    launched = {"v": False}
    monkeypatch.setattr(admin_bot, "launch_project", lambda proj: launched.update(v=True) or True)

    await admin_bot.do_start(mock_update, mock_context)

    assert launched["v"] is True
    args, _ = mock_update.message.reply_text.call_args
    assert "started" in args[0]


@pytest.mark.asyncio
async def test_start_refuses_duplicate_when_running(mock_update, mock_context, monkeypatch):
    """Start must NOT launch a second copy when the project is already running."""
    admin_bot.active_project[12345] = "test_proj"
    monkeypatch.setattr(admin_bot, "check_scripts_running", lambda proj: ["main.py"])
    launched = {"v": False}
    monkeypatch.setattr(admin_bot, "launch_project", lambda proj: launched.update(v=True) or True)

    await admin_bot.do_start(mock_update, mock_context)

    assert launched["v"] is False  # no duplicate launch
    args, _ = mock_update.message.reply_text.call_args
    assert "already running" in args[0]


def test_env_file_info_reports_mtime_size_fingerprint(tmp_path):
    """The delivery-verification facts: size and sha256 fingerprint of the raw
    bytes (matching what the bus bot's .env delivery reply shows)."""
    import hashlib
    payload = b"KEY=value\nOTHER=x\n"
    (tmp_path / ".env").write_bytes(payload)
    info = admin_bot.env_file_info({"path": str(tmp_path)})
    assert info["size"] == len(payload)
    assert info["fp"] == hashlib.sha256(payload).hexdigest()[:10]
    assert info["mtime"]  # formatted timestamp present


def test_env_file_info_none_when_missing(tmp_path):
    assert admin_bot.env_file_info({"path": str(tmp_path)}) is None


def test_runner_registry_keys_reads_registry(tmp_path, monkeypatch):
    import json
    reg = tmp_path / "runner_projects.json"
    reg.write_text(json.dumps({"_README": "x", "bus": {}, "warmship": {}}), encoding="utf-8")
    monkeypatch.setattr(admin_bot, "RUNNER_PROJECTS_JSON", str(reg))
    assert admin_bot.runner_registry_keys() == {"bus", "warmship"}


def test_runner_registry_keys_none_when_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_bot, "RUNNER_PROJECTS_JSON", str(tmp_path / "nope.json"))
    assert admin_bot.runner_registry_keys() is None

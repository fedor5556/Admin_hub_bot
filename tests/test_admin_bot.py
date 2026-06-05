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

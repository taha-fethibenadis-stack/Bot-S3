"""Student Drive Bot - browse and download files from a folder via Telegram."""

import asyncio
import hashlib
import html
import logging
import math
import os
import posixpath
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Configuration (all via environment variables, nothing secret in the code)
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Folder that holds your files. Relative paths are resolved from this file's
# location, so "drive" means <repo>/drive. Override with DRIVE_FOLDER.
_drive = Path(os.environ.get("DRIVE_FOLDER", "drive"))
if not _drive.is_absolute():
    _drive = BASE_DIR / _drive
ROOT_FOLDER = os.path.realpath(_drive)

# Optional allowlist: comma-separated Telegram user IDs, e.g. "12345,67890".
# Leave empty to let everyone use the bot.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}

MAX_BUTTONS_PER_PAGE = 8
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot upload limit

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs full request URLs at INFO level, which include the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Path handling
#
# Telegram callback data is limited to 64 bytes, so paths are replaced by a
# short deterministic hash. Because the hash depends only on the path, buttons
# keep working after a restart/redeploy: on a cache miss the index is rebuilt
# by scanning the drive folder.
# --------------------------------------------------------------------------
_index: dict[str, str] = {}


def _is_hidden(name: str) -> bool:
    return name.startswith(".") or name.lower() == "desktop.ini"


def _make_id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]


def to_id(rel: str) -> str:
    sid = _make_id(rel)
    _index[sid] = rel
    return sid


def rebuild_index() -> None:
    _index.clear()
    _index[_make_id(".")] = "."
    for dirpath, dirnames, filenames in os.walk(ROOT_FOLDER):
        dirnames[:] = [d for d in dirnames if not _is_hidden(d)]
        for name in dirnames + [f for f in filenames if not _is_hidden(f)]:
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT_FOLDER)
            rel = rel.replace(os.sep, "/")
            _index[_make_id(rel)] = rel


def resolve_id(sid: str) -> str | None:
    rel = _index.get(sid)
    if rel is None:
        rebuild_index()
        rel = _index.get(sid)
    return rel


def safe_join(rel: str) -> str | None:
    """Return the absolute path for rel, or None if it escapes the root."""
    full = os.path.realpath(os.path.join(ROOT_FOLDER, *rel.split("/")))
    if full == ROOT_FOLDER or full.startswith(ROOT_FOLDER + os.sep):
        return full
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
ICON_MAP = {
    ".pdf": "📕",
    ".doc": "📄", ".docx": "📄", ".txt": "📝",
    ".xls": "📊", ".xlsx": "📊", ".csv": "📊",
    ".ppt": "📽️", ".pptx": "📽️",
    ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️",
    ".mp4": "🎬", ".avi": "🎬", ".mov": "🎬", ".mkv": "🎬",
    ".mp3": "🎵", ".wav": "🎵", ".flac": "🎵",
    ".zip": "🗜️", ".rar": "🗜️", ".7z": "🗜️",
    ".exe": "⚙️", ".msi": "⚙️",
    ".py": "🐍", ".js": "📜", ".html": "🌐", ".css": "🎨",
}


def get_file_icon(filename: str, is_dir: bool = False) -> str:
    if is_dir:
        return "📂"
    return ICON_MAP.get(os.path.splitext(filename)[1].lower(), "📄")


def format_file_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def list_items(path: str) -> list[str]:
    """Visible entries of a folder: folders first, then files, A-Z."""
    items = [i for i in os.listdir(path) if not _is_hidden(i)]
    items.sort(key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
    return items


def count_children(path: str) -> tuple[int, int]:
    """Return (files, folders) for a folder."""
    try:
        items = list_items(path)
    except OSError:
        return 0, 0
    folders = sum(1 for i in items if os.path.isdir(os.path.join(path, i)))
    return len(items) - folders, folders


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def build_view(rel: str, page: int = 0):
    """Build (text, keyboard) for a folder. Returns None if it doesn't exist."""
    full = safe_join(rel)
    if full is None or not os.path.isdir(full):
        return None

    try:
        items = list_items(full)
    except OSError:
        return None

    total_pages = max(1, math.ceil(len(items) / MAX_BUTTONS_PER_PAGE))
    page = min(max(page, 0), total_pages - 1)
    start = page * MAX_BUTTONS_PER_PAGE
    page_items = items[start:start + MAX_BUTTONS_PER_PAGE]

    keyboard = []
    for item in page_items:
        item_full = os.path.join(full, item)
        item_rel = item if rel == "." else f"{rel}/{item}"
        sid = to_id(item_rel)

        if os.path.isdir(item_full):
            files, folders = count_children(item_full)
            text = f"{get_file_icon(item, True)} {item}\n   ({folders} folders, {files} files)"
            keyboard.append([InlineKeyboardButton(text, callback_data=f"d:{sid}")])
        else:
            try:
                size_str = format_file_size(os.path.getsize(item_full))
            except OSError:
                size_str = "?"
            text = f"{get_file_icon(item)} {item}\n   ({size_str})"
            keyboard.append([InlineKeyboardButton(text, callback_data=f"f:{sid}")])

    # Pagination
    nav = []
    here = to_id(rel)
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"p:{here}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"p:{here}:{page + 1}"))
    if nav:
        keyboard.append(nav)

    # Back / Home
    if rel == ".":
        keyboard.append([InlineKeyboardButton("🏠 Home", callback_data="root")])
    else:
        parent = posixpath.dirname(rel) or "."
        if parent == ".":
            keyboard.append([InlineKeyboardButton("⬅️ Back to Home", callback_data="root")])
        else:
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"d:{to_id(parent)}")])

    # Message text (HTML mode, every dynamic value is escaped)
    files, folders = count_children(full)
    title = "📚 <b>Student Drive</b>" if rel == "." else f"📂 <b>{html.escape(os.path.basename(full))}</b>"
    text = f"{title}\n\n📂 Folders: {folders} | 📄 Files: {files}\n\n"
    text += "Select a folder or file:" if items else "This folder is empty."
    if total_pages > 1:
        text += f"\n\nPage {page + 1}/{total_pages}"

    return text, InlineKeyboardMarkup(keyboard)


async def show_folder(query, rel: str, page: int = 0) -> None:
    view = await asyncio.to_thread(build_view, rel, page)
    if view is None:
        await query.answer("❌ Folder not found.", show_alert=True)
        return
    text, markup = view
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    await query.answer()


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    os.makedirs(ROOT_FOLDER, exist_ok=True)
    view = await asyncio.to_thread(build_view, ".", 0)
    text, markup = view
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    logger.info("User %s opened the drive", update.effective_user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "📖 <b>How to use this bot</b>\n\n"
        "1️⃣ Press /start to see the files and folders\n"
        "2️⃣ Tap a folder to open it\n"
        "3️⃣ Tap a file to download it\n"
        "4️⃣ Use ⬅️ Back to go to the parent folder\n"
        "5️⃣ Use Next/Previous to change pages\n\n"
        "<b>Features</b>\n"
        "• Folder navigation\n"
        "• File downloads (up to 50MB)\n"
        "• File sizes and folder contents shown\n"
        "• Pagination for large folders"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    action = parts[0]

    try:
        if action == "root":
            await show_folder(query, ".")
            return

        if action == "p" and len(parts) == 3 and parts[2].lstrip("-").isdigit():
            rel = await asyncio.to_thread(resolve_id, parts[1])
            if rel is None:
                await query.answer("❌ Session expired. Use /start to refresh.", show_alert=True)
                return
            await show_folder(query, rel, int(parts[2]))
            return

        if action in ("d", "f") and len(parts) == 2:
            rel = await asyncio.to_thread(resolve_id, parts[1])
            if rel is None:
                await query.answer("❌ Session expired. Use /start to refresh.", show_alert=True)
                return

            if action == "d":
                await show_folder(query, rel)
                return

            await send_file(query, context, rel)
            return

        await query.answer("❌ Unknown action.", show_alert=True)

    except Exception:
        logger.exception("Error in button_click")
        try:
            await query.answer("❌ An error occurred.", show_alert=True)
        except TelegramError:
            pass


async def send_file(query, context: ContextTypes.DEFAULT_TYPE, rel: str) -> None:
    full = safe_join(rel)
    if full is None or not os.path.isfile(full):
        await query.answer("❌ File not found.", show_alert=True)
        return

    file_size = os.path.getsize(full)
    if file_size > MAX_FILE_SIZE:
        await query.answer(
            f"❌ File too large ({format_file_size(file_size)}). Telegram limit is 50MB.",
            show_alert=True,
        )
        return

    await query.answer("⏳ Sending file...")
    filename = os.path.basename(full)
    try:
        with open(full, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=filename,
                caption=f"✅ {filename}\n📊 Size: {format_file_size(file_size)}",
                read_timeout=120,
                write_timeout=120,
            )
        logger.info("User %s downloaded: %s", query.from_user.id, rel)
    except TelegramError as e:
        logger.error("Telegram error sending file: %s", e)
        await query.message.reply_text(f"❌ Telegram error: {e}")
    except OSError as e:
        logger.error("Error reading file: %s", e)
        await query.message.reply_text("❌ Could not read the file.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ An error occurred. Please try again.")
        except TelegramError:
            pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is not set.")

    os.makedirs(ROOT_FOLDER, exist_ok=True)
    rebuild_index()
    logger.info("Student Drive Bot starting. Root folder: %s", ROOT_FOLDER)
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty: the bot is open to everyone.")

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_click))
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

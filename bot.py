import logging
import os
import re
import sqlite3
import tempfile
import asyncio
from datetime import datetime
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILE_SIZE_MB = 50
DOWNLOAD_TIMEOUT_SECONDS = 180
MAX_RETRIES = 2

DB_PATH = os.environ.get("DB_PATH", "/app/data/usage.db")

URL_REGEX = re.compile(r"https?://[^\s]+")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS downloads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER, "
        "username TEXT, "
        "first_name TEXT, "
        "url TEXT, "
        "domain TEXT, "
        "status TEXT, "
        "error_message TEXT, "
        "created_at TEXT)"
    )
    conn.commit()
    conn.close()


def log_download(user, url, status, error_message=None):
    try:
        domain = urlparse(url).netloc
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO downloads (user_id, username, first_name, url, domain, status, error_message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user.id if user else None,
                user.username if user else None,
                user.first_name if user else None,
                url,
                domain,
                status,
                error_message,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("DB log failed")


def friendly_error(e):
    msg = str(e).lower()
    if "private" in msg or "login" in msg or "cookies" in msg:
        return "This content is private or requires login, the bot cannot access it currently."
    if "unsupported url" in msg or "no extractor" in msg:
        return "This link is not supported. Make sure it is a direct video link from a supported site."
    if "video unavailable" in msg or "404" in msg:
        return "Video not found or has been deleted."
    if "timed out" in msg or "timeout" in msg:
        return "Download took too long, please try again or try another link."
    if "geo" in msg or "region" in msg:
        return "This content is geo-restricted and unavailable from the bot server."
    return "Could not download the video, check the link and try again."


def is_supported_url(url):
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


_COOKIES_TMP_PATH = None


def _get_cookies_file_path():
    global _COOKIES_TMP_PATH

    existing_file = os.environ.get("COOKIES_FILE")
    if existing_file and os.path.exists(existing_file):
        return existing_file

    cookies_text = os.environ.get("COOKIES_TEXT")
    if not cookies_text:
        return None

    if _COOKIES_TMP_PATH and os.path.exists(_COOKIES_TMP_PATH):
        return _COOKIES_TMP_PATH

    tmp_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(cookies_text)
    _COOKIES_TMP_PATH = tmp_path
    return tmp_path


async def start(update, context):
    await update.message.reply_text(
        "Hi! Send me a video link from YouTube, TikTok, Instagram, or Twitter/X and I will download it for you. Just send the link and wait a bit."
    )


async def help_command(update, context):
    await update.message.reply_text(
        "How to use:\n"
        "1. Copy the video link from the app\n"
        "2. Send it here in chat\n"
        "3. Wait for the download to finish\n\n"
        "Note: large videos (over " + str(MAX_FILE_SIZE_MB) + "MB) may not download due to Telegram limits."
    )


def download_video(url, out_dir):
    cookies_file = _get_cookies_file_path()

    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(title).80s.%(ext)s"),
        "format": "mp4[filesize<" + str(MAX_FILE_SIZE_MB) + "M]/best[ext=mp4][filesize<" + str(MAX_FILE_SIZE_MB) + "M]/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
    }
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            mp4_path = base + ".mp4"
            if os.path.exists(mp4_path):
                return mp4_path
            if os.path.exists(filename):
                return filename
            raise FileNotFoundError("File not saved after download")
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(str(e)) from e


async def handle_message(update, context):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("Please send a valid video link.")
        return

    url = match.group(0)

    if not is_supported_url(url):
        await update.message.reply_text("Invalid link, please check it and try again.")
        return

    status_msg = await update.message.reply_text("Downloading, please wait...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = None
        last_error = None
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                loop = asyncio.get_event_loop()
                file_path = await loop.run_in_executor(None, download_video, url, tmp_dir)
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.warning("Download attempt %s failed: %s", attempt, e)
                if attempt <= MAX_RETRIES:
                    await asyncio.sleep(2 * attempt)
                    continue

        if last_error is not None:
            logger.exception("Download failed after retries")
            err_text = friendly_error(last_error)
            log_download(update.effective_user, url, "failed", err_text)
            await status_msg.edit_text("Error: " + err_text)
            return

        if not os.path.exists(file_path):
            await status_msg.edit_text("Could not find the file after download.")
            return

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                "Video size " + str(round(size_mb, 1)) + "MB exceeds the allowed limit (" + str(MAX_FILE_SIZE_MB) + "MB) for bot upload."
            )
            return

        await status_msg.edit_text("Download complete, sending...")
        try:
            with open(file_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption="Done!",
                    write_timeout=120,
                    read_timeout=120,
                    connect_timeout=60,
                )
            await status_msg.delete()
            log_download(update.effective_user, url, "success")
        except Exception as e:
            logger.exception("Send failed")
            log_download(update.effective_user, url, "failed", "Failed to send file")
            await status_msg.edit_text("Could not send the file, the video may be too large or there was a connection issue.")


async def error_handler(update, context):
    logger.error("Unhandled exception", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

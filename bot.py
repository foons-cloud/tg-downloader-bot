import logging
import os
import re
import sqlite3
import tempfile
import asyncio
import threading
from datetime import datetime
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, render_template_string
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILE_SIZE_MB = 50
MAX_RETRIES = 2
DB_PATH = os.environ.get("DB_PATH", "/app/data/usage.db")
URL_REGEX = re.compile(r"https?://[^\s]+")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, first_name TEXT, url TEXT, domain TEXT, status TEXT, error_message TEXT, created_at TEXT)"
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
        return "This content is private or requires login."
    if "unsupported url" in msg or "no extractor" in msg:
        return "This link is not supported."
    if "video unavailable" in msg or "404" in msg:
        return "Video not found or deleted."
    if "timed out" in msg or "timeout" in msg:
        return "Download took too long, try again."
    if "geo" in msg or "region" in msg:
        return "Content is geo-restricted."
    return "Could not download the video, check the link."


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
    await update.message.reply_text("Hi! Send me a video link from YouTube, TikTok, Instagram, or Twitter/X.")


async def help_command(update, context):
    await update.message.reply_text("Send a video link and I will download it. Large files over 50MB may fail due to Telegram limits.")


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
            raise FileNotFoundError("File not saved")
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
        await update.message.reply_text("Invalid link.")
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
                logger.warning("Attempt %s failed: %s", attempt, e)
                if attempt <= MAX_RETRIES:
                    await asyncio.sleep(2 * attempt)
                    continue
        if last_error is not None:
            err_text = friendly_error(last_error)
            log_download(update.effective_user, url, "failed", err_text)
            await status_msg.edit_text("Error: " + err_text)
            return
        if not os.path.exists(file_path):
            await status_msg.edit_text("File not found after download.")
            return
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text("Video too large: " + str(round(size_mb, 1)) + "MB")
            return
        await status_msg.edit_text("Sending...")
        try:
            with open(file_path, "rb") as f:
                await update.message.reply_video(video=f, caption="Done!", write_timeout=120, read_timeout=120, connect_timeout=60)
            await status_msg.delete()
            log_download(update.effective_user, url, "success")
        except Exception as e:
            logger.exception("Send failed")
            log_download(update.effective_user, url, "failed", "Send failed")
            await status_msg.edit_text("Failed to send file.")


async def error_handler(update, context):
    logger.error("Unhandled exception", exc_info=context.error)


DASHBOARD_PAGE = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Bot Dashboard</title>
<style>
body{font-family:Tahoma,Arial,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:24px;}
h1{font-size:20px;}
.stats{display:flex;gap:16px;margin:16px 0 24px 0;flex-wrap:wrap;}
.card{background:#1a1d23;border-radius:10px;padding:16px 20px;min-width:140px;}
.card .num{font-size:26px;font-weight:bold;color:#4ade80;}
.card .label{font-size:13px;color:#9ca3af;margin-top:4px;}
table{width:100%;border-collapse:collapse;background:#1a1d23;border-radius:10px;overflow:hidden;font-size:13px;}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2a2d35;}
th{background:#21242b;color:#9ca3af;}
.success{color:#4ade80;}
.failed{color:#f87171;}
.empty{padding:40px;text-align:center;color:#6b7280;}
</style></head><body>
<h1>Bot Dashboard</h1>
<div class="stats">
<div class="card"><div class="num">{{ total }}</div><div class="label">Total</div></div>
<div class="card"><div class="num">{{ success_count }}</div><div class="label">Success</div></div>
<div class="card"><div class="num">{{ failed_count }}</div><div class="label">Failed</div></div>
<div class="card"><div class="num">{{ unique_users }}</div><div class="label">Unique Users</div></div>
</div>
{% if rows %}
<table><tr><th>Time</th><th>User</th><th>Domain</th><th>Status</th><th>Details</th></tr>
{% for r in rows %}
<tr><td>{{ r.created_at }}</td><td>{{ r.username or r.first_name or r.user_id }}</td><td>{{ r.domain }}</td>
<td class="{{ 'success' if r.status == 'success' else 'failed' }}">{{ 'Success' if r.status == 'success' else 'Failed' }}</td>
<td>{{ r.error_message or '-' }}</td></tr>
{% endfor %}</table>
{% else %}<div class="empty">No usage recorded yet.</div>{% endif %}
</body></html>
"""

dashboard_app = Flask(__name__)


def _get_dashboard_data():
    if not os.path.exists(DB_PATH):
        return [], 0, 0, 0, 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM downloads ORDER BY id DESC LIMIT 200").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    success_count = conn.execute("SELECT COUNT(*) FROM downloads WHERE status='success'").fetchone()[0]
    failed_count = conn.execute("SELECT COUNT(*) FROM downloads WHERE status='failed'").fetchone()[0]
    unique_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM downloads").fetchone()[0]
    conn.close()
    return rows, total, success_count, failed_count, unique_users


@dashboard_app.route("/")
def dashboard_home():
    rows, total, success_count, failed_count, unique_users = _get_dashboard_data()
    return render_template_string(DASHBOARD_PAGE, rows=rows, total=total, success_count=success_count, failed_count=failed_count, unique_users=unique_users)


def run_dashboard():
    port = int(os.environ.get("PORT", 8080))
    dashboard_app.run(host="0.0.0.0", port=port, use_reloader=False)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")
    init_db()
    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

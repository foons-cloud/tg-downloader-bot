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
MAX_FILE_SIZE_MB = 50  # حد تليكرام للبوتات العادية هو 50 ميغا للرفع
DOWNLOAD_TIMEOUT_SECONDS = 180  # حد أقصى لزمن التحميل قبل ما نلغيه
MAX_RETRIES = 2  # عدد محاولات إعادة التحميل عند فشل مؤقت (مشاكل شبكة مثلاً)

# قاعدة بيانات سجل الاستخدام - مشتركة مع الداشبورد عبر نفس الملف على القرص
DB_PATH = os.environ.get("DB_PATH", "/app/data/usage.db")

URL_REGEX = re.compile(r"https?://[^\s]+")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            url TEXT,
            domain TEXT,
            status TEXT,
            error_message TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_download(user, url: str, status: str, error_message: str = None):
    try:
        domain = urlparse(url).netloc
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO downloads (user_id, username, first_name, url, domain, status, error_message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
        logger.exception("فشل تسجيل العملية بقاعدة البيانات")

# رسائل خطأ مفهومة للمستخدم حسب نوع المشكلة
def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "private" in msg or "login" in msg or "cookies" in msg:
        return "هذا المحتوى خاص أو يحتاج تسجيل دخول، البوت ما يكدر يصله حالياً."
    if "unsupported url" in msg or "no extractor" in msg:
        return "هذا الرابط غير مدعوم. تأكد إنه رابط فيديو مباشر من موقع مدعوم."
    if "video unavailable" in msg or "404" in msg:
        return "الفيديو غير موجود أو تم حذفه."
    if "timed out" in msg or "timeout" in msg:
        return "استغرق التحميل وقت طويل جداً، حاول مرة ثانية أو جرب رابط آخر."
    if "geo" in msg or "region" in msg:
        return "هذا المحتوى مقيد جغرافياً وغير متاح من سيرفر البوت."
    return "تعذر تحميل الفيديو، تأكد من صحة الرابط وحاول م

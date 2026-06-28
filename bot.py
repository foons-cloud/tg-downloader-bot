import logging
import os
import re
import tempfile
import asyncio
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

URL_REGEX = re.compile(r"https?://[^\s]+")

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
    return "تعذر تحميل الفيديو، تأكد من صحة الرابط وحاول مرة ثانية."


def is_supported_url(url: str) -> bool:
    """فحص بسيط أن الرابط فيه دومين معروف (اختياري، yt-dlp يدعم مواقع كثيرة جداً)."""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "هلا بيك! 👋\n\n"
        "ابعثلي رابط فيديو من يوتيوب، تيك توك، انستا، أو تويتر/X وراح أنزلهولك.\n\n"
        "بس ارسل الرابط وانتظر شوية 🙂"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "طريقة الاستخدام:\n"
        "1. انسخ رابط الفيديو من التطبيق\n"
        "2. ابعثه هنا بالشات\n"
        "3. انتظر يجهز التحميل ويرسلهولك البوت\n\n"
        f"ملاحظة: الفيديوات الكبيرة (أكثر من {MAX_FILE_SIZE_MB}MB) ممكن ما تنزل بسبب حدود تليكرام."
    )


def download_video(url: str, out_dir: str) -> str:
    """يحمل الفيديو ويرجع مسار الملف. يرمي استثناء بوصف واضح عند الفشل."""
    cookies_file = os.environ.get("COOKIES_FILE")  # مسار اختياري لملف كوكيز (لانستا مثلاً)

    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(title).80s.%(ext)s"),
        "format": f"mp4[filesize<{MAX_FILE_SIZE_MB}M]/best[ext=mp4][filesize<{MAX_FILE_SIZE_MB}M]/best[ext=mp4]/best",
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
            raise FileNotFoundError("الملف لم يُحفظ بعد التحميل")
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(str(e)) from e


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text("ابعثلي رابط فيديو صحيح من فضلك 🙏")
        return

    url = match.group(0)

    if not is_supported_url(url):
        await update.message.reply_text("الرابط غير صحيح، تأكد منه وحاول مرة ثانية.")
        return

    status_msg = await update.message.reply_text("⏳ جاري التحميل، الرجاء الانتظار...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = None
        last_error = None
        for attempt in range(1, MAX_RETRIES + 2):  # محاولة أصلية + إعادة محاولات
            try:
                loop = asyncio.get_event_loop()
                file_path = await loop.run_in_executor(None, download_video, url, tmp_dir)
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.warning("Download attempt %s failed: %s", attempt, e)
                if attempt <= MAX_RETRIES:
                    await asyncio.sleep(2 * attempt)  # تأخير تدريجي قبل إعادة المحاولة
                    continue

        if last_error is not None:
            logger.exception("Download failed after retries")
            await status_msg.edit_text(f"❌ {friendly_error(last_error)}")
            return

        if not os.path.exists(file_path):
            await status_msg.edit_text("❌ تعذر العثور على الملف بعد التحميل.")
            return

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"❌ حجم الفيديو {size_mb:.1f}MB، وهذا أكبر من الحد المسموح ({MAX_FILE_SIZE_MB}MB) للرفع من البوت."
            )
            return

        await status_msg.edit_text("✅ تم التحميل، جاري الإرسال...")
        try:
            with open(file_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption="تم بنجاح ✅",
                    write_timeout=120,
                    read_timeout=120,
                    connect_timeout=60,
                )
            await status_msg.delete()
        except Exception as e:
            logger.exception("Send failed")
            await status_msg.edit_text("❌ تعذر إرسال الملف، الفيديو ممكن يكون كبير جداً أو حدث خلل بالاتصال.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("لم يتم تعيين BOT_TOKEN في متغيرات البيئة")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

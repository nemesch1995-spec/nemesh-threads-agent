# ═══════════════════════════════════════════════════════════════
# NEMESH THREADS-АГЕНТ — автопостинг у Threads з підтвердженням
# v2: додано картинки (кнопка 📷, карусель до 10 фото).
#
# БЕЗПЕКА: фото хостяться на власному домені агента (Railway), у Threads
# іде чисте посилання. Токен бота ніде не світиться.
#
# Логіка:
#   1. Читає банк тем із GitHub (сирий .md файл)
#   2. Бере наступну неопубліковану тему
#   3. Шле Артему в Telegram: 👍 / 📷 Додати фото / ✍️ / ❌
#   4. Після 👍 — публікує в Threads (текст, фото або карусель)
#   5. Веде лічильник, попереджає коли лишилось ≤3 теми
#   6. Розклад: 2 пости/день (Київ), рознесені в часі
# ═══════════════════════════════════════════════════════════════

import logging
import os
import re
import time
import uuid
import json
import mimetypes
import threading
import sqlite3
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("threads-agent")

# ───────────────────────────────────────────────
# КОНФІГ (усе з env-змінних Railway, нічого в коді)
# ───────────────────────────────────────────────

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID        = int(os.getenv("OWNER_CHAT_ID", "0"))
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
BANK_URL             = os.getenv("BANK_URL", "")

# Публічний домен агента (Railway → Settings → Networking → Generate Domain).
# Напр. https://nemesh-threads-agent-production.up.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if not PUBLIC_BASE_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    PUBLIC_BASE_URL = "https://" + os.getenv("RAILWAY_PUBLIC_DOMAIN").rstrip("/")

POST_HOUR_1 = int(os.getenv("POST_HOUR_1", "10"))
POST_HOUR_2 = int(os.getenv("POST_HOUR_2", "18"))
TZ = ZoneInfo("Europe/Kyiv")

LOW_BANK_THRESHOLD = 3
THREADS_MAX_LEN = 500
MAX_PHOTOS = 10

GRAPH = "https://graph.threads.net/v1.0"

DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
DB_PATH = os.path.join(DATA_DIR, "threads_agent.db")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

WEB_PORT = int(os.getenv("PORT", "8080"))

THREADS_USER_ID = None


# ───────────────────────────────────────────────
# МІНІ-СЕРВЕР ДЛЯ ФОТО (роздає /img/<файл> з тому)
# ───────────────────────────────────────────────

class ImgHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.startswith("/img/"):
            name = os.path.basename(self.path[len("/img/"):])
            fp = os.path.join(IMAGES_DIR, name)
            if os.path.isfile(fp):
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass  # не засмічуємо логи


def start_img_server():
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), ImgHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"Фото-сервер запущено на порту {WEB_PORT}")


# ───────────────────────────────────────────────
# БАЗА (пам'ять)
# ───────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS published(hash TEXT PRIMARY KEY, ts TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT)")
    return conn


def state_get(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def state_set(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def state_del(key):
    conn = db()
    conn.execute("DELETE FROM state WHERE key=?", (key,))
    conn.commit()
    conn.close()


def is_published(text_hash):
    conn = db()
    row = conn.execute("SELECT 1 FROM published WHERE hash=?", (text_hash,)).fetchone()
    conn.close()
    return row is not None


def mark_published(text_hash):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO published(hash,ts) VALUES(?,?)",
                 (text_hash, datetime.now(TZ).isoformat()))
    conn.commit()
    conn.close()


def post_hash(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# --- допоміжне: список фото поточного поста (URL + локальні шляхи) ---

def get_pending_images():
    raw = state_get("pending_images")
    return json.loads(raw) if raw else []


def set_pending_images(items):
    state_set("pending_images", json.dumps(items))


def clear_pending():
    for key in ("pending_hash", "pending_text", "await_edit",
                "await_photos", "pending_images"):
        state_del(key)


def cleanup_image_files():
    for it in get_pending_images():
        try:
            if it.get("path") and os.path.isfile(it["path"]):
                os.remove(it["path"])
        except Exception:
            pass


# ───────────────────────────────────────────────
# БАНК ТЕМ
# ───────────────────────────────────────────────

def fetch_bank():
    if not BANK_URL:
        return []
    try:
        r = requests.get(BANK_URL, timeout=20)
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        logger.error(f"Не вдалось завантажити банк: {e}")
        return []
    posts = []
    for chunk in re.split(r"(?m)^\s*---+\s*$", raw):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("#")]
        text = "\n".join(lines).strip()
        if text:
            posts.append(text)
    return posts


def next_unpublished():
    for text in fetch_bank():
        h = post_hash(text)
        if not is_published(h):
            return text, h
    return None, None


def remaining_count():
    return sum(1 for text in fetch_bank() if not is_published(post_hash(text)))


# ───────────────────────────────────────────────
# THREADS API
# ───────────────────────────────────────────────

def get_threads_user_id():
    global THREADS_USER_ID
    if THREADS_USER_ID:
        return THREADS_USER_ID
    r = requests.get(f"{GRAPH}/me",
                     params={"fields": "id,username", "access_token": THREADS_ACCESS_TOKEN},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    THREADS_USER_ID = data["id"]
    logger.info(f"Threads user: @{data.get('username')} (id={THREADS_USER_ID})")
    return THREADS_USER_ID


def _publish_container(uid, creation_id):
    """Публікує контейнер із кількома спробами (медіа інколи готується не миттєво)."""
    last = ""
    for attempt in range(4):
        r = requests.post(f"{GRAPH}/{uid}/threads_publish",
                          params={"access_token": THREADS_ACCESS_TOKEN,
                                  "creation_id": creation_id}, timeout=30)
        if r.ok:
            return r.json()["id"]
        last = r.text
        time.sleep(4)
    raise RuntimeError(f"publish не вдався: {last}")


def publish_to_threads(text, image_urls=None):
    """Публікує пост. image_urls: [] текст, [1] фото, [2..10] карусель.
       Повертає (True, url) або (False, помилка)."""
    image_urls = image_urls or []
    try:
        uid = get_threads_user_id()

        if not image_urls:
            params = {"access_token": THREADS_ACCESS_TOKEN, "media_type": "TEXT", "text": text}
            r1 = requests.post(f"{GRAPH}/{uid}/threads", params=params, timeout=30)
            r1.raise_for_status()
            creation_id = r1.json()["id"]

        elif len(image_urls) == 1:
            params = {"access_token": THREADS_ACCESS_TOKEN, "media_type": "IMAGE",
                      "image_url": image_urls[0], "text": text}
            r1 = requests.post(f"{GRAPH}/{uid}/threads", params=params, timeout=30)
            r1.raise_for_status()
            creation_id = r1.json()["id"]

        else:
            child_ids = []
            for url in image_urls[:MAX_PHOTOS]:
                rc = requests.post(f"{GRAPH}/{uid}/threads",
                                   params={"access_token": THREADS_ACCESS_TOKEN,
                                           "media_type": "IMAGE",
                                           "is_carousel_item": "true",
                                           "image_url": url}, timeout=30)
                rc.raise_for_status()
                child_ids.append(rc.json()["id"])
                time.sleep(1)
            rp = requests.post(f"{GRAPH}/{uid}/threads",
                               params={"access_token": THREADS_ACCESS_TOKEN,
                                       "media_type": "CAROUSEL",
                                       "children": ",".join(child_ids),
                                       "text": text}, timeout=30)
            rp.raise_for_status()
            creation_id = rp.json()["id"]

        media_id = _publish_container(uid, creation_id)

        url = "https://www.threads.net/@_nemesh_artem_"
        try:
            r3 = requests.get(f"{GRAPH}/{media_id}",
                              params={"fields": "permalink",
                                      "access_token": THREADS_ACCESS_TOKEN}, timeout=15)
            if r3.ok and "permalink" in r3.json():
                url = r3.json()["permalink"]
        except Exception:
            pass
        return True, url

    except Exception as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")  # type: ignore
        except Exception:
            detail = str(e)
        logger.error(f"Помилка публікації: {detail}")
        return False, detail


def refresh_token():
    global THREADS_ACCESS_TOKEN
    try:
        r = requests.get(f"{GRAPH}/refresh_access_token",
                         params={"grant_type": "th_refresh_token",
                                 "access_token": THREADS_ACCESS_TOKEN}, timeout=20)
        r.raise_for_status()
        new_token = r.json().get("access_token")
        if new_token:
            THREADS_ACCESS_TOKEN = new_token
            state_set("access_token", new_token)
            logger.info("Токен Threads оновлено.")
    except Exception as e:
        logger.error(f"Не вдалось оновити токен: {e}")


# ───────────────────────────────────────────────
# ПУБЛІКАЦІЯ ПОТОЧНОГО ПОСТА (загальний шлях)
# ───────────────────────────────────────────────

async def publish_pending(bot, text_override=None):
    """Публікує поточний pending-пост із зібраними фото. Чистить стан."""
    h = state_get("pending_hash")
    text = text_override if text_override is not None else state_get("pending_text")
    urls = [it["url"] for it in get_pending_images()]

    ok, res = publish_to_threads(text, urls)
    if ok:
        if h:
            mark_published(h)
        cleanup_image_files()
        clear_pending()
        suffix = f" з {len(urls)} фото" if urls else ""
        await bot.send_message(OWNER_CHAT_ID, f"✅ Опубліковано{suffix}!\n{res}")
    else:
        await bot.send_message(OWNER_CHAT_ID, f"❌ Не вийшло опублікувати:\n{res}")


# ───────────────────────────────────────────────
# TELEGRAM — клавіатури
# ───────────────────────────────────────────────

def approval_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Опублікувати", callback_data="approve"),
         InlineKeyboardButton("📷 Додати фото", callback_data="addphoto")],
        [InlineKeyboardButton("✍️ Переписати", callback_data="rewrite"),
         InlineKeyboardButton("❌ Скасувати", callback_data="cancel")],
    ])


def photos_keyboard():
    n = len(get_pending_images())
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Опублікувати з фото ({n})", callback_data="donephotos"),
        InlineKeyboardButton("❌ Скасувати", callback_data="cancel"),
    ]])


async def send_for_approval(app, text, h):
    clear_pending()
    state_set("pending_hash", h)
    state_set("pending_text", text)

    left = remaining_count()
    header = f"📝 <b>Пост на сьогодні</b>  ·  у банку лишилось: {left}\n\n"
    await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=header + text,
                               parse_mode="HTML", reply_markup=approval_keyboard())

    if left <= LOW_BANK_THRESHOLD:
        await app.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"⚠️ Банк тем майже порожній ({left} лишилось). "
                 f"Час згенерувати нові й оновити temy.md у GitHub.")


# ───────────────────────────────────────────────
# ПЛАНОВИЙ ЗАПУСК
# ───────────────────────────────────────────────

async def do_scheduled_post(app):
    if state_get("pending_hash"):
        logger.info("Пропускаю запуск: попередній пост ще не підтверджено.")
        return
    text, h = next_unpublished()
    if not text:
        await app.bot.send_message(OWNER_CHAT_ID,
            "📭 Банк тем порожній. Онови temy.md у GitHub.")
        return
    if len(text) > THREADS_MAX_LEN:
        await app.bot.send_message(OWNER_CHAT_ID,
            f"⚠️ Тема задовга ({len(text)}/{THREADS_MAX_LEN}). Скороти в банку:\n\n{text}")
        return
    await send_for_approval(app, text, h)


async def job_scheduled_post(ctx: ContextTypes.DEFAULT_TYPE):
    await do_scheduled_post(ctx.application)


async def job_refresh_token(ctx: ContextTypes.DEFAULT_TYPE):
    refresh_token()


# ───────────────────────────────────────────────
# ОБРОБКА КНОПОК
# ───────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.message.chat_id != OWNER_CHAT_ID:
        return

    action = q.data
    if not state_get("pending_hash"):
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(OWNER_CHAT_ID, "Цей пост уже оброблено.")
        return

    if action == "approve":
        await q.edit_message_reply_markup(reply_markup=None)
        await publish_pending(ctx.bot)

    elif action == "addphoto":
        if not PUBLIC_BASE_URL:
            await ctx.bot.send_message(OWNER_CHAT_ID,
                "⚠️ Фото поки недоступні: не задано PUBLIC_BASE_URL у Railway.")
            return
        state_set("await_photos", "1")
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(
            OWNER_CHAT_ID,
            f"📷 Кидай фото (до {MAX_PHOTOS}). Коли все — натисни кнопку нижче.",
            reply_markup=photos_keyboard())

    elif action == "donephotos":
        state_del("await_photos")
        await q.edit_message_reply_markup(reply_markup=None)
        if not get_pending_images():
            await ctx.bot.send_message(OWNER_CHAT_ID,
                "Фото не додано. Публікую текстом.")
        await publish_pending(ctx.bot)

    elif action == "rewrite":
        state_set("await_edit", "1")
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(OWNER_CHAT_ID,
            "✍️ Надішли свій варіант тексту наступним повідомленням.")

    elif action == "cancel":
        cleanup_image_files()
        clear_pending()
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(OWNER_CHAT_ID, "🚫 Скасовано. Тема лишилась у банку.")


# ───────────────────────────────────────────────
# ОБРОБКА ФОТО (після 📷)
# ───────────────────────────────────────────────

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    if state_get("await_photos") != "1":
        return
    items = get_pending_images()
    if len(items) >= MAX_PHOTOS:
        await update.message.reply_text(f"Вже {MAX_PHOTOS} фото, більше не можна.")
        return
    try:
        photo = update.message.photo[-1]  # найбільша якість
        f = await photo.get_file()
        name = uuid.uuid4().hex + ".jpg"
        path = os.path.join(IMAGES_DIR, name)
        await f.download_to_drive(path)
        url = f"{PUBLIC_BASE_URL}/img/{name}"
        items.append({"url": url, "path": path})
        set_pending_images(items)
        await update.message.reply_text(f"Додав фото {len(items)}.",
                                        reply_markup=photos_keyboard())
    except Exception as e:
        logger.error(f"Помилка збереження фото: {e}")
        await update.message.reply_text("Не вдалось зберегти фото, спробуй ще раз.")


# ───────────────────────────────────────────────
# ОБРОБКА ТЕКСТУ (ручний варіант після ✍️)
# ───────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    if state_get("await_edit") != "1":
        return
    new_text = update.message.text.strip()
    if len(new_text) > THREADS_MAX_LEN:
        await update.message.reply_text(
            f"⚠️ Задовго ({len(new_text)}/{THREADS_MAX_LEN}). Скороти і надішли ще раз.")
        return
    state_del("await_edit")
    await publish_pending(ctx.bot, text_override=new_text)


# ───────────────────────────────────────────────
# КОМАНДИ
# ───────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        await update.message.reply_text("Цей бот приватний.")
        return
    await update.message.reply_text(
        "Привіт! Я Threads-агент NEMESH.\n\n"
        "Команди:\n"
        "/post — запропонувати пост зараз (для тесту)\n"
        "/status — скільки тем лишилось\n"
        "/whoami — показати твій Telegram ID")


async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    await do_scheduled_post(ctx.application)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    left = remaining_count()
    pending = "так" if state_get("pending_hash") else "ні"
    photos = "так" if PUBLIC_BASE_URL else "НЕ налаштовано (PUBLIC_BASE_URL)"
    await update.message.reply_text(
        f"📊 Статус:\n"
        f"• Тем у банку: {left}\n"
        f"• Чекає підтвердження: {pending}\n"
        f"• Фото: {photos}\n"
        f"• Розклад: {POST_HOUR_1}:00 і {POST_HOUR_2}:00 (Київ)")


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твій Telegram ID: {update.effective_chat.id}")


# ───────────────────────────────────────────────
# ЗАПУСК
# ───────────────────────────────────────────────

def main():
    saved = state_get("access_token")
    if saved:
        global THREADS_ACCESS_TOKEN
        THREADS_ACCESS_TOKEN = saved

    start_img_server()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    jq.run_daily(job_scheduled_post, time=dt_time(POST_HOUR_1, 0, tzinfo=TZ))
    jq.run_daily(job_scheduled_post, time=dt_time(POST_HOUR_2, 0, tzinfo=TZ))
    jq.run_repeating(job_refresh_token, interval=30 * 24 * 3600, first=60)

    logger.info("Threads-агент (v2) запущено.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

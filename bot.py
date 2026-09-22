# ═══════════════════════════════════════════════════════════════
# NEMESH THREADS-АГЕНТ — автопостинг у Threads з підтвердженням
# v1 (тільки текст). Картинки додамо у v2.
#
# Логіка:
#   1. Читає банк тем із GitHub (сирий .md файл)
#   2. Бере наступну неопубліковану тему
#   3. Шле Артему в Telegram на підтвердження (👍 / ✍️ / ❌)
#   4. Після 👍 — публікує в Threads
#   5. Веде лічильник, попереджає коли лишилось ≤3 теми
#   6. Розклад: 2 пости/день (Київ), рознесені в часі
# ═══════════════════════════════════════════════════════════════

import logging
import os
import re
import sqlite3
import hashlib
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

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")            # новий бот від BotFather
OWNER_CHAT_ID        = int(os.getenv("OWNER_CHAT_ID", "0"))   # твій Telegram ID (428771141)
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")      # токен, який ти згенерував
BANK_URL             = os.getenv("BANK_URL", "")              # raw-посилання на temy.md у GitHub

# Час постів (Київ). Рознесені >4 год. Можна змінити через env.
POST_HOUR_1 = int(os.getenv("POST_HOUR_1", "10"))            # 10:00
POST_HOUR_2 = int(os.getenv("POST_HOUR_2", "18"))            # 18:00
TZ = ZoneInfo("Europe/Kyiv")

# Поріг попередження "банк закінчується"
LOW_BANK_THRESHOLD = 3

# Ліміт довжини поста в Threads
THREADS_MAX_LEN = 500

GRAPH = "https://graph.threads.net/v1.0"
DB_PATH = os.path.join(os.getenv("DATA_DIR", "/data"), "threads_agent.db") \
    if os.path.isdir(os.getenv("DATA_DIR", "/data")) else "threads_agent.db"

# Кеш ID користувача Threads (заповнюється при старті)
THREADS_USER_ID = None


# ───────────────────────────────────────────────
# БАЗА (пам'ять: що вже опубліковано + що зараз чекає)
# ───────────────────────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS published(
        hash TEXT PRIMARY KEY, ts TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS state(
        key TEXT PRIMARY KEY, value TEXT)""")
    return conn


def state_get(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def state_set(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
                 (key, str(value)))
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


# ───────────────────────────────────────────────
# БАНК ТЕМ (читання з GitHub)
# ───────────────────────────────────────────────

def fetch_bank():
    """Тягне banк з GitHub, повертає список текстів постів.
       Пости розділені рядком '---'. Рядки, що починаються з '#', ігноруються
       (це заголовки для навігації, не текст поста)."""
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
        lines = [ln for ln in chunk.splitlines()
                 if not ln.strip().startswith("#")]
        text = "\n".join(lines).strip()
        if text:
            posts.append(text)
    return posts


def next_unpublished():
    """Повертає (text, hash) наступного неопублікованого поста, або (None, None)."""
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
                     params={"fields": "id,username",
                             "access_token": THREADS_ACCESS_TOKEN}, timeout=20)
    r.raise_for_status()
    data = r.json()
    THREADS_USER_ID = data["id"]
    logger.info(f"Threads user: @{data.get('username')} (id={THREADS_USER_ID})")
    return THREADS_USER_ID


def publish_to_threads(text, image_url=None):
    """Публікує пост. Повертає (True, url) або (False, помилка).
       image_url — на майбутнє (v2); поки завжди None."""
    try:
        uid = get_threads_user_id()

        # Крок 1: створити контейнер
        params = {"access_token": THREADS_ACCESS_TOKEN, "text": text}
        if image_url:
            params["media_type"] = "IMAGE"
            params["image_url"] = image_url
        else:
            params["media_type"] = "TEXT"

        r1 = requests.post(f"{GRAPH}/{uid}/threads", params=params, timeout=30)
        r1.raise_for_status()
        creation_id = r1.json()["id"]

        # Крок 2: опублікувати контейнер
        r2 = requests.post(f"{GRAPH}/{uid}/threads_publish",
                           params={"access_token": THREADS_ACCESS_TOKEN,
                                   "creation_id": creation_id}, timeout=30)
        r2.raise_for_status()
        media_id = r2.json()["id"]

        # Спробувати дістати посилання на пост (не критично)
        url = f"https://www.threads.net/@_nemesh_artem_"
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
    """Оновлює довгоживучий токен (діє ~60 днів). Запускається раз на місяць."""
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
# TELEGRAM — надсилання на підтвердження
# ───────────────────────────────────────────────

def approval_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Опублікувати", callback_data="approve"),
        InlineKeyboardButton("✍️ Переписати",  callback_data="rewrite"),
        InlineKeyboardButton("❌ Скасувати",    callback_data="cancel"),
    ]])


async def send_for_approval(app, text, h):
    """Шле пост Артему на підтвердження і ставить pending."""
    state_set("pending_hash", h)
    state_set("pending_text", text)
    state_del("await_edit")

    left = remaining_count()
    header = f"📝 <b>Пост на сьогодні</b>  ·  у банку лишилось: {left}\n\n"
    await app.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=header + text,
        parse_mode="HTML",
        reply_markup=approval_keyboard()
    )

    if left <= LOW_BANK_THRESHOLD:
        await app.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"⚠️ Банк тем майже порожній ({left} лишилось). "
                 f"Час згенерувати нові й оновити файл temy.md у GitHub."
        )


# ───────────────────────────────────────────────
# ПЛАНОВИЙ ЗАПУСК ПОСТА
# ───────────────────────────────────────────────

async def do_scheduled_post(app):
    # якщо вже щось чекає підтвердження — не шлемо новий, щоб не плутати
    if state_get("pending_hash"):
        logger.info("Пропускаю запуск: попередній пост ще не підтверджено.")
        return

    text, h = next_unpublished()
    if not text:
        await app.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text="📭 Банк тем порожній — постити нічого. Онови temy.md у GitHub."
        )
        return

    if len(text) > THREADS_MAX_LEN:
        await app.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"⚠️ Тема задовга для Threads ({len(text)}/{THREADS_MAX_LEN}). "
                 f"Скороти в банку:\n\n{text}"
        )
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
    h = state_get("pending_hash")
    text = state_get("pending_text")

    if not h:
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(OWNER_CHAT_ID, "Цей пост уже оброблено.")
        return

    if action == "approve":
        ok, res = publish_to_threads(text)
        if ok:
            mark_published(h)
            state_del("pending_hash"); state_del("pending_text"); state_del("await_edit")
            await q.edit_message_reply_markup(reply_markup=None)
            await ctx.bot.send_message(OWNER_CHAT_ID, f"✅ Опубліковано!\n{res}")
        else:
            await ctx.bot.send_message(OWNER_CHAT_ID, f"❌ Не вийшло опублікувати:\n{res}")

    elif action == "rewrite":
        state_set("await_edit", "1")
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(
            OWNER_CHAT_ID,
            "✍️ Надішли свій варіант тексту наступним повідомленням — "
            "я опублікую саме його."
        )

    elif action == "cancel":
        # не постимо, тему НЕ позначаємо опублікованою (лишиться на потім)
        state_del("pending_hash"); state_del("pending_text"); state_del("await_edit")
        await q.edit_message_reply_markup(reply_markup=None)
        await ctx.bot.send_message(OWNER_CHAT_ID, "🚫 Скасовано. Тема лишилась у банку.")


# ───────────────────────────────────────────────
# ОБРОБКА ТЕКСТУ (ручний варіант після ✍️)
# ───────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    if state_get("await_edit") != "1":
        return  # не в режимі редагування — ігноруємо

    new_text = update.message.text.strip()
    if len(new_text) > THREADS_MAX_LEN:
        await update.message.reply_text(
            f"⚠️ Задовго ({len(new_text)}/{THREADS_MAX_LEN}). Скороти і надішли ще раз."
        )
        return

    ok, res = publish_to_threads(new_text)
    if ok:
        h = state_get("pending_hash")
        if h:
            mark_published(h)  # оригінальну тему вважаємо використаною
        state_del("pending_hash"); state_del("pending_text"); state_del("await_edit")
        await update.message.reply_text(f"✅ Опубліковано твій варіант!\n{res}")
    else:
        await update.message.reply_text(f"❌ Не вийшло опублікувати:\n{res}")


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
        "/post — запропонувати пост прямо зараз (для тесту)\n"
        "/status — скільки тем лишилось у банку\n"
        "/whoami — показати твій Telegram ID"
    )


async def cmd_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    await do_scheduled_post(ctx.application)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    left = remaining_count()
    pending = "так" if state_get("pending_hash") else "ні"
    await update.message.reply_text(
        f"📊 Статус:\n"
        f"• Тем у банку (неопублікованих): {left}\n"
        f"• Чекає підтвердження: {pending}\n"
        f"• Розклад: {POST_HOUR_1}:00 і {POST_HOUR_2}:00 (Київ)"
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твій Telegram ID: {update.effective_chat.id}")


# ───────────────────────────────────────────────
# ЗАПУСК
# ───────────────────────────────────────────────

def main():
    # відновити оновлений токен з бази, якщо був
    saved = state_get("access_token")
    if saved:
        global THREADS_ACCESS_TOKEN
        THREADS_ACCESS_TOKEN = saved

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Розклад через вбудований JobQueue (надійніше за окремий планувальник)
    jq = app.job_queue
    jq.run_daily(job_scheduled_post, time=dt_time(POST_HOUR_1, 0, tzinfo=TZ))
    jq.run_daily(job_scheduled_post, time=dt_time(POST_HOUR_2, 0, tzinfo=TZ))
    # Оновлення токена раз на місяць (кожні 30 днів)
    jq.run_repeating(job_refresh_token, interval=30 * 24 * 3600, first=60)

    logger.info("Threads-агент запущено.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

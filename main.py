"""
Telegram-бот для генерации ванити TRON (TRC-20) и BEP-20/ETH адресов.
- TRX: vanitygen++ (~1.5M addr/s), base58 префикс
- BEP-20: ethvanitygen (OpenSSL secp256k1 + Keccak-256), hex префикс
Поддерживает систему доступа: только авторизованные пользователи.
Хранение пользователей:
  - SQLite (по умолчанию): файл users.db в DATA_DIR или рядом с main.py
  - PostgreSQL: если задана переменная окружения DATABASE_URL (Railway Postgres)
"""

import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
MAIN_MENU, WAITING_CHAIN, WAITING_PREFIX, SEARCHING = range(4)

ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

SCRIPT_DIR = Path(__file__).parent

# Путь к SQLite: DATA_DIR (Railway Volume) → рядом с main.py
DATA_DIR = Path(os.environ.get("DATA_DIR", str(SCRIPT_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_PATH = DATA_DIR / "users.db"

# PostgreSQL: если задан DATABASE_URL (Railway Postgres addon)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# vanitygen++ для TRX
VANITYGEN_DIR = SCRIPT_DIR / "vanitygen-plusplus"
VANITYGEN_BIN = VANITYGEN_DIR / "vanitygen++"

# ethvanitygen для BEP-20
ETHVANITYGEN_BIN = SCRIPT_DIR / "ethvanitygen"

OPENSSL_LIB = "/nix/store/5xmcl9wr18g6ym3dh3363hv8hp6jyxqd-openssl-3.4.1/lib"
VANITYGEN_ENV = {
    **os.environ,
    "LD_LIBRARY_PATH": (
        f"{OPENSSL_LIB}"
        ":/nix/store/012jslm61i2424vxnzzbxqf4j9r60qzh-pcre-8.45/lib"
    ),
}

# Прогресс (одинаковый формат для обоих генераторов)
RE_PROGRESS = re.compile(
    r"\[(?P<speed>[\d.]+\s*\w+key/s)\]\[total\s+(?P<total>\d+)\]"
    r".*?\[50% in (?P<eta>[^\]]+)\]"
)


# ─── База данных пользователей ───────────────────────────────────────────────
# Поддерживает SQLite (по умолчанию) и PostgreSQL (DATABASE_URL)

def _pg():
    """Открывает соединение с PostgreSQL (только если DATABASE_URL задан)."""
    import psycopg2
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, sslmode="require")


def _sqlite():
    """Открывает соединение с SQLite."""
    return sqlite3.connect(str(SQLITE_PATH))


def _use_pg() -> bool:
    return bool(DATABASE_URL)


def init_db() -> None:
    """Создаёт таблицы при первом запуске (идемпотентно)."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                started_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("БД: PostgreSQL — таблицы готовы")
    else:
        conn = _sqlite()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                started_at TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"БД: SQLite — {SQLITE_PATH}")


def db_register_bot_user(user_id: int, username: str, first_name: str) -> None:
    """Регистрирует пользователя при /start (обновляет last_seen если уже есть)."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bot_users (user_id, username, first_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, "
            "first_name = EXCLUDED.first_name, last_seen = NOW()",
            (user_id, username, first_name),
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = _sqlite()
        conn.execute(
            "INSERT INTO bot_users (user_id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
            "first_name = excluded.first_name, last_seen = datetime('now')",
            (user_id, username, first_name),
        )
        conn.commit()
        conn.close()


def db_get_user_count() -> int:
    """Возвращает общее количество пользователей бота."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bot_users")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    else:
        conn = _sqlite()
        count = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
        conn.close()
        return count


def db_get_all_user_ids() -> list[int]:
    """Возвращает список всех user_id для рассылки."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_users")
        ids = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return ids
    else:
        conn = _sqlite()
        ids = [row[0] for row in conn.execute("SELECT user_id FROM bot_users").fetchall()]
        conn.close()
        return ids


def db_add_user(user_id: int, username: str) -> None:
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO allowed_users (user_id, username) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username",
            (user_id, username),
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = _sqlite()
        conn.execute(
            "INSERT INTO allowed_users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
            (user_id, username),
        )
        conn.commit()
        conn.close()


def db_remove_user(user_id: int) -> bool:
    """Возвращает True если пользователь был удалён."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("DELETE FROM allowed_users WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    else:
        conn = _sqlite()
        cur = conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


def db_list_users() -> list[tuple[int, str]]:
    """Возвращает список (user_id, username)."""
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT user_id, username FROM allowed_users ORDER BY added_at")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    else:
        conn = _sqlite()
        rows = conn.execute(
            "SELECT user_id, username FROM allowed_users ORDER BY added_at"
        ).fetchall()
        conn.close()
        return rows


def db_is_allowed(user_id: int) -> bool:
    if _use_pg():
        conn = _pg()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM allowed_users WHERE user_id = %s", (user_id,))
        found = cur.fetchone() is not None
        cur.close()
        conn.close()
        return found
    else:
        conn = _sqlite()
        found = conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None
        conn.close()
        return found


def is_allowed(user_id: int) -> bool:
    return True


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Найти красивый адрес", callback_data="find_address")],
        [InlineKeyboardButton("ℹ️ Как это работает?", callback_data="how_it_works")],
        [InlineKeyboardButton("⚡ Скорость поиска", callback_data="speed_info")],
    ])


def chain_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 TRON (TRC-20)", callback_data="chain_trx"),
            InlineKeyboardButton("🟡 BNB Chain (BEP-20)", callback_data="chain_bsc"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
    ])


def confirm_search_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Начать поиск", callback_data="start_search"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
        ]
    ])


def searching_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Остановить поиск", callback_data="stop_search")]
    ])


def after_success_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Искать ещё", callback_data="find_address"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
        ]
    ])


def back_to_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Назад", callback_data="main_menu")]
    ])


def try_again_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Попробовать снова", callback_data="find_address")]
    ])


# ─── Доступ ─────────────────────────────────────────────────────────────────

async def access_denied(update: Update) -> None:
    user = update.effective_user
    text = (
        "🚫 <b>Доступ закрыт.</b>\n\n"
        "Этот бот работает только для авторизованных пользователей.\n\n"
        f"Твой Telegram ID: <code>{user.id}</code>\n\n"
        "Обратись к администратору для получения доступа."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.answer("Доступ закрыт", show_alert=True)


# ─── Вспомогательные ────────────────────────────────────────────────────────

def kill_vanitygen(context: ContextTypes.DEFAULT_TYPE):
    proc = context.user_data.pop("vanitygen_proc", None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
            logger.info("Процесс генератора остановлен")
        except Exception as e:
            logger.warning(f"Не удалось остановить процесс: {e}")


def chain_label(chain: str) -> str:
    return "TRON (TRC-20)" if chain == "trx" else "BNB Chain (BEP-20)"


def chain_emoji(chain: str) -> str:
    return "🔴" if chain == "trx" else "🟡"


# ─── Обработчики ────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await access_denied(update)
        return ConversationHandler.END

    db_register_bot_user(user.id, user.username or "", user.first_name or "")
    kill_vanitygen(context)
    await update.message.reply_text(
        "👋 Привет! Я помогу найти красивый адрес кошелька с нужным префиксом.\n\n"
        "Поддерживаю TRON (TRC-20) и BNB Chain (BEP-20).\n\n"
        "Выбери действие:",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()
    kill_vanitygen(context)
    await query.edit_message_text(
        "👋 Главное меню. Выбери действие:",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def how_it_works(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "ℹ️ <b>Как это работает?</b>\n\n"
        "🔴 <b>TRON (TRC-20)</b>: адреса начинаются с <b>T</b>, затем случайные "
        "символы в base58. Бот перебирает <b>~1.5 млн адресов/сек</b>.\n\n"
        "🟡 <b>BEP-20 (BSC)</b>: адреса начинаются с <b>0x</b>, далее "
        "40 HEX-символов. Бот перебирает <b>~3–5 тыс. адресов/сек</b>.\n\n"
        "Всё происходит локально — ключи нигде не сохраняются.",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    return MAIN_MENU


async def speed_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "⚡ <b>Примерное время поиска:</b>\n\n"
        "🔴 <b>TRON (TRC-20)</b> — ~1.5 млн/сек:\n"
        "• 3 символа — секунды\n"
        "• 4 символа — 5–15 минут\n"
        "• 5 символов — часы\n"
        "• 6+ символов — дни\n\n"
        "🟡 <b>BEP-20 (BSC)</b> — ~3–5 тыс/сек:\n"
        "• 2 символа — секунды\n"
        "• 3 символа — ~1–2 минуты\n"
        "• 4 символа — ~15–30 минут\n"
        "• 5+ символов — часы/дни",
        parse_mode="HTML",
        reply_markup=back_to_main_keyboard(),
    )
    return MAIN_MENU


async def find_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()
    kill_vanitygen(context)
    await query.edit_message_text(
        "🔗 <b>Выбери блокчейн:</b>",
        parse_mode="HTML",
        reply_markup=chain_keyboard(),
    )
    return WAITING_CHAIN


async def select_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()

    chain = "trx" if query.data == "chain_trx" else "bsc"
    context.user_data["chain"] = chain

    if chain == "trx":
        hint = (
            "🔤 Введи желаемый префикс (символы после <b>T</b>).\n\n"
            "Например: <code>PAXA</code> → найду адрес типа <code>TPAXA...</code>\n\n"
            "⚠️ Допустимы только символы base58 (без O, 0, I, l)."
        )
    else:
        hint = (
            "🔤 Введи желаемый HEX-префикс (символы после <b>0x</b>).\n\n"
            "Например: <code>DEAD</code> → найду адрес типа <code>0xDEAD...</code>\n\n"
            "⚠️ Допустимы только символы 0–9 и a–f (A–F)."
        )

    await query.edit_message_text(hint, parse_mode="HTML")
    return WAITING_PREFIX


async def receive_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await access_denied(update)
        return ConversationHandler.END

    prefix = update.message.text.strip()
    chain = context.user_data.get("chain", "trx")

    if not prefix:
        await update.message.reply_text("⚠️ Введи хотя бы один символ:")
        return WAITING_PREFIX

    if chain == "trx":
        prefix = prefix.upper()
        base58_chars = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        invalid = [c for c in prefix if c not in base58_chars]
        if invalid:
            await update.message.reply_text(
                f"❌ Символы <code>{''.join(set(invalid))}</code> недопустимы.\n\n"
                f"Используй только base58-символы (без O, 0, I, l).\n\nПопробуй ещё раз:",
                parse_mode="HTML",
            )
            return WAITING_PREFIX
        if len(prefix) > 7:
            await update.message.reply_text("⚠️ Максимум 7 символов для TRX. Попробуй покороче:")
            return WAITING_PREFIX

        display = f"<code>T{prefix}</code>"
        note = ""

    else:  # BSC / BEP-20
        # Убираем 0x если ввёл
        if prefix.lower().startswith("0x"):
            prefix = prefix[2:]
        prefix = prefix.upper()
        hex_chars = set("0123456789ABCDEF")
        invalid = [c for c in prefix if c not in hex_chars]
        if invalid:
            await update.message.reply_text(
                f"❌ Символы <code>{''.join(set(invalid))}</code> недопустимы.\n\n"
                f"Используй только HEX-символы: 0–9, A–F.\n\nПопробуй ещё раз:",
                parse_mode="HTML",
            )
            return WAITING_PREFIX
        if len(prefix) > 6:
            await update.message.reply_text("⚠️ Максимум 6 символов для BEP-20. Попробуй покороче:")
            return WAITING_PREFIX

        display = f"<code>0x{prefix}...</code>"
        note = "\n\n⏱ BEP-20 поиск медленнее TRON (~3–5 тыс. адресов/сек)"

    context.user_data["prefix"] = prefix
    await update.message.reply_text(
        f"{chain_emoji(chain)} Буду искать {chain_label(chain)} адрес: {display}{note}",
        parse_mode="HTML",
        reply_markup=confirm_search_keyboard(),
    )
    return WAITING_PREFIX


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kill_vanitygen(context)
    await query.edit_message_text(
        "❌ Поиск отменён.",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()

    prefix = context.user_data.get("prefix", "")
    chain = context.user_data.get("chain", "trx")
    start_time = time.time()

    if chain == "trx":
        display_prefix = f"T{prefix}"
    else:
        display_prefix = f"0x{prefix}"

    await query.edit_message_text(
        f"🔄 {chain_emoji(chain)} Ищу <code>{display_prefix}</code>...\n\n"
        f"⚡ Скорость: запускаю...\n"
        f"Проверено адресов: <b>0</b>",
        parse_mode="HTML",
        reply_markup=searching_keyboard(),
    )

    try:
        if chain == "trx":
            proc = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", str(VANITYGEN_BIN), "-C", "TRX", f"T{prefix}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(VANITYGEN_DIR),
                env=VANITYGEN_ENV,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", str(ETHVANITYGEN_BIN), prefix,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=VANITYGEN_ENV,
            )
    except Exception as e:
        logger.error(f"Не удалось запустить генератор: {e}")
        await query.edit_message_text(
            "❌ Ошибка запуска генератора. Попробуй позже.",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU

    context.user_data["vanitygen_proc"] = proc

    context.application.create_task(
        read_vanitygen_output(proc, query, context, prefix, chain, start_time)
    )

    return SEARCHING


async def read_vanitygen_output(
    proc,
    query,
    context: ContextTypes.DEFAULT_TYPE,
    prefix: str,
    chain: str,
    start_time: float,
):
    if chain == "trx":
        display_prefix = f"T{prefix}"
        addr_tag = "TRX Address:"
        key_tag = "TRX Privkey"
    else:
        display_prefix = f"0x{prefix}"
        addr_tag = "ETH Address:"
        key_tag = "ETH Privkey:"

    address = None
    privkey = None
    last_update = 0.0
    last_msg = ""
    buf = b""

    while True:
        if context.user_data.get("vanitygen_proc") is not proc:
            proc.kill()
            return

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=1.0)
        except asyncio.TimeoutError:
            chunk = b""

        if chunk:
            buf += chunk

        while b"\r" in buf or b"\n" in buf:
            r = buf.find(b"\r")
            n = buf.find(b"\n")
            if r == -1:
                pos = n
            elif n == -1:
                pos = r
            else:
                pos = min(r, n)

            raw_line = buf[:pos]
            buf = buf[pos + 1:]
            line = raw_line.decode("utf-8", errors="replace").strip()

            if not line:
                continue

            logger.debug(f"gen: {line}")

            if line.startswith(addr_tag):
                address = line.split(":", 1)[1].strip()
                continue

            if line.startswith(key_tag):
                privkey = line.split(":", 1)[1].strip()
                continue

            m = RE_PROGRESS.search(line)
            if m and time.time() - last_update >= 3.0:
                speed = m.group("speed")
                total = int(m.group("total"))
                eta = m.group("eta")
                elapsed = time.time() - start_time

                msg = (
                    f"🔄 {chain_emoji(chain)} Ищу <code>{display_prefix}</code>...\n\n"
                    f"⚡ Скорость: <b>{speed}</b>\n"
                    f"Проверено адресов: <b>{total:,}</b>\n"
                    f"Прошло: <b>{elapsed:.0f}</b> сек  |  50% шанс через: <b>{eta}</b>"
                )
                if msg != last_msg:
                    try:
                        await query.edit_message_text(
                            msg,
                            parse_mode="HTML",
                            reply_markup=searching_keyboard(),
                        )
                        last_msg = msg
                        last_update = time.time()
                    except Exception:
                        pass

        if not chunk:
            if proc.returncode is None:
                continue
            else:
                break

    await proc.wait()

    if context.user_data.get("vanitygen_proc") is not proc:
        return

    elapsed_total = time.time() - start_time

    if address and privkey:
        if chain == "trx":
            result_text = (
                f"✅ <b>Нашёл!</b>\n\n"
                f"🔴 Сеть: <b>TRON (TRC-20)</b>\n\n"
                f"📬 Адрес: <code>{address}</code>\n"
                f"🔑 Приватный ключ: <code>{privkey}</code>\n\n"
                f"⏱ Время поиска: {elapsed_total:.1f} сек\n\n"
                f"⚠️ <b>Сохрани приватный ключ прямо сейчас. Бот его нигде не хранит.</b>"
            )
        else:
            result_text = (
                f"✅ <b>Нашёл!</b>\n\n"
                f"🟡 Сеть: <b>BNB Chain (BEP-20)</b>\n\n"
                f"📬 Адрес: <code>{address}</code>\n"
                f"🔑 Приватный ключ: <code>0x{privkey}</code>\n\n"
                f"⏱ Время поиска: {elapsed_total:.1f} сек\n\n"
                f"⚠️ <b>Сохрани приватный ключ прямо сейчас. Бот его нигде не хранит.</b>"
            )
        try:
            await query.edit_message_text(
                result_text,
                parse_mode="HTML",
                reply_markup=after_success_keyboard(),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить результат: {e}")
    else:
        try:
            await query.edit_message_text(
                "😔 Поиск завершён без результата.\n\nПопробуй снова.",
                parse_mode="HTML",
                reply_markup=try_again_keyboard(),
            )
        except Exception:
            pass

    context.user_data.pop("vanitygen_proc", None)


async def stop_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Останавливаю поиск...")
    kill_vanitygen(context)
    await asyncio.sleep(0.3)
    try:
        await query.edit_message_text(
            "🛑 Поиск остановлен.\n\nВыбери действие:",
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        pass
    return MAIN_MENU


# ─── Обработчики для незарегистрированных кнопок во время поиска ────────────

async def searching_find_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await access_denied(update)
        return ConversationHandler.END
    await query.answer()
    kill_vanitygen(context)
    await query.edit_message_text(
        "🔗 <b>Выбери блокчейн:</b>",
        parse_mode="HTML",
        reply_markup=chain_keyboard(),
    )
    return WAITING_CHAIN


# ─── Админ-команды ───────────────────────────────────────────────────────────

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("🚫 Эта команда доступна только администратору.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "ℹ️ Использование: <code>/adduser &lt;telegram_id&gt; [имя]</code>\n\n"
            "Пример: <code>/adduser 123456789 Иван</code>",
            parse_mode="HTML",
        )
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID должен быть числом.")
        return

    name = " ".join(args[1:]) if len(args) > 1 else f"user_{user_id}"
    if db_is_allowed(user_id):
        await update.message.reply_text(
            f"⚠️ Пользователь <code>{user_id}</code> уже имеет доступ.",
            parse_mode="HTML",
        )
        return

    db_add_user(user_id, name)
    await update.message.reply_text(
        f"✅ Доступ выдан: <b>{name}</b> (<code>{user_id}</code>)",
        parse_mode="HTML",
    )
    logger.info(f"Доступ выдан пользователю {user_id} ({name})")


@admin_only
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "ℹ️ Использование: <code>/removeuser &lt;telegram_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID должен быть числом.")
        return

    deleted = db_remove_user(user_id)
    if not deleted:
        await update.message.reply_text(
            f"⚠️ Пользователь <code>{user_id}</code> не найден.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"🗑 Доступ отозван: <code>{user_id}</code>",
        parse_mode="HTML",
    )
    logger.info(f"Доступ отозван у пользователя {user_id}")


@admin_only
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = db_list_users()
    if not users:
        await update.message.reply_text(
            "📋 Список пользователей пуст.\n\n"
            "Добавь: <code>/adduser &lt;telegram_id&gt; [имя]</code>",
            parse_mode="HTML",
        )
        return

    lines = [f"👥 <b>Пользователи с доступом ({len(users)}):</b>\n"]
    for uid, name in users:
        lines.append(f"• <b>{name}</b> — <code>{uid}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton("👥 Список доступа", callback_data="adm:listusers")],
    ])


def admin_confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить всем", callback_data="adm:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="adm:cancel"),
        ]
    ])


def admin_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="adm:menu")]
    ])


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("admin_state", None)
    context.user_data.pop("broadcast_text", None)
    await update.message.reply_text(
        f"🔧 <b>Панель администратора</b>\n\n"
        f"👤 Твой ID: <code>{ADMIN_ID}</code>",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


@admin_only
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "menu":
        context.user_data.pop("admin_state", None)
        context.user_data.pop("broadcast_text", None)
        await query.edit_message_text(
            f"🔧 <b>Панель администратора</b>\n\n"
            f"👤 Твой ID: <code>{ADMIN_ID}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )

    elif action == "stats":
        total = db_get_user_count()
        await query.edit_message_text(
            f"📊 <b>Статистика бота</b>\n\n"
            f"👥 Всего пользователей: <b>{total}</b>\n"
            f"(все кто нажал /start хотя бы раз)",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard(),
        )

    elif action == "listusers":
        users = db_list_users()
        if not users:
            text = "📋 Список доступа пуст."
        else:
            lines = [f"👥 <b>Список доступа ({len(users)}):</b>\n"]
            for uid, name in users:
                lines.append(f"• <b>{name}</b> — <code>{uid}</code>")
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())

    elif action == "broadcast":
        context.user_data["admin_state"] = "waiting_broadcast"
        await query.edit_message_text(
            "📢 <b>Рассылка</b>\n\n"
            "Отправь текст сообщения, который хочешь разослать всем пользователям.\n\n"
            "<i>Поддерживается HTML-форматирование: &lt;b&gt;жирный&lt;/b&gt;, "
            "&lt;i&gt;курсив&lt;/i&gt;, &lt;code&gt;код&lt;/code&gt;</i>",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard(),
        )

    elif action == "confirm":
        text = context.user_data.get("broadcast_text")
        if not text:
            await query.edit_message_text("❌ Текст рассылки не найден. Начни заново.",
                                          reply_markup=admin_back_keyboard())
            return

        user_ids = db_get_all_user_ids()
        total = len(user_ids)
        sent = 0
        failed = 0

        status_msg = await query.edit_message_text(
            f"📤 Отправляю рассылку...\n0 / {total}",
        )

        for uid in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 <b>Сообщение от администратора:</b>\n\n{text}",
                    parse_mode="HTML",
                )
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

            if (sent + failed) % 20 == 0:
                try:
                    await status_msg.edit_text(
                        f"📤 Отправляю рассылку...\n{sent + failed} / {total}"
                    )
                except Exception:
                    pass

        context.user_data.pop("admin_state", None)
        context.user_data.pop("broadcast_text", None)

        await status_msg.edit_text(
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"📨 Отправлено: <b>{sent}</b>\n"
            f"❌ Не доставлено: <b>{failed}</b>\n"
            f"👥 Всего: <b>{total}</b>",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard(),
        )

    elif action == "cancel":
        context.user_data.pop("admin_state", None)
        context.user_data.pop("broadcast_text", None)
        await query.edit_message_text(
            f"🔧 <b>Панель администратора</b>\n\n"
            f"👤 Твой ID: <code>{ADMIN_ID}</code>",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает текст от администратора когда ожидается текст рассылки."""
    if context.user_data.get("admin_state") != "waiting_broadcast":
        return

    text = update.message.text
    context.user_data["broadcast_text"] = text
    context.user_data["admin_state"] = "confirm_broadcast"

    total = db_get_user_count()
    preview = text[:300] + ("..." if len(text) > 300 else "")

    await update.message.reply_text(
        f"📢 <b>Превью рассылки</b>\n\n"
        f"<blockquote>{preview}</blockquote>\n\n"
        f"👥 Получателей: <b>{total}</b>\n\n"
        "Подтвердить отправку?",
        parse_mode="HTML",
        reply_markup=admin_confirm_keyboard(),
    )
    raise ApplicationHandlerStop


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления:", exc_info=context.error)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_TELEGRAM_ID не задан")
    if not VANITYGEN_BIN.exists():
        raise RuntimeError(f"Бинарник не найден: {VANITYGEN_BIN}")
    if not ETHVANITYGEN_BIN.exists():
        raise RuntimeError(f"Бинарник не найден: {ETHVANITYGEN_BIN}")

    init_db()

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(find_address, pattern="^find_address$"),
                CallbackQueryHandler(how_it_works, pattern="^how_it_works$"),
                CallbackQueryHandler(speed_info, pattern="^speed_info$"),
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            ],
            WAITING_CHAIN: [
                CallbackQueryHandler(select_chain, pattern="^chain_(trx|bsc)$"),
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            ],
            WAITING_PREFIX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prefix),
                CallbackQueryHandler(start_search, pattern="^start_search$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
                CallbackQueryHandler(find_address, pattern="^find_address$"),
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            ],
            SEARCHING: [
                CallbackQueryHandler(stop_search, pattern="^stop_search$"),
                CallbackQueryHandler(searching_find_address, pattern="^find_address$"),
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("admin", cmd_admin), group=0)
    application.add_handler(CommandHandler("adduser", cmd_adduser), group=0)
    application.add_handler(CommandHandler("removeuser", cmd_removeuser), group=0)
    application.add_handler(CommandHandler("listusers", cmd_listusers), group=0)
    application.add_handler(
        CallbackQueryHandler(handle_admin_callback, pattern="^adm:"),
        group=0,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID),
            handle_admin_message,
        ),
        group=0,
    )
    application.add_handler(conv_handler, group=1)
    application.add_error_handler(error_handler)

    logger.info(f"Бот запускается... Администратор: {ADMIN_ID}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

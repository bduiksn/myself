
# -*- coding: utf-8 -*-
"""
Diamond Self Bot
Single-file Telegram bot + self worker.

Run:
    python bot.py

Required:
    pip install -r requirements.txt
"""

import asyncio
import base64
import logging
import contextlib
import html
import json
import os
import random
import secrets
import tempfile
import shutil
import zipfile
import re
import sqlite3
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, Button, functions, types, utils
from telethon.tl.functions.messages import (
    SetTypingRequest,
    SendReactionRequest,
    TranslateTextRequest,
    GetInlineBotResultsRequest,
    SendInlineBotResultRequest,
    SendMessageRequest,
)
from telethon.tl.types import SendMessageTypingAction, ReactionEmoji, TextWithEntities
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
    MessageNotModifiedError,
)

# ============================================================
# CONFIG
# ============================================================

API_ID = 32955870
API_HASH = "a40ba705a967c3c8e490f4684f42256a"

BOT_TOKEN = "8895638922:AAFrlYVGEW3t5WbjzeXFgcWcajToeVK02v0"

ADMINS = [7727625618]

CARD_NUMBER = "5022291579049451"
CARD_HOLDER = "علی محمدی پور"

SELF_HOURLY_COST = 2.5
SELF_CLOCK_UPDATE_INTERVAL = 1
MIN_SELF_BALANCE = 100
MIN_DIAMOND_PURCHASE = 500
DIAMOND_PRICE_TOMAN = 40

# Local, free speech-to-text configuration.
# No API key is required. The Persian STT engine is faster-whisper + Whisper large-v3.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "fa")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv(
    "WHISPER_COMPUTE_TYPE",
    "int8" if WHISPER_DEVICE == "cpu" else "float16",
)
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "8"))
WHISPER_BEST_OF = int(os.getenv("WHISPER_BEST_OF", "8"))
WHISPER_PATIENCE = float(os.getenv("WHISPER_PATIENCE", "1.2"))

# Free providers for currency and local media features
# Currency uses CoinMarketCap's current keyless public endpoint first and
# CoinGecko's public endpoint as a fallback. No API key is required.
CMC_PUBLIC_BASE_URL = "https://pro-api.coinmarketcap.com/public-api"
COINGECKO_PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
CRYPTO_PROVIDER_TIMEOUT = 12

TRANSFER_TAX = 0.10
GAME_TAX = 0.10
GAME_TIMEOUT = 300

# The BOT-side game/balance features are available only in the official group.
# This restriction does NOT affect the logged-in SELF workers.
OFFICIAL_GROUP_USERNAME = "DimondSelfGap"
OFFICIAL_GROUP_ID = None
BOT_ENABLED_KEY = "bot_enabled"
BOT_UPDATE_TEXT = "ربات درحال آپدیت هست ، لطفاً منتظر بمونید!"
REFERRAL_REWARD = 25
MIN_GAME = 20
MIN_TRANSFER = 10

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "database_users"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOT_IMAGE_PATH = BASE_DIR / "1782502761872.jpg"

# ============================================================
# DATABASE
# ============================================================

def db_path(user_id: int) -> Path:
    return DATA_DIR / f"user_{int(user_id)}.db"


def connect_db(user_id: int):
    conn = sqlite3.connect(db_path(user_id), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_user_db(user_id: int):
    with connect_db(user_id) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                invited_by INTEGER NOT NULL DEFAULT 0,
                referral_reward_claimed INTEGER NOT NULL DEFAULT 0,
                self_start_time INTEGER NOT NULL DEFAULT 0,
                self_enabled INTEGER NOT NULL DEFAULT 0,
                phone_number TEXT
            )
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "phone_number" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS self_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_string TEXT NOT NULL,
                sub_type INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                start_time INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER PRIMARY KEY,
                reward_claimed INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            INSERT OR IGNORE INTO users
            (user_id, balance, banned, invited_by, referral_reward_claimed,
             self_start_time, self_enabled, phone_number)
            VALUES (?, 0, 0, 0, 0, 0, 0, NULL)
        """, (user_id,))


def get_balance(user_id: int) -> float:
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return float(row[0]) if row else 0.0


def _fmt_diamonds(value) -> str:
    value = float(value or 0)
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def get_phone_number(user_id: int):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute("SELECT phone_number FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] else None


def normalize_iran_phone(value: str):
    if not value:
        return None
    value = str(value).strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    value = re.sub(r"[\s()\-]", "", value)
    if value.startswith("0098"):
        value = "+98" + value[4:]
    elif value.startswith("09"):
        value = "+98" + value[1:]
    elif value.startswith("98"):
        value = "+" + value
    if not re.fullmatch(r"\+989\d{9}", value):
        return None
    return value


def save_phone_number(user_id: int, phone: str):
    normalized = normalize_iran_phone(phone)
    if not normalized:
        return False
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute("UPDATE users SET phone_number=? WHERE user_id=?", (normalized, user_id))
    return True


def has_registered_phone(user_id: int) -> bool:
    return bool(get_phone_number(user_id))


async def send_phone_request(user_id: int):
    await bot.send_message(
        user_id,
        "📱 برای ادامه، شماره موبایل ایران خودت را از دکمه زیر به اشتراک بگذار.",
        buttons=[[Button.request_phone("📱 اشتراک‌گذاری شماره", resize=True)]],
    )


def change_balance(user_id: int, amount: float):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (float(amount), user_id)
        )


def is_banned(user_id: int) -> bool:
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute(
            "SELECT banned FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return bool(row and row[0])


def set_banned(user_id: int, value: bool):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute(
            "UPDATE users SET banned=? WHERE user_id=?",
            (1 if value else 0, user_id)
        )


def get_setting(user_id: int, key: str, default=None):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
        return row[0] if row else default


def set_setting(user_id: int, key: str, value):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, str(value))
        )


def get_active_session(user_id: int):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute("""
            SELECT session_string, sub_type, start_time
            FROM self_sessions
            WHERE is_active=1
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()
        return row


def save_active_session(user_id: int, session_string: str, sub_type: int):
    now = int(time.time())
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute("UPDATE self_sessions SET is_active=0")
        db.execute("""
            INSERT INTO self_sessions
            (session_string, sub_type, is_active, start_time)
            VALUES (?, ?, 1, ?)
        """, (session_string, sub_type, now))
        db.execute("""
            UPDATE users
            SET self_start_time=?, self_enabled=1
            WHERE user_id=?
        """, (now, user_id))


def deactivate_session(user_id: int):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute("UPDATE self_sessions SET is_active=0")
        db.execute("""
            UPDATE users
            SET self_start_time=0, self_enabled=0
            WHERE user_id=?
        """, (user_id,))


def all_active_sessions():
    result = []
    for file in DATA_DIR.glob("user_*.db"):
        match = re.fullmatch(r"user_(\d+)\.db", file.name)
        if not match:
            continue
        user_id = int(match.group(1))
        try:
            row = get_active_session(user_id)
            if row:
                result.append((user_id, row[0], int(row[1])))
        except Exception as exc:
            print(f"[DB] failed to load {user_id}: {exc}")
    return result


def total_diamonds_in_circulation() -> float:
    """Return the sum of all user balances stored in database_users."""
    total = 0.0
    for file in DATA_DIR.glob("user_*.db"):
        match = re.fullmatch(r"user_(\d+)\.db", file.name)
        if not match:
            continue
        user_id = int(match.group(1))
        try:
            init_user_db(user_id)
            with connect_db(user_id) as db:
                row = db.execute("SELECT COALESCE(balance, 0) FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row:
                    total += float(row[0] or 0)
        except Exception as exc:
            print(f"[DB] failed to sum balance for {user_id}: {exc}")
    return total


# ============================================================
# BOT STATE
# ============================================================

bot = TelegramClient(
    str(BASE_DIR / "diamond_bot"),
    API_ID,
    API_HASH
)

pending = {}
purchase_state = {}
active_games = {}
self_workers = {}
self_clients = {}
_self_reply_cache = set()
_secretary_reply_cache = {}
_spam_tasks = {}

_inline_bot_cache = {}
_cleanup_tasks = {}
_cleanup_panel_messages = {}

# Channel-save is deliberately isolated: one in-memory session and one worker
# per owner. The progress message is always the exact same panel message.
_channel_save_sessions = {}
_channel_save_tasks = {}
_first_comment_ui_target = {}
_first_comment_channel_sessions = {}
_first_comment_sent_cache = set()
# Independent state for media conversions; never shares channel-save state.
media_convert_state = {}
# Independent state for voice -> text jobs.  Kept separate from media conversion
# so a slow Whisper/OpenAI request can never leave a stale "processing" flag.
stt_state = {}
# Last five incoming private messages per chat.  Used only as a short-lived
# deletion snapshot so a user-cleared chat can be archived without scanning
# the whole conversation.
_deleted_message_cache = {}
# MessageDeleted updates for private chats do not reliably carry the peer/chat id.
# Keep a tiny message-id -> chat index so an immediately deleted message can
# still be mapped back to the correct private conversation.
_deleted_message_index = {}



# ============================================================
# INLINE BUTTON STYLE COMPATIBILITY
# ============================================================

def _button_style(text: str, data: bytes = b"") -> str:
    """Semantic style selection: success=positive, primary=management, danger=destructive."""
    label = (text or "").casefold()
    raw = data.decode("utf-8", errors="ignore").casefold() if isinstance(data, (bytes, bytearray)) else str(data).casefold()
    danger_words = ("لغو", "حذف", "توقف", "خاموش", "خروج", "رد", "مسدود", "بستن", "پاک", "disable", "delete", "stop", "cancel", "close", "reject")
    success_words = ("تأیید", "تایید", "خرید", "ارسال", "اجرا", "فعال", "پیوستن", "پرداخت", "ساخت", "روشن", "refresh", "update", "join", "confirm", "send", "enable")
    if any(w in label or w in raw for w in danger_words):
        return "danger"
    if any(w in label or w in raw for w in success_words):
        return "success"
    return "primary"


def btn(text: str, data, style: str | None = None):
    """Real Telegram inline button with safe fallback for old Telethon/API versions."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    chosen = style or _button_style(text, data)
    # Back/cancel buttons are always red/danger, regardless of caller style.
    if any(w in (text or '').casefold() for w in ('بازگشت', 'برگشت', 'لغو', 'cancel', 'back')):
        chosen = 'danger'
    try:
        return Button.inline(text, data, style=chosen)
    except Exception:
        return Button.inline(text, data)


# ============================================================
# FORCE JOIN + BACKUP
# ============================================================

FORCE_JOIN_KEY = "force_join_channels"
SUPPORT_USERNAME = "HusteRIX"
BACKUP_PREFIX = "husterix_backup_"
MAX_BACKUP_BYTES = 100 * 1024 * 1024


def _admin_state(key: str, default=None):
    return get_setting(int(ADMINS[0]), key, default) if ADMINS else default


def _set_admin_state(key: str, value):
    if ADMINS:
        set_setting(int(ADMINS[0]), key, value)


def is_bot_enabled() -> bool:
    """Return whether the BOT-side features are enabled. SELF workers are independent."""
    return str(_admin_state(BOT_ENABLED_KEY, "on")).lower() == "on"


def set_bot_enabled(enabled: bool):
    _set_admin_state(BOT_ENABLED_KEY, "on" if enabled else "off")


async def resolve_official_group_id():
    """Resolve the official group once after the BOT client is connected."""
    global OFFICIAL_GROUP_ID
    try:
        entity = await bot.get_entity("@" + OFFICIAL_GROUP_USERNAME)
        OFFICIAL_GROUP_ID = int(utils.get_peer_id(entity, add_mark=True))
        print(f"[BOT GROUP] Official group @{OFFICIAL_GROUP_USERNAME} -> {OFFICIAL_GROUP_ID}")
    except Exception as exc:
        OFFICIAL_GROUP_ID = None
        print(f"[BOT GROUP] Failed to resolve @{OFFICIAL_GROUP_USERNAME}: {exc}")
    return OFFICIAL_GROUP_ID


async def is_official_group_event(event) -> bool:
    """True only for messages/callbacks originating from the official group."""
    global OFFICIAL_GROUP_ID
    if not (getattr(event, "is_group", False) or getattr(event, "is_channel", False)):
        return False
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return False
    if OFFICIAL_GROUP_ID is None:
        await resolve_official_group_id()
    return OFFICIAL_GROUP_ID is not None and int(chat_id) == int(OFFICIAL_GROUP_ID)


async def cancel_all_active_games_for_update():
    """Cancel pending BOT games safely when the BOT is put into update mode."""
    games = list(active_games.items())
    active_games.clear()
    for (chat_id, message_id), game in games:
        organizer = int(game.get("organizer", 0))
        amount = int(game.get("amount", 0))
        task = game.get("task")
        if task:
            task.cancel()
        if organizer and amount > 0:
            change_balance(organizer, amount)
        with contextlib.suppress(Exception):
            await bot.delete_messages(chat_id, message_id)
        if organizer:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    organizer,
                    f"❌ بازی به دلیل فعال شدن حالت آپدیت لغو شد.\n💎 {amount:,} الماس به حساب شما برگشت."
                )


def get_force_join_channels():
    try:
        raw = json.loads(_admin_state(FORCE_JOIN_KEY, "[]"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def save_force_join_channels(channels):
    _set_admin_state(FORCE_JOIN_KEY, json.dumps(channels, ensure_ascii=False))


def _channel_url(channel):
    username = str(channel.get("username") or "").lstrip("@")
    if username:
        return f"https://t.me/{username}"
    return channel.get("url") or ""


async def _is_joined_channel(user_id: int, channel):
    try:
        entity_ref = channel.get("username") or channel.get("id")
        entity = await bot.get_entity(entity_ref)
        participant = await bot(functions.channels.GetParticipantRequest(
            channel=entity,
            participant=await bot.get_input_entity(user_id),
        ))
        return isinstance(
            getattr(participant, "participant", None),
            (types.ChannelParticipant, types.ChannelParticipantAdmin, types.ChannelParticipantCreator)
        )
    except Exception:
        # A bot that cannot verify a channel must fail closed for force-join.
        return False


async def get_missing_force_joins(user_id: int):
    missing = []
    for channel in get_force_join_channels():
        if not await _is_joined_channel(user_id, channel):
            missing.append(channel)
    return missing


def force_join_buttons(channels):
    rows = []
    for channel in channels:
        url = _channel_url(channel)
        title = str(channel.get("title") or channel.get("username") or "کانال")
        if url:
            rows.append([Button.url(f"📢 {title}", url)])
    rows.append([btn("🟢 عضو شدم، ادامه", b"fj_check", "success")])
    return rows


async def show_force_join(event, channels=None):
    channels = channels if channels is not None else get_force_join_channels()
    if not channels:
        return False
    link_lines = []
    for channel in channels:
        title = html.escape(str(channel.get("title") or channel.get("username") or "کانال"))
        url = _channel_url(channel)
        if url:
            link_lines.append(f'• <a href="{html.escape(url, quote=True)}">📢 {title}</a>')
        else:
            link_lines.append(f"• {title}")
    text = (
        "🔐 <b>عضویت اجباری</b>\\n\\n"
        "برای ادامه، روی نام هر کانال/گروه زیر بزن و عضو شو:\\n\\n"
        + "\\n".join(link_lines)
        + "\\n\\nبعد از عضویت روی «🟢 عضو شدم، ادامه» بزن."
    )
    await edit_or_send(event, text, force_join_buttons(channels))
    return True


async def ensure_force_join(user_id: int, event=None):
    if user_id in ADMINS:
        return True
    missing = await get_missing_force_joins(user_id)
    if not missing:
        return True
    if event is not None:
        await show_force_join(event, missing)
    else:
        await bot.send_message(
            user_id,
            "🔐 برای ادامه ابتدا عضو کانال‌های اجباری شوید.",
            buttons=force_join_buttons(missing)
        )
    return False


def _safe_backup_member(name: str):
    p = Path(name)
    return not p.is_absolute() and ".." not in p.parts


def _backup_sqlite_database(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source, timeout=30)
    dest_conn = sqlite3.connect(destination, timeout=30)
    try:
        source_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        source_conn.backup(dest_conn)
        dest_conn.commit()
    finally:
        dest_conn.close()
        source_conn.close()


def create_backup_sync():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_root = Path(tempfile.mkdtemp(prefix="husterix_backup_build_"))
    archive = BASE_DIR / f"{BACKUP_PREFIX}{stamp}.zip"
    try:
        db_copy = temp_root / "database_users"
        db_copy.mkdir(parents=True, exist_ok=True)
        for source in DATA_DIR.glob("user_*.db"):
            if source.is_file():
                _backup_sqlite_database(source, db_copy / source.name)

        media_source = BASE_DIR / "banner_media"
        media_copy = temp_root / "banner_media"
        if media_source.is_dir():
            shutil.copytree(media_source, media_copy)

        manifest = {
            "format": 2,
            "created_at": datetime.now().isoformat(),
            "database_dir": "database_users",
            "banner_media_dir": "banner_media",
            "force_join_channels": get_force_join_channels(),
        }
        (temp_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in temp_root.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(temp_root).as_posix())
        return archive
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def inspect_backup_sync(archive_path: Path):
    if archive_path.stat().st_size > MAX_BACKUP_BYTES:
        raise RuntimeError("backup_too_large")
    with zipfile.ZipFile(archive_path, "r") as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise RuntimeError("backup_manifest_missing")
        if not all(_safe_backup_member(n) for n in names):
            raise RuntimeError("backup_path_traversal")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if int(manifest.get("format", 0)) not in {1, 2}:
            raise RuntimeError("backup_format")
        if not any(n.startswith("database_users/") for n in names):
            raise RuntimeError("backup_database_missing")
        if int(manifest.get("format", 1)) >= 2 and not any(n.startswith("banner_media/") for n in names):
            # banner_media is optional when there were no banner files; the DB remains authoritative.
            if manifest.get("banner_media_dir") != "banner_media":
                raise RuntimeError("backup_media_manifest")
        return manifest


def restore_backup_sync(archive_path: Path):
    manifest = inspect_backup_sync(archive_path)
    restore_root = Path(tempfile.mkdtemp(prefix="husterix_restore_"))
    old_root = BASE_DIR / f".database_users_old_{secrets.token_hex(6)}"
    old_media = BASE_DIR / f".banner_media_old_{secrets.token_hex(6)}"
    media_replaced = False
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(restore_root)
        extracted = restore_root / "database_users"
        if not extracted.is_dir():
            raise RuntimeError("backup_database_missing")

        extracted_media = restore_root / "banner_media"
        os.replace(DATA_DIR, old_root)
        os.replace(extracted, DATA_DIR)

        if extracted_media.is_dir():
            current_media = BASE_DIR / "banner_media"
            if current_media.exists():
                os.replace(current_media, old_media)
            os.replace(extracted_media, current_media)
            media_replaced = True

        fj = manifest.get("force_join_channels")
        if isinstance(fj, list):
            save_force_join_channels(fj)

        shutil.rmtree(old_root, ignore_errors=True)
        if media_replaced:
            shutil.rmtree(old_media, ignore_errors=True)
        return manifest
    except Exception:
        if not DATA_DIR.exists() and old_root.exists():
            os.replace(old_root, DATA_DIR)
        if media_replaced:
            current_media = BASE_DIR / "banner_media"
            if current_media.exists():
                shutil.rmtree(current_media, ignore_errors=True)
            if old_media.exists():
                os.replace(old_media, current_media)
        raise
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)


async def stop_all_self_workers_for_backup():
    users = list(self_clients.keys())
    for uid in users:
        with contextlib.suppress(Exception):
            deactivate_session(uid)
        task = self_workers.get(uid)
        if task:
            task.cancel()
        client = self_clients.get(uid)
        if client:
            with contextlib.suppress(Exception):
                await client.disconnect()
    await asyncio.sleep(0.2)
    self_workers.clear()
    self_clients.clear()


# ============================================================
# UI
# ============================================================

def main_buttons(user_id: int):
    rows = [
        [btn("💎 خرید سلف", b"buy_self", "success")],
        [
            btn("⚙️ مدیریت سلف", b"manage_self", "primary"),
            btn("👤 حساب کاربری", b"user_account", "primary"),
        ],
        [
            btn("👥 زیرمجموعه‌گیری", b"referral_system", "primary"),
            Button.url("🆘 پشتیبانی", "https://t.me/HusteRIX", style="primary"),
        ],
    ]
    if user_id in ADMINS:
        rows.append([btn("🛠 مدیریت", b"admin_panel", "primary")])
    return rows


async def send_main(target, user_id: int, text="**به سلـف‌ساز 𝗛𝘂𝘀𝘁𝗲𝗥𝗜𝗫 𝗗𝗶𝗺𝗼𝗻𝗱 𝗦𝗲𝗹𝗳 خوش آمدید! 💎**\n\nبرای ساخت سلـف‌ربات، خرید الماس یا دریافت پاداش زیرمجـموعه‌گـیری، لطـفاً یکی از گزینـه‌های منوی زیر را انتـخاب کنـید:"):
    buttons = main_buttons(user_id)
    if BOT_IMAGE_PATH.exists():
        return await bot.send_file(
            target,
            BOT_IMAGE_PATH,
            caption=text,
            buttons=buttons
        )
    return await bot.send_message(target, text, buttons=buttons)


async def safe_answer(event, text="", alert=False):
    with contextlib.suppress(Exception):
        await event.answer(text, alert=alert)


async def _with_timeout(coro, timeout=10):
    return await asyncio.wait_for(coro, timeout=float(timeout))


async def edit_or_send(event, text, buttons=None):
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await bot.send_message(event.sender_id, text, buttons=buttons)


async def user_name(user_id: int):
    try:
        entity = await bot.get_entity(user_id)
        if getattr(entity, "username", None):
            return f"@{entity.username}"
        name = getattr(entity, "first_name", None) or "کاربر"
        return name[:25]
    except Exception:
        return f"`{user_id}`"


# ============================================================
# SELF FEATURES / SELF PANEL
# ============================================================

SELF_CLOCK_FONTS = {
    "normal":"0123456789","bold":"𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟳𝟖𝟗","double":"𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡",
    "sans":"𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫","sans_bold":"𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵",
    "mono":"𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿","full":"０１２３４５６７８９",
    "circled":"⓪①②③④⑤⑥⑦⑧⑨","negative":"⓿❶❷❸❹❺❻❼❽❾",
    "superscript":"⁰¹²³⁴⁵⁶⁷⁸⁹","subscript":"₀₁₂₃₄₅₆₇₈₉",
    "persian":"۰۱۲۳۴۵۶۷۸۹","arabic":"٠١٢٣٤٥٦٧٨٩","devanagari":"०१२३४५६७८९",
}
SELF_FONT_ALIASES = {
    "عادی":"normal","بولد":"bold","دوبل":"double","سانس":"sans","سانس بولد":"sans_bold",
    "مونو":"mono","فول":"full","دایره":"circled","منفی":"negative",
    "بالانویس":"superscript","زیرنویس":"subscript","فارسی":"persian","عربی":"arabic","هندی":"devanagari"
}
SELF_ENGLISH_FONTS = {
    "normal": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"),
    "bold": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇"),
    "italic": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝘈𝘉𝘊𝘋𝘌𝘍𝘎𝘏𝘐𝘑𝘒𝘓𝘔𝘕𝘖𝘗𝘘𝘙𝘚𝘛𝘜𝘝𝘞𝘟𝘠𝘡𝘢𝘣𝘤𝘥𝘦𝘧𝘨𝘩𝘪𝘫𝘬𝘭𝘮𝘯𝘰𝘱𝘲𝘳𝘴𝘵𝘶𝘷𝘸𝘹𝘺𝘻"),
    "bold_italic": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝑨𝑩𝑪𝑫𝑬𝑭𝑮𝑯𝑰𝑱𝑲𝑳𝑴𝑵𝑶𝑷𝑸𝑹𝑺𝑻𝑼𝑽𝑾𝑿𝒀𝒁𝒂𝒃𝒄𝒅𝒆𝒇𝒈𝒉𝒊𝒋𝒌𝒍𝒎𝒏𝒐𝒑𝒒𝒓𝒔𝒕𝒖𝒗𝒘𝒙𝒚𝒛"),
    "monospace": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣"),
    "double": str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫"),
}
SELF_ENGLISH_FONT_ALIASES = {"عادی":"normal","بولد":"bold","ایتالیک":"italic","بولد ایتالیک":"bold_italic","مونو":"monospace","دوبل":"double"}
SELF_DEFAULTS = {
    "time_name":"on", "clock_font":"normal", "bold":"off", "persian_font":"off", "english_font":"normal",
    "translate":"off", "auto_reply":"off", "auto_reply_text":"سلام، فعلاً در دسترس نیستم.", "auto_read":"off",
    "typing":"off", "game_mode":"off", "reaction_targets":"[]", "reaction_emojis":"{}",
}
STRETCH_MAP = dict(zip("بپتثجچحخدذرزژسشصضطظعغفقکگلمنهیيك", "بـپـتـثـجـچـحـخـدـذـرـزـژـسـشـصـضـطـظـعـغـفـقـکـگـلـمـنـهـیـيـكـ"))

def self_get(uid, key, default=None):
    return get_setting(uid, key, SELF_DEFAULTS.get(key) if default is None else default)

def self_set(uid, key, value):
    set_setting(uid, key, value)

def self_auto_reply_map(uid):
    try:
        raw = json.loads(self_get(uid, "auto_reply_keywords", "{}"))
        return {str(k).casefold(): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}

def self_save_auto_reply_map(uid, mapping):
    self_set(uid, "auto_reply_keywords", json.dumps(mapping, ensure_ascii=False))

def self_banners(uid):
    """Load and normalize persisted banners."""
    try:
        raw = json.loads(self_get(uid, "banners", "[]"))
        if not isinstance(raw, list):
            return []
    except Exception:
        return []
    normalized = []
    changed = False
    for item in raw:
        if not isinstance(item, dict):
            changed = True
            continue
        try:
            item["id"] = int(item.get("id", 0))
            if item["id"] < 1:
                changed = True
                continue
            item["interval"] = max(1, int(item.get("interval", 60)))
            clean_targets = set()
            for raw_target in (item.get("targets") or []):
                try:
                    target_id = int(raw_target)
                    if target_id != 0:
                        clean_targets.add(target_id)
                except (TypeError, ValueError):
                    changed = True
            item["targets"] = sorted(clean_targets)
            item["enabled"] = item.get("enabled", True) not in {False, 0, "0", "off", "false"}
            item["last_sent"] = float(item.get("last_sent", 0) or 0)
        except (TypeError, ValueError):
            changed = True
            continue
        normalized.append(item)
    if changed:
        self_set(uid, "banners", json.dumps(normalized, ensure_ascii=False))
    return normalized

def self_save_banners(uid, banners):
    self_set(uid, "banners", json.dumps(banners, ensure_ascii=False))

def _next_banner_id(banners):
    return max([int(b.get("id", 0)) for b in banners] or [0]) + 1

def _banner_from_list(banners, banner_id):
    for banner in banners:
        try:
            if int(banner.get("id", 0)) == int(banner_id):
                return banner
        except (TypeError, ValueError):
            continue
    return None

def _banner_by_id(uid, banner_id):
    return _banner_from_list(self_banners(uid), banner_id)

def _banner_media_dir(uid):
    path = BASE_DIR / "banner_media" / str(int(uid))
    path.mkdir(parents=True, exist_ok=True)
    return path

def _get_tabchi_client(uid):
    """Return only the SELF client assigned to this user for Tabchi operations."""
    return self_clients.get(int(uid))


async def _banner_send(client, uid, banner, target):
    # Never trust the caller-provided client for Tabchi sends.
    self_client = _get_tabchi_client(uid)
    if not self_client:
        raise RuntimeError("SELF client is not active")
    if banner.get("mode", "forward") == "forward":
        return await self_client.forward_messages(
            target, int(banner["source_msg_id"]),
            from_peer=int(banner["source_chat_id"])
        )
    media_path = banner.get("media_path")
    caption = banner.get("text") or ""
    if media_path and Path(media_path).exists():
        return await self_client.send_file(target, media_path, caption=caption)
    return await self_client.send_message(target, caption)

async def _banner_recent_pv(client, count):
    if not client:
        raise RuntimeError("SELF client is not active")
    result = []
    me = await client.get_me()
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not entity or getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False):
            continue
        if getattr(entity, "bot", False) or getattr(entity, "id", None) == me.id:
            continue
        result.append(entity)
        if len(result) >= int(count):
            break
    return result

async def _banner_dispatch_now(client, uid, banner, targets):
    client = _get_tabchi_client(uid)
    if not client:
        raise RuntimeError("SELF client is not active")
    sent = failed = 0
    for target in targets:
        try:
            await _banner_send(client, uid, banner, target)
            sent += 1
        except Exception as exc:
            failed += 1
            print(f"[BANNER {uid}] send {banner.get('id')} -> {getattr(target, 'id', target)} failed: {exc}")
    return sent, failed

async def _banner_dispatch_configured_now(client, uid, banner):
    """Send one configured banner immediately and record the send time.

    The caller may be the self worker or the exact client that received the
    command.  Keeping this function independent of self_clients avoids a
    race where the command arrives while the worker is still registering.
    """
    client = _get_tabchi_client(uid)
    if not client:
        raise RuntimeError("SELF client is not active")

    if not banner or not banner.get("enabled", True):
        return 0, 0

    target_ids = []
    for raw_gid in banner.get("targets", []):
        try:
            gid = int(raw_gid)
        except (TypeError, ValueError):
            print(f"[BANNER {uid}] invalid target id: {raw_gid!r}")
            continue
        if gid == 0:
            continue
        if gid not in target_ids:
            target_ids.append(gid)
    banner["targets"] = target_ids

    if not target_ids:
        return 0, 0

    targets = []
    for gid in target_ids:
        try:
            targets.append(await client.get_entity(gid))
        except Exception as exc:
            print(f"[BANNER {uid}] target resolve failed for {gid}: {exc}")

    if not targets:
        return 0, len(target_ids)

    sent, failed = await _banner_dispatch_now(client, uid, banner, targets)
    if sent:
        banner["last_sent"] = time.time()
    return sent, failed + max(0, len(target_ids) - len(targets))


async def _banner_dispatch_all_configured(client, uid):
    """Immediately send every configured banner that has at least one target."""
    client = _get_tabchi_client(uid)
    if not client:
        raise RuntimeError("SELF client is not active")
    banners = self_banners(uid)
    total_sent = total_failed = 0
    changed = False
    for banner in banners:
        if not banner.get("enabled", True) or not banner.get("targets"):
            continue
        sent, failed = await _banner_dispatch_configured_now(client, uid, banner)
        total_sent += sent
        total_failed += failed
        if sent:
            changed = True
    if changed:
        self_save_banners(uid, banners)
    return total_sent, total_failed


async def _banner_worker_tick(client, uid):
    client = _get_tabchi_client(uid)
    if not client:
        raise RuntimeError("SELF client is not active")
    if self_get(uid, "banner_auto", "off") != "on":
        return
    now = time.time()
    banners = self_banners(uid)
    changed = False
    for banner in banners:
        if not banner.get("enabled", True):
            continue
        interval = max(1, int(banner.get("interval", 60))) * 60
        last_sent = float(banner.get("last_sent", 0) or 0)
        if last_sent and now - last_sent < interval:
            continue
        sent, _ = await _banner_dispatch_configured_now(client, uid, banner)
        if sent:
            changed = True
    if changed:
        self_save_banners(uid, banners)


def self_reaction_targets(uid):
    try:
        return {int(x) for x in json.loads(self_get(uid, "reaction_targets", "[]"))}
    except Exception:
        return set()

def self_save_reaction_targets(uid, targets):
    self_set(uid, "reaction_targets", json.dumps(sorted(int(x) for x in targets)))

def self_reaction_map(uid):
    try:
        raw = json.loads(self_get(uid, "reaction_emojis", "{}"))
        return {int(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}

def self_set_reaction(uid, target_id, emoji):
    mapping = self_reaction_map(uid)
    mapping[int(target_id)] = emoji
    self_set(uid, "reaction_emojis", json.dumps(mapping, ensure_ascii=False))

def self_remove_reaction(uid, target_id):
    mapping = self_reaction_map(uid)
    mapping.pop(int(target_id), None)
    self_set(uid, "reaction_emojis", json.dumps(mapping, ensure_ascii=False))

def self_clock(uid):
    now = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%H:%M")
    chars = SELF_CLOCK_FONTS.get(self_get(uid, "clock_font", "normal"), SELF_CLOCK_FONTS["normal"])
    return now.translate(str.maketrans("0123456789", chars))


def _clock_suffix_pattern():
    # All digit alphabets used by the clock-font selector.  This lets us
    # replace a previously formatted clock instead of accumulating old
    # Unicode digits in the profile name.
    digit_chars = "".join(SELF_CLOCK_FONTS.values())
    return re.compile(rf"\s*[{re.escape(digit_chars)}]{{1,2}}:[{re.escape(digit_chars)}]{{2}}\s*$")


_CLOCK_SUFFIX_RE = _clock_suffix_pattern()

def _clean_clock_suffix(name: str) -> str:
    return _CLOCK_SUFFIX_RE.sub("", name or "").strip()

def self_transform_english(text, uid):
    return text.translate(SELF_ENGLISH_FONTS.get(self_get(uid,"english_font","normal"), SELF_ENGLISH_FONTS["normal"]))

def self_stretch(text):
    if not text:
        return text
    non_joining_right = set("اآأإدذرزژوؤء")
    persian = set("بپتثجچحخسشصضطظعغفقکگلمنهىيیک")
    out = []
    for piece in re.split(r"(\s+)", text):
        if not piece or piece.isspace() or len(piece) < 4:
            out.append(piece)
            continue
        chars = list(piece)
        candidates = [i for i in range(len(chars)-1)
                      if chars[i] in persian and chars[i+1] in persian and chars[i] not in non_joining_right]
        selected = set(candidates[1::2][:max(1, min(2, len(chars)//5))])
        for i,ch in enumerate(chars):
            out.append(ch)
            if i in selected:
                out.append("ـ")
    return "".join(out)[:3900]

async def self_translate(client, text):
    if not text.strip():
        return None
    try:
        result = await client(TranslateTextRequest(
            to_lang="en",
            text=[TextWithEntities(text=text, entities=[])],
        ))
        values = getattr(result, "result", None)
        if values:
            translated = getattr(values[0], "text", None)
            if translated:
                return translated.strip()
    except Exception as exc:
        print(f"[SELF] Telegram translation failed: {exc}")
    try:
        import argostranslate.translate as argos
        result = argos.translate(text, "fa", "en")
        return result.strip() if result else None
    except Exception as exc:
        print(f"[SELF] Argos translation unavailable: {exc}")
        return None

def _self_cb(uid: int, action: str) -> bytes:
    return f"sp:{int(uid)}:{action}".encode("utf-8")


def _font_label(kind: str, key: str) -> str:
    labels = {
        "clock":{"normal":"عادی","bold":"بولد","double":"دوبل","sans":"سانس","sans_bold":"سانس بولد","mono":"مونو","full":"فول","circled":"دایره","negative":"منفی","superscript":"بالانویس","subscript":"زیرنویس","persian":"فارسی","arabic":"عربی","devanagari":"هندی"},
        "english":{"normal":"عادی","bold":"بولد","italic":"ایتالیک","bold_italic":"بولد ایتالیک","monospace":"مونو","double":"دوبل"},
    }
    return labels.get(kind, {}).get(key, key)

def self_panel_buttons(uid):
    def toggle_style(key):
        return "success" if self_get(uid, key, "off") == "on" else "danger"

    clock = self_get(uid, "clock_font", "normal")
    english = self_get(uid, "english_font", "normal")
    reaction_on = bool(self_reaction_targets(uid))
    return [
        [
            btn(f"🕐 ساعت {'روشن' if self_get(uid,'time_name')=='on' else 'خاموش'}", _self_cb(uid, "time"), "success" if self_get(uid,'time_name')=='on' else "danger"),
            btn(f"🔤 فونت: {_font_label('clock', clock)}", _self_cb(uid, "clockfont"), "primary"),
        ],
        [
            btn(f"🅱 بولد {'روشن' if self_get(uid,'bold')=='on' else 'خاموش'}", _self_cb(uid, "bold"), toggle_style("bold")),
            btn(f"🅵 فارسی {'روشن' if self_get(uid,'persian_font')=='on' else 'خاموش'}", _self_cb(uid, "persian"), toggle_style("persian_font")),
        ],
        [
            btn("🧹 پاکسازی", _self_cb(uid, "cleanup"), "danger"),
            btn(f"🌐 ترجمه {'روشن' if self_get(uid,'translate')=='on' else 'خاموش'}", _self_cb(uid, "translate"), toggle_style("translate")),
        ],
        [
            btn(f"👁 سین {'روشن' if self_get(uid,'auto_read')=='on' else 'خاموش'}", _self_cb(uid, "read"), toggle_style("auto_read")),
            btn(f"💬 پاسخ خودکار {'روشن' if self_get(uid,'auto_reply')=='on' else 'خاموش'}", _self_cb(uid, "autoreply"), toggle_style("auto_reply")),
        ],
        [
            btn(f"⌨️ تایپینگ {'روشن' if self_get(uid,'typing')=='on' else 'خاموش'}", _self_cb(uid, "typing"), toggle_style("typing")),
            btn(f"🎮 بازی {'روشن' if self_get(uid,'game_mode')=='on' else 'خاموش'}", _self_cb(uid, "game"), toggle_style("game_mode")),
        ],
        [
            btn(f"🔤 انگلیسی: {_font_label('english', english)}", _self_cb(uid, "engfont"), "primary"),
            btn("🏓 پینگ", _self_cb(uid, "ping"), "success"),
        ],
        [
            btn("🤖 تبچی", _self_cb(uid, "banners"), "primary"),
            btn("💾 ذخیره چنل", _self_cb(uid, "cs_open"), "primary"),
        ],
        [
            btn("💬 کامنت اول", _self_cb(uid, "comment_setup"), "primary"),
            btn("📚 راهنما", _self_cb(uid, "guide"), "primary"),
        ],
        [btn("❌ بستن", _self_cb(uid, "close"), "danger")],
    ]


def self_panel_text(uid):
    return (
        "╭━━━━━━━ ◈ 𝗛𝘂𝘀𝘁𝗲𝗥𝗜𝗫 ◈ ━━━━━━━╮\n\n"
        "𝙎𝙀𝙇𝙁  \n\n"
        "      𝙎𝙀𝙏𝙏𝙄𝙉𝙂𝙎\n\n"
        "  تنظیمات و شخصی‌سازی سلف\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


SELF_FEATURE_GUIDES = {
    "clock": ("""🕐 <b>ساعت روی نام پروفایل</b>

این قابلیت ساعت ایران را به انتهای نام پروفایل سلف اضافه می‌کند و به‌صورت خودکار آن را به‌روزرسانی می‌کند.

<b>فعال‌سازی:</b> از پنل سلف روی «🕐 ساعت» بزن یا دستور زیر را ارسال کن:
<code>ساعت روشن</code>

<b>خاموش‌کردن:</b>
<code>ساعت خاموش</code>

<b>تغییر ظاهر ساعت:</b>
<code>فونت ساعت بولد</code>
<code>فونت ساعت دایره</code>
<code>فونت ساعت فارسی</code>

فونت‌های موجود شامل عادی، بولد، دوبل، سانس، سانس بولد، مونو، فول، دایره، منفی، بالانویس، زیرنویس، فارسی، عربی و هندی هستند. تغییر فونت فقط ظاهر ساعت را عوض می‌کند و منطق سلف را تغییر نمی‌دهد."""),

    "fonts": ("""🔤 <b>فونت و شخصی‌سازی متن</b>

از این بخش می‌توانی ظاهر ساعت و نوشته‌های انگلیسی و فارسی سلف را شخصی‌سازی کنی.

<b>فونت ساعت:</b>
<code>فونت ساعت بولد</code>
مثلاً: عادی، بولد، دوبل، سانس، سانس بولد، مونو، فول، دایره، منفی، بالانویس، زیرنویس، فارسی، عربی، هندی.

<b>فونت انگلیسی:</b>
<code>فونت انگلیسی بولد</code>
<code>فونت انگلیسی ایتالیک</code>
<code>فونت انگلیسی بولد ایتالیک</code>
<code>فونت انگلیسی مونو</code>
<code>فونت انگلیسی دوبل</code>

<b>بولد فارسی و فونت فارسی:</b>
<code>بولد روشن</code> / <code>بولد خاموش</code>
<code>فونت فارسی روشن</code> / <code>فونت فارسی خاموش</code>

اگر فقط می‌خواهی ظاهر متن انگلیسی تغییر کند، از «فونت انگلیسی» استفاده کن؛ برای ساعت از «فونت ساعت» استفاده کن."""),

    "translate": ("""🌐 <b>ترجمه خودکار</b>

با فعال‌کردن این قابلیت، متن‌های ارسالی سلف می‌توانند به انگلیسی ترجمه شوند.

<b>روشن:</b>
<code>ترنسلیت روشن</code>

<b>خاموش:</b>
<code>ترنسلیت خاموش</code>

همین قابلیت از دکمه «🌐 ترجمه» داخل پنل هم قابل کنترل است. ترجمه از سرویس‌های داخلی تلگرام و در صورت نیاز موتور جایگزین انجام می‌شود."""),

    "presence": ("""👁 <b>سین، تایپینگ و حالت بازی</b>

این سه گزینه برای طبیعی‌ترکردن فعالیت اکانت سلف هستند و از پنل یا دستور قابل کنترل‌اند.

<b>👁 سین خودکار</b>
پیام‌های دریافتی را خوانده‌شده می‌کند.
<code>سین روشن</code>
<code>سین خاموش</code>

<b>⌨️ تایپینگ</b>
هنگام آماده‌سازی پاسخ، وضعیت تایپ‌کردن را در چت نمایش می‌دهد.
<code>تایپینگ روشن</code>
<code>تایپینگ خاموش</code>

<b>🎮 حالت بازی</b>
به‌جای وضعیت تایپینگ، وضعیت بازی را نمایش می‌دهد.
<code>حالت بازی روشن</code>
<code>حالت بازی خاموش</code>

هرکدام مستقل قابل روشن/خاموش‌شدن هستند."""),

    "autoreply": ("""💬 <b>پاسخ خودکار؛ ساخت پاسخ برای کلمات مشخص</b>

پاسخ خودکار برای زمانی است که می‌خواهی وقتی یک کلمه یا عبارت مشخص در پیام طرف مقابل دیده شد، سلف پاسخ ذخیره‌شده را ارسال کند.

<b>مرحله ۱ — روشن‌کردن قابلیت:</b>
<code>پاسخ خودکار روشن</code>

<b>مرحله ۲ — ساخت کلمه/کلید:</b>
<code>پاسخ خودکار جدید سلام</code>

این دستور «سلام» را به‌عنوان کلید ثبت می‌کند.

<b>مرحله ۳ — تعیین پاسخ</b>
یک پیام متنی که می‌خواهی به‌عنوان پاسخ استفاده شود ارسال/آماده کن، سپس روی همان پیام ریپلای کن و بنویس:
<code>ذخیره پاسخ خودکار سلام</code>

یعنی: «سلام» ← متن پیام ریپلای‌شده.

<b>حذف یک پاسخ:</b>
<code>حذف پاسخ خودکار سلام</code>

<b>دیدن همه کلیدها و پاسخ‌ها:</b>
<code>لیست پاسخ خودکار</code>

<b>خاموش‌کردن موقت:</b>
<code>پاسخ خودکار خاموش</code>

⚠️ نکته مهم: دستور «ذخیره پاسخ خودکار ...» باید روی یک پیام متنی ریپلای شود؛ صرفاً نوشتن دستور، متن پاسخ را ایجاد نمی‌کند."""),

    "reaction": ("""❤️ <b>ریاکشن خودکار برای یک کاربر</b>

با این قابلیت می‌توانی تعیین کنی سلف برای پیام‌های یک کاربر مشخص، یک ریاکشن ثابت بگذارد.

<b>فعال‌سازی:</b> در پیوی روی یکی از پیام‌های همان کاربر ریپلای کن و مثلاً بنویس:
<code>ریاکشن ❤️</code>
یا:
<code>ریاکشن 🔥</code>

از این به بعد ریاکشن انتخاب‌شده برای آن کاربر استفاده می‌شود. تلگرام خودش معتبر بودن ایموجی را بررسی می‌کند.

<b>حذف ریاکشن همان کاربر:</b> روی پیام او ریپلای کن و بنویس:
<code>حذف ریاکشن</code>

⚠️ این تنظیم برای «کاربر هدف» ذخیره می‌شود، نه فقط برای همان پیام."""),

    "lock": ("""🔒 <b>قفل چت خصوصی</b>

برای یک کاربر مشخص در پیوی می‌توانی قفل چت فعال کنی تا پیام‌های بعدی او به‌صورت دوطرفه پاک شوند.

<b>فعال‌سازی:</b> در پیوی روی پیام همان کاربر ریپلای کن:
<code>قفل چت</code>

<b>بازکردن قفل:</b> دوباره روی پیام همان کاربر ریپلای کن:
<code>بازکردن قفل چت</code>

این قابلیت فقط برای پیوی طراحی شده و برای استفاده صحیح باید دستور را با ریپلای روی پیام کاربر بفرستی.

⚠️ «قفل چت» با «بلاک» فرق دارد؛ قفل، تنظیم پاک‌سازی پیام‌هاست و لزوماً کاربر را از تلگرام بلاک نمی‌کند."""),

    "media": ("""🎙️ <b>رسانه و تبدیل فایل‌ها</b>

برای تبدیل، دستور را روی پیام رسانه‌ای موردنظر ریپلای کن.

<b>🎵 ویس → MP3</b>
<code>ویس به mp3</code>
روی Voice ریپلای کن؛ خروجی یک فایل واقعی MP3 است.

<b>🎙️ MP3 → ویس</b>
<code>mp3 به ویس</code>
روی فایل MP3 ریپلای کن؛ خروجی به‌صورت Voice ارسال می‌شود.

<b>🎬 ویدیو → ویس</b>
<code>ویدیو به ویس</code>
روی ویدیو ریپلای کن؛ صدای ویدیو استخراج و به Voice تبدیل می‌شود.

<b>🎬 ویدیو → MP3</b>
<code>ویدیو به mp3</code>
روی ویدیو ریپلای کن؛ فقط ترک صوتی به MP3 تبدیل می‌شود.

<b>📝 ویس/فایل صوتی → متن</b>
روی ویس یا فایل صوتی ریپلای کن و بنویس:
<code>متن</code>
یا برای حالت ریپلای صریح:
<code>متن + ریپلی</code>

<b>🖼 تصویر → متن (OCR)</b>
روی تصویر ریپلای کن:
<code>OCR</code>
یا:
<code>او سی آر</code>

پردازش تبدیل‌های رسانه‌ای مستقل از بخش ذخیره چنل انجام می‌شود."""),

    "files": ("""📦 <b>فایل، آرشیو و دانلود</b>

<b>📥 دانلود پیام</b>
روی پیام موردنظر ریپلای کن و بنویس:
<code>دانلود</code>
این گزینه برای ذخیره/دریافت محتوای پیام در مسیر پردازش سلف استفاده می‌شود.

<b>📦 استخراج ZIP/RAR</b>
روی فایل ZIP یا RAR ریپلای کن:
<code>استخراج</code>

فرم‌های دارای ریپلای نیز پشتیبانی می‌شوند، مانند:
<code>استخراج + ریپلای</code>
<code>استخراج ریپلای</code>

همچنین نام‌های انگلیسی قدیمی مانند <code>unzip</code> در منطق فعلی وجود دارند.

⚠️ فقط آرشیو/پیام مناسب را ریپلای کن؛ برای یک پیام عادی نیازی به «استخراج» نیست."""),

    "channel_save": ("""💾 <b>ذخیره چنل؛ انتقال پیام‌های کانال به Saved Messages</b>

این قابلیت برای برداشتن تعداد مشخصی از پیام‌های یک کانال و ذخیره‌کردن آن‌ها در <b>Saved Messages</b> اکانت SELF ساخته شده است. عملیات با خود اکانت SELF انجام می‌شود، نه با ربات.

<b>روش استفاده:</b>
① از پنل سلف روی «💾 ذخیره چنل» بزن.
② کانال موردنظر را از لیست انتخاب کن.
③ نوع محتوا را انتخاب کن:
• 🖼 تصویر
• 🎬 ویدیو
• 🎵 موسیقی
• 🎤 ویس
• 📝 متن
• 📦 همه
④ تعداد را با صفحه اعداد وارد کن.
⑤ روی «✅ تأیید و شروع» بزن.

در زمان اجرا، همان پیام پنل ویرایش می‌شود و نوار پیشرفت، تعداد بررسی‌شده، موفق و ناموفق را نشان می‌دهد. در پایان موارد موفق داخل <b>Saved Messages</b> قرار می‌گیرند.

<b>حداکثر تعداد:</b> ۱۰۰۰ مورد در هر عملیات.

⚠️ اگر کانالی در لیست نمی‌آید، باید آن کانال برای اکانت SELF قابل دسترسی باشد."""),

    "tabchi": ("""📢 <b>تبچی؛ بنر و ارسال خودکار</b>

تبچی برای ساخت «بنر» از یک پیام و ارسال آن به مقصدهای مشخص با فاصله زمانی تعیین‌شده است. هر بنر شماره مخصوص خودش را دارد؛ مثلاً <b>۱</b>.

<b>① ساخت بنر</b>
روی پیام موردنظر ریپلای کن:
<code>تنظیم بنر فور</code>
یعنی پیام به شکل Forward ارسال می‌شود.

یا:
<code>تنظیم بنر کپی</code>
یعنی محتوای پیام به‌صورت کپی/ارسال مجدد استفاده می‌شود؛ برای مدیا فایل محلی نیز نگهداری می‌شود.

بعد از ساخت، مثلاً پیام می‌گوید بنر <b>#۱</b> ساخته شده است. از همین شماره در دستورات بعدی استفاده کن.

<b>② اضافه‌کردن همین گپ به مقصد بنر</b>
داخل همان گروه بنویس:
<code>تنظیم گپ هدف بنر ۱</code>
گروه فعلی به مقصدهای بنر ۱ اضافه می‌شود.

<b>③ حذف گپ از مقصد</b>
داخل همان گروه:
<code>حذف گپ هدف بنر ۱</code>

<b>④ قرار دادن تمام گپ‌های قابل‌دسترسی به‌عنوان مقصد</b>
<code>تنظیم هدف بنر ۱ تمام گپ ها</code>
این دستور گپ‌های گروهی قابل‌دسترسی سلف را بررسی و به مقصدهای بنر اضافه می‌کند.

<b>⑤ زمان‌بندی ارسال</b>
<code>تنظیم عدد بنر ۱ ۳۰ دقیقه</code>
یعنی بنر ۱ با فاصله ۳۰ دقیقه‌ای ارسال شود. حداقل زمان ۱ دقیقه است.

<b>⑥ ارسال به پیوی‌های اخیر</b>
<code>فور بنر در ۱۰ پیوی اخیر</code>
بنر شماره ۱ را نمی‌گوید؛ پس حتماً شماره را مشخص کن، مثال کامل:
<code>فور بنر در ۱ ۱۰ پیوی اخیر</code>
یعنی بنر ۱ به ۱۰ پیوی اخیر ارسال شود.

<b>⑦ کنترل تبچی</b>
<code>تبچی روشن</code>
<code>تبچی خاموش</code>
با روشن‌شدن تبچی، بنرهای فعالِ دارای مقصد می‌توانند ارسال خودکار داشته باشند و ارسال فوری بنرهای تنظیم‌شده نیز انجام می‌شود.

<b>⑧ مشاهده وضعیت و لیست</b>
<code>وضعیت تبچی</code>
<code>لیست بنر هام</code>

<b>⑨ حذف بنر</b>
<code>حذف بنر ۱</code>

<b>⑩ پاک‌کردن تمام بنرها</b>
<code>پاکسازی لیست بنر ها</code>

💡 اگر چند بنر داری، همیشه شماره صحیح را جایگزین «۱» کن. مثلاً برای بنر ۳ بنویس <code>حذف بنر ۳</code>."""),

    "spam": ("""🔁 <b>تکرار پیام</b>

برای فرستادن یک پیام چند بار پشت سر هم، روی پیام موردنظر ریپلای کن و بنویس:
<code>تکرار 160</code>

عدد تعداد دفعات تکرار است. در منطق فعلی تعداد مجاز بین <b>۱ تا ۱۰۰۰</b> قرار دارد.

مثال:
روی «سلام» ریپلای + <code>تکرار 20</code>
یعنی همان پیام تا ۲۰ بار تکرار می‌شود.

⚠️ این قابلیت دکمه‌ای داخل پنل ندارد و با دستور اجرا می‌شود. از تعدادهای بالا با احتیاط استفاده کن تا با محدودیت‌های تلگرام مواجه نشوی."""),

    "comments": ("""💬 <b>کامنت اول کانال</b>

این قابلیت یک متن ثابت را برای پست‌های کانال در Discussion متصل‌شده به‌عنوان کامنت اول ارسال می‌کند. برای کارکرد صحیح، کانال باید Discussion متصل داشته باشد.

<b>مرحله ۱ — انتخاب کانال</b>
از پنل «💬 کامنت اول» وارد شو و کانال را از لیست انتخاب کن.

<b>مرحله ۲ — تنظیم متن</b>
بعد از انتخاب کانال، روی پیام متنی‌ای که می‌خواهی کامنت باشد ریپلای کن و بنویس:
<code>تنظیم کامنت</code>

متن پیام ریپلای‌شده برای همان کانال ذخیره و قابلیت فعال می‌شود.

<b>تنظیم کانال با دستور:</b>
<code>تنظیم کامنت اول @Channel</code>
یا آیدی کانال را بده.

<b>حذف تنظیم یک کانال:</b>
<code>حذف کامنت اول @Channel</code>

<b>دیدن تنظیمات:</b>
<code>لیست کامنت</code>

<b>پاک‌کردن تمام تنظیمات:</b>
<code>پاکسازی لیست کامنت</code>

در پنل هر کانال امکان روشن/خاموش‌کردن و حذف تنظیمات نیز وجود دارد. اگر کانال Discussion نداشته باشد، کامنت اول قابل تنظیم نیست."""),

    "secretary": ("""🤵 <b>منشی؛ پاسخ آماده برای پیوی</b>

منشی برای زمانی است که می‌خواهی سلف در پیوی، هنگام دریافت پیام از یک کاربر، یک پاسخ آماده بفرستد. منشی فقط در <b>پیوی</b> کار می‌کند.

<b>مرحله ۱ — ساخت پاسخ منشی</b>
یک پیام متنی یا مدیایی که می‌خواهی منشی ارسال کند آماده کن. روی همان پیام ریپلای کن و بنویس:
<code>تنظیم منشی</code>

اگر پیام مدیا باشد، مدیا نیز برای استفاده بعدی ذخیره می‌شود.

<b>مرحله ۲ — فعال‌سازی</b>
<code>منشی روشن</code>

<b>خاموش‌کردن:</b>
<code>منشی خاموش</code>

<b>تعیین فاصله زمانی پاسخ‌ها:</b>
<code>تنظیم زمان منشی 15</code>

عدد برحسب دقیقه است و در منطق فعلی باید بین <b>۵ تا ۶۰ دقیقه</b> باشد.

یعنی اگر روی ۱۵ دقیقه تنظیم شود، برای هر کاربر در هر بازه زمانی فقط پاسخ منشی موردنظر ارسال می‌شود و از پاسخ‌دادن مکرر جلوگیری می‌شود.

⚠️ اگر منشی روشن است ولی پاسخی تنظیم نشده، ابتدا با «تنظیم منشی» پیام پاسخ را ذخیره کن."""),

    "group": ("""🛡 <b>مدیریت گروه با SELF</b>

این دستورات برای مدیریت گروه هستند و فقط زمانی اجرا می‌شوند که SELF مجوز/ادمین لازم را در گروه داشته باشد. بیشتر دستورات باید روی پیام مناسب ریپلای شوند.

<b>📌 پین پیام</b>
روی پیام موردنظر ریپلای:
<code>پین</code>

<b>📌 برداشتن پین</b>
<code>حذف پین</code>

<b>🚫 بن کاربر</b>
روی پیام کاربر ریپلای:
<code>بن</code>
یا:
<code>سیک</code>

<b>✅ آن‌بن</b>
روی پیام کاربر ریپلای:
<code>آن بن</code>

اگر SELF ادمین نباشد یا سطح دسترسی لازم را نداشته باشد، عملیات توسط تلگرام رد می‌شود.

💡 «بن» و «آن بن» مربوط به دسترسی کاربر در همان گروه هستند؛ «بن سراسری» قابلیت جداگانه‌ای است و در بخش بعد توضیح داده شده است."""),

    "globalban": ("""🚫 <b>بن سراسری</b>

بن سراسری یک لیست داخلی از کاربران مسدودشده برای همان سلف ایجاد می‌کند و با آن می‌توانی یک کاربر را در سطح قابلیت‌های مربوط به سلف مسدود نگه داری.

<b>افزودن با یوزرنیم/آیدی:</b>
<code>بن سراسری @username</code>

<b>افزودن با ریپلای:</b>
روی پیام کاربر ریپلای کن و فقط بنویس:
<code>بن سراسری</code>

<b>حذف از لیست:</b>
<code>حذف بن سراسری @username</code>
یا با ریپلای به کاربر هدف.

<b>مشاهده لیست:</b>
<code>لیست بن سراسری</code>

اگر دستور افزودن در یک گروه اجرا شود و SELF ادمین باشد، منطق فعلی می‌تواند همان کاربر را در آن گروه نیز محدود کند.

⚠️ «بن سراسری» با بن عادی گروه فرق دارد؛ لیست آن جداگانه ذخیره می‌شود."""),

    "tag": ("""🏷 <b>تگ اعضای گروه</b>

این قابلیت اعضای گروه را به‌صورت گروهی منشن می‌کند. دستور باید داخل گروه اجرا شود.

<b>تگ تعداد مشخص:</b>
<code>تگ 20</code>
یعنی تا ۲۰ عضو مناسب برای تگ انتخاب شود.

<b>تگ همه اعضای قابل‌تگ:</b>
<code>همه</code>

محدوده تعداد مشخص در منطق فعلی <b>۱ تا ۱۰۰۰</b> است. ربات‌ها و خود اکانت SELF از لیست حذف می‌شوند.

اگر دستور را روی یک پیام ریپلای کنی، پیام‌های تگ می‌توانند به همان پیام پاسخ داده شوند. پیام دستور بعد از اجرا حذف می‌شود و اعضا در بسته‌های گروهی ارسال می‌شوند تا پیام بیش از حد بزرگ نشود."""),

    "currency": ("""💱 <b>قیمت ارز و رمزارز</b>

برای گرفتن قیمت، نماد دارایی را بعد از «قیمت» بنویس:
<code>قیمت BTC</code>
<code>قیمت ETH</code>
<code>قیمت SOL</code>
<code>قیمت USDT</code>

سرویس قیمت به‌صورت عمومی استفاده می‌شود و در صورت خطا مسیر جایگزین وجود دارد.

💡 نماد رمزارز را دقیق بنویس؛ مثلاً <code>BTC</code> برای بیت‌کوین.

این بخش صرفاً نمایش نرخ است و تغییری در موجودی یا تراکنش‌های سلف ایجاد نمی‌کند."""),

    "logo": ("""🎨 <b>لوگوساز</b>

لوگوساز داخلی برای ساخت لوگو با قالب‌های آماده است. قابلیت فعلی دارای <b>۱۲ قالب داخلی</b> و رایگان است.

<b>فرمت دستور:</b>
<code>لوگو 12 HusteRIX</code>

عدد، شماره قالب و متن بعد از آن، نوشته‌ای است که می‌خواهی روی لوگو قرار بگیرد.

مثال:
<code>لوگو 3 Diamond Self</code>

اگر قالب یا متن را اشتباه وارد کنی، خود قابلیت پیام خطا/راهنمای لازم را برمی‌گرداند."""),

    "profile": ("""👤 <b>کپی و بازگردانی پروفایل</b>

این ابزار برای گرفتن اطلاعات پروفایل یک کاربر از روی پیام ریپلای و اعمال آن روی پروفایل SELF طراحی شده است.

<b>کپی پروفایل:</b>
روی پیام همان کاربر ریپلای کن و بنویس:
<code>کپی پروفایل</code>

<b>بازگردانی پروفایل قبلی:</b>
<code>حذف کپی پروفایل</code>

همیشه قبل از اجرای «کپی پروفایل» مطمئن شو روی پیام کاربر درست ریپلای کرده‌ای؛ چون هدف از روی sender همان پیام تشخیص داده می‌شود."""),

    "creation": ("""🏗 <b>ساخت گروه و چنل</b>

SELF می‌تواند از طریق دستور، یک گروه یا کانال ایجاد کند.

<b>ساخت گروه:</b>
<code>ساخت گروه نام گروه</code>

<b>ساخت چنل:</b>
<code>ساخت چنل نام کانال</code>

متن بعد از دستور، نام گروه/چنل است. مثال:
<code>ساخت گروه گپ دوستان</code>
<code>ساخت چنل Diamond News</code>

⚠️ ایجاد مورد جدید با اکانت SELF انجام می‌شود و محدودیت‌های خود تلگرام، دسترسی حساب و محدودیت‌های ضداسپم ممکن است روی نتیجه اثر بگذارند."""),

    "dice": ("""🎲 <b>تاس و حالت‌های سرگرمی</b>

برای تولید مقدار مشخص تاس، از دستور زیر استفاده کن:
<code>تاس 6</code>

عدد مجاز بین <b>۱ تا ۶</b> است. دستور پس از اجرا حذف می‌شود و SELF تلاش می‌کند مقدار خواسته‌شده را تولید کند.

مثال‌ها:
<code>تاس 1</code>
<code>تاس 3</code>
<code>تاس 6</code>

همچنین یک دستور نمایشی/شوخی به نام زیر در منطق فعلی وجود دارد:
<code>هک</code>
این مورد یک عملیات سرگرمی است و به معنی هک واقعی حساب یا سیستم نیست."""),

    "stickers": ("""🖼 <b>استیکر و عکس</b>

برای تبدیل تصویر به استیکر:
<code>استیکر</code>
یا:
<code>تبدیل عکس به استیکر</code>

برای حالتی که می‌خواهی ریپلای حفظ شود:
<code>استیکر + ریپلای</code>

برای تبدیل استیکر به عکس:
<code>عکس</code>
یا:
<code>تبدیل استیکر به عکس</code>

حالت دارای ریپلای:
<code>عکس + ریپلای</code>

این دستورات بر اساس نوع رسانه پیام ریپلای‌شده عمل می‌کنند؛ بنابراین برای تبدیل، روی رسانه مناسب ریپلای کن."""),

    "misc": ("""🧰 <b>دستورات عمومی و کاربردی</b>

<b>🏓 پینگ:</b>
<code>پینگ</code>
زمان تقریبی پاسخ اجرای سلف را نمایش می‌دهد.

<b>⚙️ بازکردن پنل:</b>
<code>پنل</code>
پنل شیشه‌ای تنظیمات را در همان چت باز می‌کند.

<b>📚 بازکردن راهنما:</b>
<code>راهنما</code>
راهنمای کامل قابلیت‌ها را باز می‌کند.

<b>🧹 پاکسازی اکانت:</b> از دکمه «🧹 پاکسازی» در پنل استفاده کن. این بخش دسته‌های مختلف چت‌ها، ربات‌ها، گروه‌ها، کانال‌ها و مخاطبین را مدیریت می‌کند.

<b>📱 شماره و فعال‌سازی:</b> قبل از استفاده از پنل، در ربات اصلی باید شماره موردنیاز را طبق پیام /start ثبت کنی.

💡 اگر دستوری پاسخ نداد، ابتدا بررسی کن که آن دستور مربوط به SELF باشد، روی پیام درست ریپلای شده باشد و اکانت SELF فعال باشد."""),
}



def self_feature_guide_buttons(uid):
    labels = [
        ("🕐 ساعت روی نام", "clock"), ("🔤 فونت‌ها", "fonts"),
        ("🌐 ترجمه", "translate"), ("👁 سین / تایپینگ / بازی", "presence"),
        ("💬 پاسخ خودکار", "autoreply"), ("❤️ ریاکشن", "reaction"),
        ("🔒 قفل چت", "lock"), ("🎙️ رسانه و تبدیل‌ها", "media"),
        ("📦 فایل و دانلود", "files"), ("📢 تبچی", "tabchi"),
        ("🔁 تکرار / اسپم", "spam"), ("💬 کامنت اول", "comments"),
        ("🤵 منشی", "secretary"), ("🛡 مدیریت گروه", "group"),
        ("🚫 بن سراسری", "globalban"), ("🏷 تگ اعضا", "tag"),
        ("💱 نرخ ارز", "currency"), ("🎨 لوگوساز", "logo"),
        ("👤 کپی پروفایل", "profile"), ("🏗 ساخت گروه / چنل", "creation"),
        ("🎲 تاس / سرگرمی", "dice"), ("🖼 استیکر / عکس", "stickers"),
        ("🧰 دستورات عمومی", "misc"),
    ]
    rows=[]
    for i in range(0, len(labels), 2):
        row=[btn(labels[i][0], _self_cb(uid, "feature_help:"+labels[i][1]), "primary")]
        if i+1 < len(labels):
            row.append(btn(labels[i+1][0], _self_cb(uid, "feature_help:"+labels[i+1][1]), "primary"))
        rows.append(row)
    rows.append([btn("💾 ذخیره چنل", _self_cb(uid, "feature_help:channel_save"), "primary")])
    rows.append([btn("🔙 بازگشت", _self_cb(uid, "panel"), "danger")])
    return rows


def self_font_preview(uid, kind):
    if kind == "clock":
        return (
            f"🕐 پیش‌نمایش ساعت\n\n<b>{self_clock(uid)}</b>\n"
            f"فونت فعلی: <b>{_font_label('clock', self_get(uid,'clock_font','normal'))}</b>"
        )
    return (
        f"🔤 پیش‌نمایش فونت انگلیسی\n\n<b>{self_transform_english('Hello Telegram', uid)}</b>\n"
        f"فونت فعلی: <b>{_font_label('english', self_get(uid,'english_font','normal'))}</b>"
    )


async def _get_inline_bot_entity(client):
    """Resolve the bot entity once per logged-in self client."""
    cache_key = id(client)
    cached = _inline_bot_cache.get(cache_key)
    if cached is not None:
        return cached

    me = await bot.get_me()
    if not me:
        raise RuntimeError("Bot entity could not be resolved")

    username = getattr(me, "username", None)
    entity = await client.get_input_entity(f"@{username}" if username else me.id)
    _inline_bot_cache[cache_key] = entity
    return entity


async def send_self_inline_result(event, query: str):
    """Insert the bot's inline result using the logged-in user account.

    Unlike bot.send_message(), this does not require the bot to be a member
    of the target chat.
    """
    if not event.peer_id:
        raise RuntimeError("Target peer is unavailable")

    bot_entity = await _get_inline_bot_entity(event.client)
    results = await event.client(
        GetInlineBotResultsRequest(
            bot=bot_entity,
            peer=event.peer_id,
            geo_point=None,
            query=query,
            offset="",
        )
    )

    if not getattr(results, "results", None):
        raise RuntimeError(f"Inline bot returned no result for query: {query}")

    result = results.results[0]
    await event.client(
        SendInlineBotResultRequest(
            peer=event.peer_id,
            query_id=results.query_id,
            id=result.id,
            hide_via=True,
            clear_draft=True,
        )
    )


def _event_inline_message_id(event):
    """Return the real inline-message identifier from a Telethon callback."""
    if not getattr(event, "via_inline", False):
        return None
    query = getattr(event, "query", None)
    return getattr(query, "msg_id", None)


def _serialize_inline_message_id(value):
    """Serialize Telethon's InputBotInlineMessageID for durable per-operation state."""
    if value is None:
        return None
    try:
        return {
            "dc_id": int(value.dc_id),
            "id": int(value.id),
            "access_hash": int(value.access_hash),
        }
    except Exception:
        return None


def _deserialize_inline_message_id(value):
    """Rebuild an InputBotInlineMessageID from saved state."""
    if not isinstance(value, dict):
        return None
    try:
        return types.InputBotInlineMessageID(
            dc_id=int(value["dc_id"]),
            id=int(value["id"]),
            access_hash=int(value["access_hash"]),
        )
    except Exception:
        return None


async def _edit_panel_message(*, text, buttons=None, inline_message_id=None,
                              chat_id=None, message_id=None, parse_mode="html"):
    """Edit either an inline message or a normal bot-owned message."""
    if inline_message_id is not None:
        target = (
            inline_message_id
            if isinstance(inline_message_id, types.InputBotInlineMessageID)
            else _deserialize_inline_message_id(inline_message_id)
        )
        if target is None:
            raise RuntimeError("invalid inline_message_id")
        return await bot.edit_message(
            target,
            text,
            parse_mode=parse_mode,
            buttons=buttons,
        )

    if chat_id is None or message_id is None:
        raise RuntimeError(
            f"panel message identity missing: chat_id={chat_id} message_id={message_id}"
        )

    return await bot.edit_message(
        int(chat_id),
        int(message_id),
        text,
        parse_mode=parse_mode,
        buttons=buttons,
    )


async def _transfer_sender_label(client, user_id: int) -> str:
    """@username when available, otherwise the numeric Telegram ID."""
    try:
        entity = await client.get_entity(int(user_id))
        username = getattr(entity, "username", None)
        if username:
            return f"@{username}"
    except Exception:
        pass
    return str(int(user_id))



def self_chat_lock_targets(uid):
    try:
        raw = json.loads(self_get(uid, "chat_lock_targets", "[]"))
        return {int(x) for x in raw}
    except Exception:
        return set()


def self_save_chat_lock_targets(uid, targets):
    self_set(uid, "chat_lock_targets", json.dumps(sorted(int(x) for x in targets)))














async def _tg_call_with_flood_retry(call_factory, *, label="telegram", max_retries=20):
    """Retry Telegram API calls after FloodWait instead of aborting a long operation."""
    for attempt in range(max_retries):
        try:
            return await call_factory()
        except FloodWaitError as exc:
            wait = max(1, int(getattr(exc, "seconds", 1)))
            print(f"[TELEGRAM] FloodWait during {label}: sleeping {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)
    raise RuntimeError(f"Telegram kept rate-limiting {label} after {max_retries} retries")


async def _cleanup_delete_private(client, entity):
    # revoke=True performs the two-sided deletion where Telegram permits it.
    await _tg_call_with_flood_retry(
        lambda: client(functions.messages.DeleteHistoryRequest(
            peer=entity, max_id=0, just_clear=False, revoke=True
        )),
        label="delete private history",
    )


async def _cleanup_leave_dialog_safe(client, entity, uid):
    try:
        if isinstance(entity, types.Channel):
            await _tg_call_with_flood_retry(
                lambda: client(functions.channels.LeaveChannelRequest(channel=entity)),
                label="leave channel/group",
            )
        elif isinstance(entity, types.Chat):
            me = await client.get_me()
            input_me = await client.get_input_entity(me)
            await _tg_call_with_flood_retry(
                lambda: client(functions.messages.DeleteChatUserRequest(
                    chat_id=entity.id, user_id=input_me
                )),
                label="leave basic group",
            )
        return True, None
    except Exception as exc:
        return False, str(exc)


async def _cleanup_contacts(client):
    result = await _tg_call_with_flood_retry(
        lambda: client(functions.contacts.GetContactsRequest(hash=0)),
        label="get contacts",
    )
    users = getattr(result, "users", None) or []
    input_users = [types.InputUser(u.id, u.access_hash) for u in users if getattr(u, "access_hash", None) is not None]
    if not input_users:
        return 0
    await _tg_call_with_flood_retry(
        lambda: client(functions.contacts.DeleteContactsRequest(id=input_users)),
        label="delete contacts",
    )
    return len(input_users)


async def _cleanup_dialog_snapshot(client, uid):
    dialogs = []
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not entity or getattr(entity, "id", None) == uid:
            continue
        dialogs.append(dialog)
    return dialogs


def _cleanup_categories(dialogs):
    chats = []
    bots = []
    groups = []
    channels = []
    for dialog in dialogs:
        entity = getattr(dialog, "entity", None)
        if getattr(dialog, "is_group", False):
            groups.append(dialog)
        elif getattr(dialog, "is_channel", False):
            channels.append(dialog)
        elif getattr(dialog, "is_user", False):
            if getattr(entity, "bot", False):
                bots.append(dialog)
            else:
                chats.append(dialog)
    return chats, bots, groups, channels


async def _cleanup_private_dialogs(client, dialogs, uid, label, block_bots=False, progress_cb=None):
    total = len(dialogs)
    if not total:
        return 0

    # Telegram rate-limits long cleanup jobs.  A small amount of concurrency
    # makes private-history cleanup substantially faster without hammering the API.
    semaphore = asyncio.Semaphore(3)
    lock = asyncio.Lock()
    done = 0

    async def one(dialog):
        nonlocal done
        entity = dialog.entity
        async with semaphore:
            try:
                # This cleanup action is explicitly initiated by the account owner.
                # Never archive from this path. Automatic archiving is handled by
                # MessageDeleted for incoming messages only.
                await _cleanup_delete_private(client, entity)
                if block_bots and getattr(entity, "bot", False):
                    await _tg_call_with_flood_retry(
                        lambda e=entity: client(functions.contacts.BlockRequest(id=e)),
                        label="block bot",
                    )
            except Exception as exc:
                print(f"[CLEANUP {uid}] private {getattr(entity,'id','?')}: {exc}")
            finally:
                async with lock:
                    done += 1
                    current = done
                # Updating the Telegram panel for every dialog was a major
                # source of slowness.  Refresh only every 5 items and at the end.
                if progress_cb and (current == total or current % 5 == 0):
                    await progress_cb(f"🧹 {label}… {current}/{total}")

    await asyncio.gather(*(one(dialog) for dialog in dialogs))
    return done


async def _cleanup_leave_dialogs(client, dialogs, uid, label, progress_cb=None):
    total = len(dialogs)
    if not total:
        return 0

    semaphore = asyncio.Semaphore(3)
    lock = asyncio.Lock()
    done = 0

    async def one(dialog):
        nonlocal done
        entity = dialog.entity
        async with semaphore:
            ok, err = await _cleanup_leave_dialog_safe(client, entity, uid)
            if not ok:
                print(f"[CLEANUP {uid}] leave {getattr(entity,'id','?')}: {err}")
            async with lock:
                done += 1
                current = done
            if progress_cb and (current == total or current % 5 == 0):
                await progress_cb(f"🚪 {label}… {current}/{total}")

    await asyncio.gather(*(one(dialog) for dialog in dialogs))
    return done


async def _cleanup_run(uid, target, panel_chat_id=None, panel_message_id=None, panel_inline_message_id=None):
    client = self_clients.get(uid)
    if not client:
        self_set(uid, "cleanup_progress", "❌ سلف فعال نیست")
        return

    last_panel_update = 0.0

    async def progress(text, force=False):
        nonlocal last_panel_update
        self_set(uid, "cleanup_progress", text)
        if panel_inline_message_id is not None or (panel_chat_id is not None and panel_message_id is not None):
            now = time.monotonic()
            # Never edit the same Telegram message dozens/hundreds of times per
            # second. State is still saved on every call; UI is throttled.
            if not force and (now - last_panel_update) < 0.75:
                return
            last_panel_update = now
            with contextlib.suppress(Exception):
                await _edit_panel_message(
                    text=self_panel_text(uid),
                    buttons=self_panel_buttons(uid),
                    inline_message_id=panel_inline_message_id,
                    chat_id=panel_chat_id,
                    message_id=panel_message_id,
                    parse_mode="html",
                )

    try:
        self_set(uid, "cleanup_running", "on")
        await progress("⏳ در حال آماده‌سازی پاکسازی…")
        dialogs = await _cleanup_dialog_snapshot(client, uid)
        chats, bots, groups, channels = _cleanup_categories(dialogs)
        total = 0

        if target in {"chats", "all"}:
            total += await _cleanup_private_dialogs(client, chats, uid, "پاکسازی چت‌ها به‌صورت دوطرفه", progress_cb=progress)

        if target in {"bots", "all"}:
            total += await _cleanup_private_dialogs(client, bots, uid, "پاکسازی و بلاک ربات‌ها", block_bots=True, progress_cb=progress)

        if target in {"groups", "all"}:
            total += await _cleanup_leave_dialogs(client, groups, uid, "ترک گپ‌ها", progress_cb=progress)

        if target in {"channels", "all"}:
            total += await _cleanup_leave_dialogs(client, channels, uid, "ترک کانال‌ها", progress_cb=progress)

        contact_count = 0
        if target in {"contacts", "all"}:
            await progress("👥 در حال حذف مخاطبین…")
            try:
                contact_count = await _cleanup_contacts(client)
            except Exception as exc:
                print(f"[CLEANUP {uid}] contacts: {exc}")

        labels = {
            "chats": "چت‌ها", "bots": "ربات‌ها", "groups": "گپ‌ها",
            "channels": "کانال‌ها", "contacts": "مخاطبین", "all": "همه"
        }
        await progress(f"✅ {labels.get(target, 'پاکسازی')} انجام شد • {total} گفتگو • {contact_count} مخاطب", force=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[CLEANUP {uid}] fatal: {exc}")
        await progress(f"⚠️ پاکسازی با خطا متوقف شد: {exc}", force=True)
    finally:
        self_set(uid, "cleanup_running", "off")
        _cleanup_tasks.pop(uid, None)


async def _cleanup_account(uid, panel_chat_id=None, panel_message_id=None, panel_inline_message_id=None):
    # Backward-compatible entry point: "all" is the old full-cleanup behavior.
    await _cleanup_run(uid, "all", panel_chat_id, panel_message_id, panel_inline_message_id)



# ============================================================
# CHANNEL SAVE — CLEAN IMPLEMENTATION
# ============================================================

CHANNEL_SAVE_MAX_COUNT = 1000
CHANNEL_SAVE_PROGRESS_INTERVAL = 0.75


def _cs_session(uid):
    return _channel_save_sessions.get(int(uid))


def _cs_clear(uid):
    _channel_save_sessions.pop(int(uid), None)


def _cs_editor_from_event(event):
    # Keep the original callback event as the primary editor.  This is important
    # for inline-result messages: the callback event already knows exactly which
    # message Telegram delivered the button press from, so progress edits do not
    # accidentally switch to a different message/client identity.
    return {
        "event": event,
        "inline_message_id": _event_inline_message_id(event),
        "chat_id": getattr(event, "chat_id", None),
        "message_id": getattr(event, "message_id", None),
    }


async def _cs_edit(editor, text, buttons=None):
    """Edit the exact message that produced the confirm callback."""
    event = editor.get("event")
    last_exc = None

    # First use the original callback event.  It is the most reliable way to
    # edit the same inline message throughout the whole save lifecycle.
    if event is not None:
        try:
            return await event.edit(text, parse_mode="html", buttons=buttons)
        except Exception as exc:
            last_exc = exc

    # Fallback for normal bot-owned messages / environments where event.edit is
    # unavailable after the callback has returned.
    try:
        return await _edit_panel_message(
            text=text,
            buttons=buttons,
            inline_message_id=editor.get("inline_message_id"),
            chat_id=editor.get("chat_id"),
            message_id=editor.get("message_id"),
            parse_mode="html",
        )
    except Exception as exc:
        last_exc = exc
        raise last_exc


def _cs_progress_bar(done, total, width=16):
    if total <= 0:
        return "░" * width, 0
    ratio = max(0.0, min(1.0, float(done) / float(total)))
    filled = round(width * ratio)
    return "█" * filled + "░" * (width - filled), int(ratio * 100)


def _cs_media_match(message, kind):
    if kind == "photos":
        return bool(getattr(message, "photo", None))
    if kind == "videos":
        return bool(getattr(message, "video", None))
    if kind == "music":
        return bool(getattr(message, "audio", None)) and not bool(getattr(message, "voice", None))
    if kind == "voice":
        return bool(getattr(message, "voice", None))
    if kind == "text":
        return bool((getattr(message, "raw_text", "") or "").strip()) and not bool(getattr(message, "media", None))
    return bool(getattr(message, "media", None) or (getattr(message, "raw_text", "") or "").strip())


async def _cs_channels(client):
    """Return broadcast channels the logged-in self account can actually read."""
    result = []
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not isinstance(entity, types.Channel):
            continue
        if not getattr(entity, "broadcast", False):
            continue
        if getattr(entity, "megagroup", False):
            continue
        title = (getattr(entity, "title", None) or getattr(dialog, "name", None) or "بدون نام").strip()
        result.append({
            "id": int(entity.id),
            "access_hash": int(entity.access_hash) if getattr(entity, "access_hash", None) is not None else None,
            "title": title,
            "username": getattr(entity, "username", None),
        })
    result.sort(key=lambda x: x["title"].casefold())
    return result


def _cs_channel_buttons(uid, channels):
    rows = []
    for idx, item in enumerate(channels):
        title = item["title"]
        if len(title) > 42:
            title = title[:39] + "..."
        rows.append([btn(f"📢 {title}", _self_cb(uid, f"cs_pick:{idx}"), "primary")])
    rows.append([btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")])
    return rows


def _cs_media_buttons(uid):
    return [
        [btn("🖼 تصویر", _self_cb(uid, "cs_media:photos"), "primary"), btn("🎬 ویدیو", _self_cb(uid, "cs_media:videos"), "primary")],
        [btn("🎵 موسیقی", _self_cb(uid, "cs_media:music"), "primary"), btn("🎤 ویس", _self_cb(uid, "cs_media:voice"), "primary")],
        [btn("📝 متن", _self_cb(uid, "cs_media:text"), "primary"), btn("📦 همه", _self_cb(uid, "cs_media:all"), "primary")],
        [btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")],
    ]


def _cs_count_buttons(uid, value):
    return [
        [btn("1", _self_cb(uid, "cs_num:1")), btn("2", _self_cb(uid, "cs_num:2")), btn("3", _self_cb(uid, "cs_num:3"))],
        [btn("4", _self_cb(uid, "cs_num:4")), btn("5", _self_cb(uid, "cs_num:5")), btn("6", _self_cb(uid, "cs_num:6"))],
        [btn("7", _self_cb(uid, "cs_num:7")), btn("8", _self_cb(uid, "cs_num:8")), btn("9", _self_cb(uid, "cs_num:9"))],
        [btn("⌫", _self_cb(uid, "cs_back"), "danger"), btn("0", _self_cb(uid, "cs_num:0")), btn("🗑", _self_cb(uid, "cs_clear"), "danger")],
        [btn("✅ تأیید و شروع", _self_cb(uid, "cs_confirm"), "success")],
        [btn("🔙 بازگشت", _self_cb(uid, "cs_media_back"), "primary")],
    ]


def _cs_count_text(state):
    labels = {"photos":"تصویر", "videos":"ویدیو", "music":"موسیقی", "voice":"ویس", "text":"متن", "all":"همه مدیاها"}
    return (
        f"💾 <b>ذخیره چنل</b>\n\n"
        f"📢 چنل: <b>{html.escape(state['channel_title'])}</b>\n"
        f"📦 نوع: <b>{labels.get(state['media'], state['media'])}</b>\n\n"
        f"🔢 تعداد: <b>{state.get('count', 0)}</b>\n\n"
        f"تعداد موردنظر را انتخاب کن و بعد «تأیید و شروع» را بزن."
    )


async def _cs_save_one(client, entity, message):
    """Save one channel message into Saved Messages, with a safe fallback."""
    # First choice: server-side copy/forward. This preserves the original media
    # without downloading large files through the bot process.
    try:
        result = await _tg_call_with_flood_retry(
            lambda: client.forward_messages("me", message, from_peer=entity),
            label="save channel message",
        )
        return bool(result)
    except Exception as forward_exc:
        # Protected content may reject forwarding. For downloadable media/text,
        # try a real re-upload/copy as a fallback.
        if not getattr(message, "media", None):
            try:
                await _tg_call_with_flood_retry(
                    lambda: client.send_message("me", getattr(message, "raw_text", "") or ""),
                    label="save text fallback",
                )
                return True
            except Exception:
                raise forward_exc

        tmp_dir = Path(tempfile.mkdtemp(prefix="channel_save_"))
        try:
            path = await _tg_call_with_flood_retry(
                lambda: client.download_media(message, file=str(tmp_dir)),
                label="download protected channel media",
            )
            if not path:
                raise forward_exc
            await _tg_call_with_flood_retry(
                lambda: client.send_file(
                    "me", path, caption=(getattr(message, "raw_text", "") or "")[:4096]
                ),
                label="upload protected channel media",
            )
            return True
        except Exception:
            raise forward_exc
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _cs_worker(uid, client, state, editor):
    current = asyncio.current_task()
    requested = int(state["count"])
    kind = state["media"]
    title = state["channel_title"]
    success = 0
    failed = 0
    found = 0
    last_ui = 0.0

    async def update(text, buttons=None):
        try:
            await _cs_edit(editor, text, buttons)
        except Exception as exc:
            # UI failure must never kill the actual save operation.
            print(f"[CHANNEL_SAVE {uid}] progress edit failed: {exc}")

    try:
        access_hash = state.get("access_hash")
        if access_hash is not None:
            entity = await _tg_call_with_flood_retry(
                lambda: client.get_entity(types.InputPeerChannel(int(state["channel_id"]), int(access_hash))),
                label="resolve save channel",
            )
        else:
            entity = await _tg_call_with_flood_retry(
                lambda: client.get_entity(int(state["channel_id"])),
                label="resolve save channel",
            )

        await update(
            f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(title)}</b>\n\n"
            "🔎 در حال پیدا کردن پیام‌های موردنظر...\n"
            f"📦 درخواست: <b>{requested}</b>\n"
            "⏳ لطفاً صبر کن..."
        )

        selected = []
        async for message in client.iter_messages(entity):
            if not message or not _cs_media_match(message, kind):
                continue
            selected.append(message)
            found = len(selected)
            now = time.monotonic()
            if now - last_ui >= CHANNEL_SAVE_PROGRESS_INTERVAL or found == requested:
                last_ui = now
                scan_percent = min(20, int(found * 20 / max(1, requested)))
                scan_bar, _ = _cs_progress_bar(scan_percent, 100)
                await update(
                    f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(title)}</b>\n\n"
                    f"🔎 در حال یافتن پیام‌ها... <b>{found}/{requested}</b>\n"
                    f"<code>{scan_bar}</code> <b>{scan_percent}%</b>\n\n"
                    "⏳ تاریخچه در حال بررسی است..."
                )
            if len(selected) >= requested:
                break

        if not selected:
            await update(
                f"❌ <b>ذخیره چنل</b>\n\n📢 {html.escape(title)}\n\n"
                "هیچ مورد قابل ذخیره‌ای با نوع انتخاب‌شده پیدا نشد.",
                [[btn("🔙 بازگشت به پنل", _self_cb(uid, "panel"), "primary")]],
            )
            return

        selected.reverse()  # oldest -> newest for a natural Saved Messages order
        total = len(selected)
        await update(
            f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(title)}</b>\n\n"
            f"🔄 شروع ذخیره <b>{total}</b> مورد...\n"
            f"<code>{_cs_progress_bar(0, total)[0]}</code> <b>0%</b>\n\n"
            "✅ موفق: 0\n❌ ناموفق: 0"
        )

        last_ui = 0.0
        for index, message in enumerate(selected, 1):
            try:
                if await _cs_save_one(client, entity, message):
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                print(f"[CHANNEL_SAVE {uid}] item {index} failed: {exc}")

            now = time.monotonic()
            # Update the same Telegram message at a safe cadence.  For short
            # jobs every item is shown; for large jobs we avoid FloodWait while
            # still guaranteeing a moving percentage and a final 100% update.
            if index == total or total <= 20 or (now - last_ui) >= CHANNEL_SAVE_PROGRESS_INTERVAL:
                last_ui = now
                bar, percent = _cs_progress_bar(index, total)
                await update(
                    f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(title)}</b>\n\n"
                    f"<code>{bar}</code> <b>{percent}%</b>\n\n"
                    f"📦 پیشرفت: <b>{index}/{total}</b>\n"
                    f"✅ موفق: <b>{success}</b>\n"
                    f"❌ ناموفق: <b>{failed}</b>"
                )

        status = "✅ ذخیره با موفقیت کامل شد" if failed == 0 else "⚠️ ذخیره با تعدادی خطا تمام شد"
        await update(
            f"{status}\n\n"
            f"📢 چنل: <b>{html.escape(title)}</b>\n"
            f"📦 درخواست: <b>{requested}</b>\n"
            f"📚 پیدا شده: <b>{total}</b>\n"
            f"✅ ذخیره‌شده: <b>{success}</b>\n"
            f"❌ ناموفق: <b>{failed}</b>\n\n"
            "📁 موارد موفق در <b>Saved Messages</b> ذخیره شدند.",
            [[btn("🔙 بازگشت به پنل", _self_cb(uid, "panel"), "primary")]],
        )

    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await update(
                "⚠️ <b>عملیات ذخیره متوقف شد.</b>",
                [[btn("🔙 بازگشت به پنل", _self_cb(uid, "panel"), "primary")]],
            )
        raise
    except Exception as exc:
        logging.exception("channel save worker crashed")
        await update(
            "❌ <b>ذخیره چنل با خطا متوقف شد.</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>",
            [[btn("🔙 بازگشت به پنل", _self_cb(uid, "panel"), "primary")]],
        )
    finally:
        if _channel_save_tasks.get(uid) is current:
            _channel_save_tasks.pop(uid, None)
        session = _cs_session(uid)
        if session and session.get("editor") == editor:
            # Keep the final message visible; only remove in-memory state.
            _cs_clear(uid)


async def _cs_open(event, uid):
    if uid in _channel_save_tasks and not _channel_save_tasks[uid].done():
        await safe_answer(event, "⏳ یک ذخیره‌سازی در حال اجراست.", True)
        return True
    client = self_clients.get(uid)
    if not client:
        await event.edit("❌ سلف فعال نیست. ابتدا سلف را فعال کن.", parse_mode="html", buttons=self_panel_buttons(uid))
        return True
    try:
        channels = await _cs_channels(client)
        if not channels:
            await event.edit(
                "💾 <b>ذخیره چنل</b>\n\n❌ هیچ چنل قابل دسترسی پیدا نشد.",
                parse_mode="html",
                buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
            )
            return True
        _channel_save_sessions[uid] = {"step": "channel", "channels": channels, "editor": _cs_editor_from_event(event)}
        await event.edit(
            "💾 <b>ذخیره چنل</b>\n\nچنلی را که می‌خواهی از آن ذخیره کنی انتخاب کن:",
            parse_mode="html",
            buttons=_cs_channel_buttons(uid, channels),
        )
    except Exception as exc:
        logging.exception("channel list failed")
        await event.edit(
            "❌ <b>لیست چنل‌ها دریافت نشد.</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
    return True

async def safe_callback_edit(event, *args, **kwargs):
    """Edit a callback message and ignore Telegram's harmless no-op error."""
    try:
        return await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        return None


async def handle_self_panel_callback(event):
    data = event.data.decode("utf-8", errors="ignore")
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "sp":
        return False
    try:
        uid = int(parts[1])
    except ValueError:
        await safe_answer(event, "❌ پنل نامعتبر است.", True)
        return True

    if event.sender_id != uid:
        await safe_answer(event, "❌ این پنل متعلق به شما نیست.", True)
        return True

    action = parts[2]


    await safe_answer(event)

    if action == "cs_open":
        return await _cs_open(event, uid)

    if action.startswith("cs_"):
        session = _cs_session(uid)
        if not session:
            await safe_answer(event, "❌ این عملیات دیگر فعال نیست.", True)
            return True

        if action.startswith("cs_pick:"):
            try:
                idx = int(action.split(":", 1)[1])
                item = session["channels"][idx]
            except (ValueError, IndexError, KeyError):
                await safe_answer(event, "❌ چنل انتخابی معتبر نیست.", True)
                return True
            session.update({
                "step": "media",
                "channel_id": item["id"],
                "access_hash": item.get("access_hash"),
                "channel_title": item["title"],
            })
            await safe_callback_edit(event, 
                f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(item['title'])}</b>\n\nنوع مدیا را انتخاب کن:",
                parse_mode="html", buttons=_cs_media_buttons(uid)
            )
            return True

        if action.startswith("cs_media:"):
            kind = action.split(":", 1)[1]
            if kind not in {"photos", "videos", "music", "voice", "text", "all"} or session.get("step") != "media":
                return True
            session.update({"step": "count", "media": kind, "count": 0})
            await safe_callback_edit(event, _cs_count_text(session), parse_mode="html", buttons=_cs_count_buttons(uid, 0))
            return True

        if action == "cs_media_back":
            session["step"] = "media"
            await safe_callback_edit(event, 
                f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(session.get('channel_title','چنل'))}</b>\n\nنوع مدیا را انتخاب کن:",
                parse_mode="html", buttons=_cs_media_buttons(uid)
            )
            return True

        if action.startswith("cs_num:") and session.get("step") == "count":
            digit = action.split(":", 1)[1]
            if digit.isdigit():
                value = str(session.get("count", 0))
                value = "" if value == "0" else value
                candidate = (value + digit).lstrip("0") or "0"
                if len(candidate) <= 4 and int(candidate) <= CHANNEL_SAVE_MAX_COUNT:
                    session["count"] = int(candidate)
                else:
                    await safe_answer(event, f"⚠️ حداکثر {CHANNEL_SAVE_MAX_COUNT} مورد است.", True)
                    return True
            await safe_callback_edit(event, _cs_count_text(session), parse_mode="html", buttons=_cs_count_buttons(uid, session["count"]))
            return True

        if action == "cs_back" and session.get("step") == "count":
            value = str(session.get("count", 0))
            session["count"] = int(value[:-1] or "0")
            await safe_callback_edit(event, _cs_count_text(session), parse_mode="html", buttons=_cs_count_buttons(uid, session["count"]))
            return True

        if action == "cs_clear" and session.get("step") == "count":
            session["count"] = 0
            await safe_callback_edit(event, _cs_count_text(session), parse_mode="html", buttons=_cs_count_buttons(uid, 0))
            return True

        if action == "cs_confirm" and session.get("step") == "count":
            count = int(session.get("count", 0))
            if count < 1:
                await safe_answer(event, "⚠️ ابتدا تعداد را انتخاب کن.", True)
                return True
            if uid in _channel_save_tasks and not _channel_save_tasks[uid].done():
                await safe_answer(event, "⏳ یک عملیات ذخیره در حال اجراست.", True)
                return True
            client = self_clients.get(uid)
            if not client:
                await safe_callback_edit(event, "❌ سلف فعال نیست.", parse_mode="html", buttons=self_panel_buttons(uid))
                _cs_clear(uid)
                return True

            # Capture the exact message identity once. The same message is edited
            # for the whole lifecycle; a UI edit failure must never cancel saving.
            editor = _cs_editor_from_event(event)
            session["step"] = "processing"
            session["count"] = count
            session["editor"] = editor
            worker_state = dict(session)

            # Give the user immediate feedback when possible, but never make this
            # cosmetic edit a prerequisite for the actual save worker.
            with contextlib.suppress(Exception):
                await safe_callback_edit(event, 
                    f"💾 <b>ذخیره چنل</b>\n\n📢 <b>{html.escape(session['channel_title'])}</b>\n\n"
                    "⏳ در حال آماده‌سازی...\n"
                    "<code>░░░░░░░░░░░░░░░░</code> <b>0%</b>",
                    parse_mode="html",
                    buttons=None,
                )

            task = asyncio.create_task(_cs_worker(uid, client, worker_state, editor))
            _channel_save_tasks[uid] = task
            return True

        return True

    if action == "panel":
        _first_comment_channel_sessions.pop(int(uid), None)

    if action == "close":
        _first_comment_channel_sessions.pop(int(uid), None)
        # The panel is a bot-owned inline-result message.  Do not delete it:
        # edit it and remove every button, so the user gets a visible
        # confirmation instead of a dead/unchanged inline panel.
        await safe_answer(event, "پنل با موفقیت بسته شد.")
        with contextlib.suppress(Exception):
            await safe_callback_edit(event, "✅ پنل با موفقیت بسته شد.", parse_mode="html", buttons=None)
        return True
    if action == "comment_setup":
        try:
            client=self_clients.get(int(uid))
            if not client:
                raise RuntimeError("SELF session is not connected")

            # IMPORTANT: channel discovery and rendering deliberately mirror
            # «💾 ذخیره چنل».  Do not put channel ids/entities inside callback_data.
            # Only a tiny numeric index is sent; the full channel data stays in
            # this in-memory session. This avoids Telegram's reply-markup limit.
            channels=await _cs_channels(client)
            _first_comment_channel_sessions[int(uid)] = channels

            rows=[]
            for idx,item in enumerate(channels):
                title=item["title"]
                if len(title)>42:
                    title=title[:39]+"..."
                rows.append([btn(f"📢 {title}",_self_cb(uid,f"fc_pick:{idx}"),"primary")])

            if not rows:
                rows=[[btn("🔙 بازگشت",_self_cb(uid,"panel"),"primary")]]
                text="💬 <b>کامنت اول</b>\n\n❌ هیچ کانال پخشی که SELF به آن دسترسی دارد پیدا نشد."
            else:
                rows.append([btn("🔙 بازگشت",_self_cb(uid,"panel"),"primary")])
                text="💬 <b>کامنت اول</b>\n\nکانال را انتخاب کن:"

            await safe_callback_edit(event, text,parse_mode="html",buttons=rows)
        except Exception as exc:
            logging.exception("first comment channel list failed")
            await safe_callback_edit(event, 
                f"❌ <b>دریافت کانال‌ها ناموفق بود.</b>\n\n<code>{html.escape(str(exc))}</code>",
                parse_mode="html",
                buttons=[[btn("🔙 بازگشت",_self_cb(uid,"panel"),"primary")]]
            )
        return True

    if action.startswith("fc_pick:"):
        try:
            idx=int(action.split(":",1)[1])
            channels=_first_comment_channel_sessions.get(int(uid),[])
            item=channels[idx]
            cid=int(item["id"])
            client=self_clients.get(int(uid))
            if not client:
                raise RuntimeError("SELF session is not connected")
            if not client: raise RuntimeError("SELF session is not connected")
            entity=await client.get_entity(cid)
            if not isinstance(entity,types.Channel) or getattr(entity,"megagroup",False): raise RuntimeError("این مورد کانال پخش نیست")
            full=await client(functions.channels.GetFullChannelRequest(channel=entity))
            did=getattr(getattr(full,"full_chat",None),"linked_chat_id",None)
            if not did:
                await safe_callback_edit(event, f"❌ <b>{html.escape(getattr(entity,'title','کانال'))}</b>\n\nاین کانال Discussion متصل ندارد.",parse_mode="html",buttons=[[btn("🔄 انتخاب کانال دیگر",_self_cb(uid,"comment_setup"),"primary")],[btn("🏠 پنل اصلی",_self_cb(uid,"panel"),"danger")]])
                return True
            discussion=await client.get_entity(int(did)); old=_first_comment_config(uid,cid) or {}
            item={"id":int(entity.id),"access_hash":getattr(entity,"access_hash",None),"title":getattr(entity,"title","کانال"),"username":getattr(entity,"username",None),"discussion_id":int(did),"discussion_access_hash":getattr(discussion,"access_hash",None),"text":str(old.get("text") or "")[:4096],"enabled":bool(old.get("enabled",True))}
            _upsert_first_comment_config(uid,item); _set_comment_target(uid,cid)
            status="🟢 فعال" if item["enabled"] and item["text"] else ("🟡 بدون متن" if item["enabled"] else "🔴 خاموش")
            preview=html.escape(item["text"][:500]) if item["text"] else "❌ تنظیم نشده"
            await safe_callback_edit(event, f"💬 <b>کامنت اول</b>\n\n📢 <b>{html.escape(item['title'])}</b>\n💬 Discussion: <b>{html.escape(getattr(discussion,'title','گروه گفتگو'))}</b>\n\nوضعیت: <b>{status}</b>\n📝 متن فعلی: <blockquote>{preview}</blockquote>\n\nروی یک پیام متنی ریپلای کن و <code>تنظیم کامنت</code> بفرست.",parse_mode="html",buttons=[[btn("✏️ راهنمای تنظیم متن",_self_cb(uid,"comment_text_help"),"success"),btn("🗑 حذف تنظیم کانال",_self_cb(uid,"comment_remove"),"danger")],[btn("🔴 خاموش" if item["enabled"] else "🟢 فعال",_self_cb(uid,"comment_toggle"),"danger" if item["enabled"] else "success"),btn("🔄 کانال دیگر",_self_cb(uid,"comment_setup"),"primary")],[btn("🏠 پنل اصلی",_self_cb(uid,"panel"),"danger")]])
        except Exception as exc:
            await safe_callback_edit(event, f"❌ <b>تنظیم کانال ناموفق بود.</b>\n\n<code>{html.escape(str(exc))}</code>",parse_mode="html",buttons=[[btn("🔄 تلاش دوباره",_self_cb(uid,"comment_setup"),"primary")],[btn("🏠 پنل اصلی",_self_cb(uid,"panel"),"danger")]])
        return True

    if action == "comment_toggle":
        cid=_comment_target(uid); cfg=_first_comment_config(uid,cid) if cid else None
        if not cfg: await safe_answer(event,"❌ ابتدا کانال را انتخاب کن.",True); return True
        cfg["enabled"]=not bool(cfg.get("enabled",True)); _upsert_first_comment_config(uid,cfg)
        await safe_callback_edit(event, f"{'🟢 کامنت اول فعال شد.' if cfg['enabled'] else '🔴 کامنت اول خاموش شد.'}",parse_mode="html",buttons=[[btn("🔙 برگشت",_self_cb(uid,"comment_setup"),"primary")]])
        return True

    if action == "comment_remove":
        cid=_comment_target(uid)
        if not cid or not _remove_first_comment_config(uid,cid): await safe_answer(event,"❌ تنظیمی برای حذف پیدا نشد.",True); return True
        await safe_callback_edit(event, "✅ <b>تنظیمات این کانال کامل حذف شد.</b>",parse_mode="html",buttons=[[btn("📢 لیست کانال‌ها",_self_cb(uid,"comment_setup"),"primary")],[btn("🏠 پنل اصلی",_self_cb(uid,"panel"),"danger")]])
        return True

    if action == "comment_text_help":
        cid=_comment_target(uid); cfg=_first_comment_config(uid,cid) if cid else None
        if not cfg: await safe_answer(event,"❌ ابتدا کانال را انتخاب کن.",True); return True
        await safe_callback_edit(event, f"✏️ <b>تنظیم متن کامنت</b>\n\n📢 {html.escape(str(cfg.get('title') or 'کانال'))}\n\nروی یک پیام متنی ریپلای کن و بنویس:\n<code>تنظیم کامنت</code>\n\nمتن برای همین کانال ذخیره و کامنت اول فعال می‌شود.",parse_mode="html",buttons=[[btn("🔙 برگشت",_self_cb(uid,"comment_setup"),"primary")]])
        return True

    if action == "comment_help":
        await safe_callback_edit(event, "💬 <b>کامنت اول</b>\n\n📢 کانال را از لیست انتخاب کن.\n✏️ روی پیام متنی ریپلای + <code>تنظیم کامنت</code>\n🟢/🔴 فعال و خاموش از پنل همان کانال\n🗑 حذف تنظیمات از پنل همان کانال",parse_mode="html",buttons=[[btn("🔙 بازگشت",_self_cb(uid,"comment_setup"),"primary")]])
        return True

    if action == "secretary_help":
        await safe_callback_edit(event, 
            "🤵 <b>منشی</b>\n\n"
            "<code>تنظیم منشی</code> + ریپلای روی متن/مدیا\n"
            "<code>منشی روشن</code> / <code>منشی خاموش</code>\n"
            "<code>تنظیم زمان منشی 15</code>\n\n"
            "فقط پیوی؛ هر کاربر در هر بازه فقط یک پاسخ.",
            parse_mode="html", buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]]
        )
        return True
    if action == "group_help":
        await safe_callback_edit(event, 
            "🛡 <b>مدیریت گروه</b>\n\n"
            "<code>پین</code> / <code>حذف پین</code> با ریپلای\n"
            "<code>بن</code> یا <code>سیک</code> با ریپلای\n"
            "<code>آن بن</code> با ریپلای\n"
            "<code>بن سراسری @user</code>\n<code>حذف بن سراسری @user</code>\n<code>لیست بن سراسری</code>",
            parse_mode="html", buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]]
        )
        return True
    if action == "tag_help":
        await safe_callback_edit(event, 
            "🏷 <b>تگ اعضا</b>\n\n<code>تگ 20</code>\n<code>همه</code>\n\nپیام دستور حذف و تگ‌ها گروهی ارسال می‌شوند.",
            parse_mode="html", buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]]
        )
        return True
    if action == "guide":
        try:
            await safe_callback_edit(event, 
                "📚 <b>راهنمای قابلیت‌ها</b>\n\nقابلیت موردنظر را انتخاب کن:",
                parse_mode="html",
                buttons=self_feature_guide_buttons(uid),
            )
        except Exception as exc:
            print(f"[SELF {uid}] guide callback failed: {exc}")
            await safe_answer(event, "❌ راهنما باز نشد؛ دوباره تلاش کن.", True)
        return True
    if action == "ping":
        started = time.perf_counter()
        with contextlib.suppress(Exception):
            await safe_callback_edit(event, "🏓 <b>در حال محاسبه پینگ...</b>", parse_mode="html")
        latency = round((time.perf_counter() - started) * 1000, 2)
        await safe_callback_edit(event, 
            f"🏓 <b>پینگ سلف</b>\\n\\n⚡ <code>{latency} ms</code>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True

    if action == "banners":
        banners = self_banners(uid)
        value = self_get(uid, "banner_auto", "off")
        status = "روشن ✅" if value == "on" else "خاموش ❌"
        body = [f"📢 <b>مدیریت بنرها</b>\n\n🔘 ارسال خودکار: {status}"]
        for b in banners:
            body.append(
                f"\n<b>#{int(b['id'])}</b> • "
                f"{'فوروارد' if b.get('mode') == 'forward' else 'کپی'} • "
                f"هر {int(b.get('interval', 60))} دقیقه • "
                f"مقصد: {len(b.get('targets', []))}"
            )
        if not banners:
            body.append("\nهنوز بنری ثبت نشده است.")
        await safe_callback_edit(event, 
            "".join(body), parse_mode="html",
            buttons=[
                [btn("🟢 روشن کردن تبچی" if value != "on" else "🔴 خاموش کردن تبچی", _self_cb(uid, "banner_toggle"), "success" if value != "on" else "danger")],
                [btn("📚 راهنمای دستورات بنر", _self_cb(uid, "banner_help"), "primary")],
                [btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")],
            ],
        )
        return True

    if action == "banner_toggle":
        current = self_get(uid, "banner_auto", "off")
        value = "off" if current == "on" else "on"
        sent = failed = 0
        if value == "on":
            client = _get_tabchi_client(uid)
            if not client:
                await safe_callback_edit(event, 
                    "❌ <b>سلف فعال نیست.</b>\n\nتبچی فقط با اکانت SELF اجرا می‌شود و با BOT ارسال نخواهد کرد.",
                    parse_mode="html",
                )
                return
            self_set(uid, "banner_auto", value)
            sent, failed = await _banner_dispatch_all_configured(client, uid)
        else:
            self_set(uid, "banner_auto", value)
        banners = self_banners(uid)
        status = "روشن ✅" if value == "on" else "خاموش ❌"
        body = [f"📢 <b>مدیریت بنرها</b>\n\n🔘 ارسال خودکار: {status}"]
        if value == "on" and (sent or failed):
            body.append(f"\n📨 ارسال فوری: {sent} مقصد")
            if failed:
                body.append(f"\n⚠️ ناموفق: {failed}")
        for b in banners:
            body.append(
                f"\n<b>#{int(b['id'])}</b> • "
                f"{'فوروارد' if b.get('mode') == 'forward' else 'کپی'} • "
                f"هر {int(b.get('interval', 60))} دقیقه • "
                f"مقصد: {len(b.get('targets', []))}"
            )
        if not banners:
            body.append("\nهنوز بنری ثبت نشده است.")
        await safe_callback_edit(event, 
            "".join(body), parse_mode="html",
            buttons=[
                [btn("🟢 روشن کردن تبچی" if value != "on" else "🔴 خاموش کردن تبچی", _self_cb(uid, "banner_toggle"), "success" if value != "on" else "danger")],
                [btn("📚 راهنمای دستورات بنر", _self_cb(uid, "banner_help"), "primary")],
                [btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")],
            ],
        )
        return True

    if action == "banner_help":
        await safe_callback_edit(event, 
            "🤖 <b>راهنمای تبچی</b>\n<i>مدیریت بنر، مقصدها و ارسال خودکار</i>\n\n"
            "<blockquote>📌 <b>نکته:</b> عدد یعنی شماره بنر؛ مثلاً <code>۱</code>.</blockquote>\n\n"
            "<b>① روشن / خاموش</b>\n<code>تبچی روشن</code>\n<code>تبچی خاموش</code>\n"
            "فعال یا غیرفعال‌کردن ارسال خودکار بنرها.\n\n"
            "<b>② ساخت بنر</b>\nروی پیام موردنظر ریپلای کن:\n"
            "<code>تنظیم بنر فور</code> → فوروارد\n"
            "<code>تنظیم بنر کپی</code> → کپی پیام\n\n"
            "<b>③ حذف و پاکسازی</b>\n<code>حذف بنر ۱</code>\n<code>پاکسازی لیست بنر ها</code>\n\n"
            "<b>④ زمان‌بندی</b>\n<code>تنظیم عدد بنر ۱ ۳۰ دقیقه</code>\n"
            "بنر ۱ را هر ۳۰ دقیقه ارسال می‌کند.\n\n"
            "<b>⑤ مقصد یک گروه</b>\nداخل گروه هدف: <code>تنظیم گپ هدف بنر ۱</code>\n"
            "حذف همان مقصد: <code>حذف گپ هدف بنر ۱</code>\n\n"
            "<b>⑥ تمام گروه‌ها</b>\n<code>تنظیم هدف بنر ۱ تمام گپ ها</code>\n"
            "تمام گروه‌های قابل دسترسی سلف را مقصد بنر می‌کند.\n\n"
            "<b>⑦ ارسال فوری به پیوی‌ها</b>\n<code>فور بنر ۱ در ۲۰ پیوی اخیر</code>\n"
            "بنر ۱ را همان لحظه برای ۲۰ پیوی اخیر ارسال می‌کند.\n\n"
            "<blockquote>💡 <b>ترتیب پیشنهادی:</b> ریپلای پیام → ساخت بنر → تعیین مقصد → تعیین زمان → تبچی روشن.</blockquote>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "banners"), "primary")]],
        )
        return True

    if action == "currency":
        await safe_callback_edit(event, 
            "💱 <b>نرخ لحظه‌ای ارز</b>\n\n"
            "قیمت را با این دستور بگیر:\n\n"
            "<code>قیمت BTC</code>\n<code>قیمت ETH</code>\n<code>قیمت SOL</code>\n<code>قیمت USDT</code>\n\n"
            "⚡ دریافت مستقیم از سرویس عمومی بدون API Key؛ در صورت خطا fallback فعال است.",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True
    if action == "logo":
        await safe_callback_edit(event, 
            "🎨 <b>لوگوساز</b>\n\n"
            "ساخت لوگو با ۱۲ قالب داخلی و رایگان:\n<code>لوگو 12 HusteRIX</code>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True


    if action == "feature_help_menu":
        await safe_callback_edit(event, "📚 <b>راهنمای قابلیت‌ها</b>\n\nقابلیت موردنظر را انتخاب کن:", parse_mode="html", buttons=self_feature_guide_buttons(uid))
        return True
    if action.startswith("feature_help:"):
        key=action.split(":",1)[1]
        body=SELF_FEATURE_GUIDES.get(key)
        if not body:
            return True
        await safe_callback_edit(event, 
            body,
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "feature_help_menu"), "danger")]],
        )
        return True
    if action == "panel":
        await safe_callback_edit(event, self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action == "cleanup":
        await safe_callback_edit(event, 
            "🧹 <b>پاکسازی اکانت</b>\n\n"
            "هر بخش مستقل است و فقط همان بخش را پاک می‌کند.\n"
            "💾 Saved Messages دست‌نخورده می‌ماند.",
            parse_mode="html",
            buttons=[
                [btn("💬 پاکسازی چت‌ها (دوطرفه)", _self_cb(uid, "cleanup_choose:chats"), "danger")],
                [btn("👥 پاکسازی گپ‌ها", _self_cb(uid, "cleanup_choose:groups"), "danger")],
                [btn("📢 پاکسازی کانال‌ها", _self_cb(uid, "cleanup_choose:channels"), "danger")],
                [btn("👤 پاکسازی مخاطبین", _self_cb(uid, "cleanup_choose:contacts"), "danger")],
                [btn("🤖 پاکسازی و بلاک ربات‌ها", _self_cb(uid, "cleanup_choose:bots"), "danger")],
                [btn("🧹 پاکسازی همه", _self_cb(uid, "cleanup_choose:all"), "danger")],
                [btn("↩️ برگشت", _self_cb(uid, "panel"), "primary")],
            ],
        )
        return True

    if action.startswith("cleanup_choose:"):
        target = action.split(":", 1)[1]
        labels = {
            "chats": "💬 پاکسازی چت‌ها به‌صورت دوطرفه",
            "groups": "👥 پاکسازی گپ‌ها",
            "channels": "📢 پاکسازی کانال‌ها",
            "contacts": "👤 پاکسازی مخاطبین",
            "bots": "🤖 پاکسازی و بلاک ربات‌ها",
            "all": "🧹 پاکسازی همه",
        }
        if target not in labels:
            return True
        await safe_callback_edit(event, 
            f"⚠️ <b>{labels[target]}</b>\n\n"
            "این عملیات قابل برگشت نیست.\n"
            "Saved Messages دست‌نخورده می‌ماند.\n\n"
            "برای شروع تأیید کن:",
            parse_mode="html",
            buttons=[
                [btn("⚠️ تأیید و اجرا", _self_cb(uid, f"cleanup_confirm:{target}"), "danger")],
                [btn("↩️ برگشت به پاکسازی", _self_cb(uid, "cleanup"), "primary")],
            ],
        )
        return True

    if action.startswith("cleanup_confirm:"):
        target = action.split(":", 1)[1]
        if target not in {"chats", "groups", "channels", "contacts", "bots", "all"}:
            return True
        if self_get(uid, "cleanup_running", "off") == "on":
            await safe_callback_edit(event, "⏳ یک پاکسازی همین الان در حال اجراست.", parse_mode="html", buttons=self_panel_buttons(uid))
            return True
        client = self_clients.get(uid)
        if not client:
            await safe_callback_edit(event, "❌ سلف فعال نیست.", buttons=self_panel_buttons(uid))
            return True
        _cleanup_panel_messages[uid] = (
            getattr(event, "chat_id", None),
            getattr(event, "message_id", None),
            _event_inline_message_id(event),
        )
        task = asyncio.create_task(
            _cleanup_run(
                uid,
                target,
                getattr(event, "chat_id", None),
                getattr(event, "message_id", None),
                _event_inline_message_id(event),
            )
        )
        _cleanup_tasks[uid] = task
        await safe_callback_edit(event, "⏳ پاکسازی شروع شد…\nپیشرفت لحظه‌ای در همین پنل نمایش داده می‌شود.", parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action == "lock_help":
        await safe_callback_edit(event, 
            self_panel_text(uid) + "\n\n🔒 روی پیام کاربر در پیوی ریپلای کن و بنویس: <b>قفل چت</b>\nبرای خاموش‌کردن: <b>بازکردن قفل چت</b>",
            parse_mode="html", buttons=self_panel_buttons(uid)
        )
        return True

    if action == "block_help":
        await safe_callback_edit(event, 
            self_panel_text(uid) + "\n\n🚫 داخل گروه روی پیام کاربر ریپلای کن و بنویس: <b>بلاک + ریپلای</b>",
            parse_mode="html", buttons=self_panel_buttons(uid)
        )
        return True

    toggles = {
        "time": "time_name", "bold": "bold", "persian": "persian_font",
        "translate": "translate", "read": "auto_read", "typing": "typing",
        "game": "game_mode", "autoreply": "auto_reply",
    }
    if action in toggles:
        key = toggles[action]
        current = self_get(uid, key, "off")
        self_set(uid, key, "off" if current == "on" else "on")
        await safe_callback_edit(event, self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action == "clockfont":
        names = list(SELF_CLOCK_FONTS)
        cur = self_get(uid, "clock_font", "normal")
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self_set(uid, "clock_font", nxt)

        # Apply the selected font immediately to the profile name instead of
        # waiting for the worker's next polling tick.
        client = self_clients.get(uid)
        if client and time_name_enabled(uid):
            with contextlib.suppress(Exception):
                await update_time_name(uid, client)

        await safe_callback_edit(event, 
            self_panel_text(uid) + "\n\n" + self_font_preview(uid, "clock"),
            parse_mode="html",
            buttons=self_panel_buttons(uid),
        )
        return True

    if action == "engfont":
        names = list(SELF_ENGLISH_FONTS)
        cur = self_get(uid, "english_font", "normal")
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self_set(uid, "english_font", nxt)
        await safe_callback_edit(event, 
            self_panel_text(uid) + "\n\n" + self_font_preview(uid, "english"),
            parse_mode="html",
            buttons=self_panel_buttons(uid),
        )
        return True

    if action == "autoreply":
        current = self_get(uid, "auto_reply", "off")
        self_set(uid, "auto_reply", "off" if current == "on" else "on")
        mapping = self_auto_reply_map(uid)
        status = "روشن ✅" if self_get(uid, "auto_reply") == "on" else "خاموش ❌"
        await safe_callback_edit(event, 
            f"💬 <b>پاسخ خودکار</b>\\n\\nوضعیت: {status}\\nکلمات ثبت‌شده: {len(mapping)}\\n\\n"
            "دستورات:\\n"
            "<code>پاسخ خودکار جدید [کلمه]</code>\\n"
            "<code>ذخیره پاسخ خودکار [کلمه]</code> + ریپلای\\n"
            "<code>حذف پاسخ خودکار [کلمه]</code>\\n"
            "<code>لیست پاسخ خودکار</code>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True

    if action == "reaction":
        await safe_callback_edit(event, 
            self_panel_text(uid) + "\n\n❤️ برای فعال‌سازی: روی پیام کاربر ریپلای کن و «ریاکشن ❤️» بفرست.\nبرای حذف: «حذف ریاکشن»." ,
            parse_mode="html",
            buttons=self_panel_buttons(uid),
        )
        return True

    return True



async def _media_reply_message(event):
    if not event.is_reply:
        return None, "❌ روی عکس یا استیکر ریپلای کن."
    replied = await event.get_reply_message()
    if not replied:
        return None, "❌ پیام ریپلای‌شده پیدا نشد."
    return replied, None


def _is_animated_image(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1)
    except Exception:
        return False


def _image_to_webp(path, out_path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
        # Telegram sticker canvas: max 512x512, transparent background preserved.
        im.thumbnail((512, 512), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        x = (512 - im.width) // 2
        y = (512 - im.height) // 2
        canvas.alpha_composite(im, (x, y))
        canvas.save(out_path, "WEBP", lossless=True, quality=95, method=6)


def _animated_to_gif(path, out_path):
    # Pillow handles GIF and animated WebP. TGS (Telegram animated sticker)
    # is decoded with python-lottie when available; WebM falls back to ffmpeg.
    if str(path).lower().endswith(".tgs"):
        try:
            from lottie.parsers.tgs import parse_tgs
            from lottie.exporters.gif import export_gif
            animation = parse_tgs(str(path))
            export_gif(animation, str(out_path))
            return out_path.exists()
        except Exception:
            pass
    try:
        from PIL import Image
        with Image.open(path) as im:
            if getattr(im, "is_animated", False):
                frames = []
                durations = []
                for i in range(getattr(im, "n_frames", 1)):
                    im.seek(i)
                    frame = im.convert("RGBA")
                    frames.append(frame.copy())
                    durations.append(int(im.info.get("duration", 100) or 100))
                if frames:
                    frames[0].save(
                        out_path, "GIF", save_all=True, append_images=frames[1:],
                        duration=durations, loop=0, disposal=2
                    )
                    return True
    except Exception:
        pass

    import subprocess
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-vf", "fps=15,scale='min(512,iw)':-1:flags=lanczos",
             "-loop", "0", str(out_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
        )
        return out_path.exists()
    except Exception:
        return False


async def _self_image_to_sticker(event, uid, keep_reply=False):
    replied, error = await _media_reply_message(event)
    if error:
        return error
    if not getattr(replied, "photo", False):
        return "❌ پیام ریپلای‌شده عکس نیست."
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_sticker_{uid}_"))
    try:
        src = await replied.download_media(file=str(tmp_dir))
        if not src:
            return "❌ دانلود عکس ناموفق بود."
        out = tmp_dir / "sticker.webp"
        _image_to_webp(src, out)
        await event.client.send_file(
            event.chat_id, str(out), force_document=False,
            caption=None, reply_to=(replied.id if keep_reply else None),
            attributes=[types.DocumentAttributeSticker(alt="🙂", stickerset=types.InputStickerSetEmpty(), mask=False)]
        )
        return "✅ عکس به استیکر تبدیل شد."
    except ImportError:
        return "❌ برای تبدیل عکس به استیکر نصب Pillow لازم است."
    except Exception as exc:
        print(f"[SELF {uid}] image->sticker failed: {exc}")
        return "❌ تبدیل عکس به استیکر انجام نشد."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _self_sticker_to_photo(event, uid, keep_reply=False):
    replied, error = await _media_reply_message(event)
    if error:
        return error
    if not getattr(replied, "sticker", False):
        return "❌ پیام ریپلای‌شده استیکر نیست."
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_photo_{uid}_"))
    try:
        src = await replied.download_media(file=str(tmp_dir))
        if not src:
            return "❌ دانلود استیکر ناموفق بود."
        animated = bool(getattr(replied, "gif", False) or _is_animated_image(src) or str(src).lower().endswith((".tgs", ".webm")))
        reply_to = replied.id if keep_reply else None
        if animated:
            out = tmp_dir / "sticker.gif"
            if not _animated_to_gif(src, out):
                return "❌ استیکر متحرک بود، اما تبدیل آن به GIF انجام نشد. برای TGS نصب python-lottie هم لازم است."
            await event.client.send_file(event.chat_id, str(out), force_document=False, reply_to=reply_to)
        else:
            from PIL import Image
            out = tmp_dir / "photo.png"
            with Image.open(src) as im:
                im.convert("RGBA").save(out, "PNG")
            await event.client.send_file(event.chat_id, str(out), force_document=False, reply_to=reply_to)
        return "✅ استیکر به تصویر تبدیل شد."
    except ImportError:
        return "❌ برای تبدیل استیکر به تصویر نصب Pillow لازم است."
    except Exception as exc:
        print(f"[SELF {uid}] sticker->photo failed: {exc}")
        return "❌ تبدیل استیکر به تصویر انجام نشد."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _self_save_replied_message(event, uid):
    """Save the replied message/media into Telegram Saved Messages."""
    if not event.is_reply:
        return "❌ روی پیام موردنظر ریپلای کن و سپس «دانلود» را بفرست."
    replied = await event.get_reply_message()
    if not replied:
        return "❌ پیام ریپلای‌شده پیدا نشد."

    client = event.client
    try:
        await client.forward_messages("me", replied, from_peer=event.chat_id)
        return "✅ پیام با موفقیت به پیام‌های ذخیره‌شده ارسال شد."
    except Exception:
        pass

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_save_{uid}_"))
    try:
        if replied.media:
            path = await replied.download_media(file=str(tmp_dir))
            if not path:
                return "❌ این رسانه قابل دانلود نیست یا زمان آن تمام شده است."
            await client.send_file("me", path, caption=replied.raw_text or "")
        else:
            await client.send_message("me", replied.raw_text or "")
        return "✅ پیام به پیام‌های ذخیره‌شده منتقل شد."
    except Exception as exc:
        print(f"[SELF {uid}] save message failed: {exc}")
        return "❌ ذخیره پیام انجام نشد؛ ممکن است پیام محافظت‌شده یا منقضی‌شده باشد."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _normalize_persian_transcript(text: str) -> str:
    """Conservative Persian cleanup after Whisper; never uses a cloud API."""
    if not text:
        return ""
    table = str.maketrans({
        "ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "هٔ", "ة": "ه",
        "ؤ": "و", "إ": "ا", "أ": "ا", "ٱ": "ا",
        "٠": "۰", "١": "۱", "٢": "۲", "٣": "۳", "٤": "۴",
        "٥": "۵", "٦": "۶", "٧": "۷", "٨": "۸", "٩": "۹",
    })
    text = text.translate(table)
    text = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"([؟!،؛,:.])\1{1,}", r"\1", text)
    text = re.sub(r" +([،؛؟!:.])", r"\1", text)
    text = re.sub(r"([،؛؟!])(?=\S)", r"\1 ", text)
    return text.strip()


async def _self_transcribe_reply(event, uid):
    """Voice/audio -> high-accuracy Persian text using local faster-whisper only."""
    if not event.is_reply:
        return "❌ روی ویس یا فایل صوتی ریپلای کن و «متن» را بفرست."
    replied = await event.get_reply_message()
    if not replied or _message_media_kind(replied) not in {"voice", "audio"}:
        return "❌ ویس یا فایل صوتی پیدا نشد."

    state = stt_state.setdefault(uid, {"status": "processing", "started": time.monotonic(), "last_ui": 0.0, "percent": 0})

    async def progress_loop():
        phases = (
            "🎙️ ویس دانلود شد…", "🔊 در حال آماده‌سازی صدا…",
            f"🧠 در حال بارگذاری {WHISPER_MODEL}…",
            "🧠 در حال تشخیص گفتار فارسی…", "✍️ در حال مرتب‌سازی متن فارسی…",
        )
        idx = 0
        while True:
            with contextlib.suppress(Exception):
                elapsed = int(time.monotonic() - state.get("started", time.monotonic()))
                await event.edit(
                    f"{phases[idx % len(phases)]}\n\n⏳ زمان پردازش: {elapsed} ثانیه\n▰▱▱▱▱  در حال انجام",
                    parse_mode="html",
                )
            idx += 1
            await asyncio.sleep(2.5)

    progress_task = asyncio.create_task(progress_loop())
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_stt_{uid}_"))
    try:
        try:
            path = await asyncio.wait_for(replied.download_media(file=str(tmp_dir)), timeout=float(os.getenv("STT_DOWNLOAD_TIMEOUT_SECONDS", "300")))
        except asyncio.TimeoutError:
            return "❌ دانلود ویس بیش از حد طول کشید؛ دوباره تلاش کن."
        except Exception as exc:
            print(f"[SELF {uid}] STT download failed: {exc}")
            return "❌ دانلود ویس ناموفق بود."
        if not path:
            return "❌ دانلود ویس ناموفق بود."

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return "❌ موتور تبدیل ویس نصب نیست. این نسخه کاملاً بدون API کار می‌کند؛ `faster-whisper` را نصب کن."

        def load_and_transcribe():
            model = getattr(_self_transcribe_reply, "_model", None)
            model_key = getattr(_self_transcribe_reply, "_model_key", None)
            current_key = (WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
            if model is None or model_key != current_key:
                model = WhisperModel(
                    WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE,
                    cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "0")) or None,
                    num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
                )
                _self_transcribe_reply._model = model
                _self_transcribe_reply._model_key = current_key

            segments, info = model.transcribe(
                str(path), language=WHISPER_LANGUAGE, task="transcribe",
                beam_size=WHISPER_BEAM_SIZE, best_of=WHISPER_BEST_OF, patience=WHISPER_PATIENCE,
                temperature=0, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
                condition_on_previous_text=True, compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0, no_speech_threshold=0.5,
                initial_prompt=(
                    "این یک گفتار فارسی است. متن را دقیقاً به فارسی بنویس. "
                    "کلمات فارسی، محاوره‌ای و نام‌های خاص را حفظ کن و ترجمه نکن."
                ),
            )
            pieces = []
            for seg in segments:
                part = _normalize_persian_transcript(seg.text or "")
                if part:
                    pieces.append(part)
            return _normalize_persian_transcript(" ".join(pieces)), info

        try:
            result, info = await asyncio.wait_for(asyncio.to_thread(load_and_transcribe), timeout=float(os.getenv("STT_LOCAL_TIMEOUT_SECONDS", "1800")))
        except asyncio.TimeoutError:
            return "❌ تبدیل ویس به متن بیش از حد طول کشید؛ دوباره تلاش کن."
        except Exception as exc:
            print(f"[SELF {uid}] local transcription failed: {exc}")
            return "❌ موتور تبدیل ویس خطا داد؛ لاگ سرور را بررسی کن."

        if not result:
            return "❌ صدایی برای تبدیل به متن پیدا نشد."
        detected = getattr(info, "language", None)
        if detected and detected != "fa":
            print(f"[SELF {uid}] Whisper detected language={detected}, forced={WHISPER_LANGUAGE}")
        return f"📝 <b>متن ویس</b>\n\n{html.escape(result)}"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[SELF {uid}] transcription failed: {exc}")
        return "❌ تبدیل ویس به متن انجام نشد؛ دوباره تلاش کن."
    finally:
        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task
        stt_state.pop(uid, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _self_ocr_reply(event, uid):
    """Image -> text with Tesseract OCR."""
    if not event.is_reply:
        return "❌ روی تصویر ریپلای کن و «OCR» را بفرست."
    replied = await event.get_reply_message()
    if not replied or not replied.photo:
        return "❌ تصویر پیدا نشد."

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_ocr_{uid}_"))
    try:
        path = await replied.download_media(file=str(tmp_dir))
        if not path:
            return "❌ دانلود تصویر ناموفق بود."

        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            return "❌ قابلیت OCR نیاز به نصب `pytesseract` و `Pillow` دارد."

        def run_ocr():
            from PIL import ImageOps, ImageFilter

            image = Image.open(path).convert("RGB")

            # Upscale small screenshots/photos before OCR.
            max_side = max(image.size)
            if max_side < 2600:
                scale = min(3.0, 2600.0 / max_side)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )

            gray = ImageOps.grayscale(image)
            gray = ImageOps.autocontrast(gray)
            gray = gray.filter(ImageFilter.SHARPEN)

            # Multiple layouts/thresholds improve Persian OCR on screenshots and photos.
            variants = [
                (gray, 6),
                (gray, 11),
                (gray.point(lambda p: 255 if p > 170 else 0), 6),
                (gray.point(lambda p: 255 if p > 200 else 0), 11),
            ]

            best_text = ""
            best_score = float("-inf")
            for variant, psm in variants:
                try:
                    data = pytesseract.image_to_data(
                        variant,
                        lang="fas+eng",
                        config=f"--oem 1 --psm {psm}",
                        output_type=pytesseract.Output.DICT,
                    )
                    words = []
                    confidences = []
                    for word, conf in zip(data.get("text", []), data.get("conf", [])):
                        word = (word or "").strip()
                        if not word:
                            continue
                        words.append(word)
                        try:
                            value = float(conf)
                            if value >= 0:
                                confidences.append(value)
                        except (TypeError, ValueError):
                            pass
                    candidate = " ".join(words).strip()
                    if not candidate:
                        continue
                    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
                    score = avg_conf + min(len(candidate), 500) * 0.01
                    if score > best_score:
                        best_score = score
                        best_text = candidate
                except Exception as exc:
                    print(f"[SELF {uid}] OCR pass psm={psm} failed: {exc}")

            if best_text:
                return best_text

            try:
                return pytesseract.image_to_string(
                    gray, lang="fas+eng", config="--oem 1 --psm 6"
                ).strip()
            except Exception:
                return pytesseract.image_to_string(
                    gray, lang="eng", config="--oem 1 --psm 6"
                ).strip()

        result = await asyncio.to_thread(run_ocr)
        return f"🔎 **متن تصویر:**\n\n{result}" if result else "❌ متنی در تصویر پیدا نشد."
    except Exception as exc:
        print(f"[SELF {uid}] OCR failed: {exc}")
        return "❌ OCR انجام نشد؛ مطمئن شو Tesseract روی سرور نصب است."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _self_create_chat_or_channel(event, uid, kind, title):
    """Create a Telegram supergroup or channel."""
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title:
        return f"❌ اسم {kind} را وارد کن."

    try:
        from telethon.tl import functions
        result = await event.client(functions.channels.CreateChannelRequest(
            title=title,
            about="ساخته‌شده توسط HusteRIX Self",
            broadcast=(kind == "چنل"),
            megagroup=(kind == "گروه"),
        ))
        created = getattr(result, "chats", None) or []
        if created:
            username = getattr(created[0], "username", None)
            extra = f"\n🔗 @{username}" if username else ""
            return f"✅ {kind} «{title}» ساخته شد.{extra}"
        return f"✅ {kind} «{title}» ساخته شد."
    except Exception as exc:
        print(f"[SELF {uid}] create {kind} failed: {exc}")
        return f"❌ ساخت {kind} ناموفق بود.\n{exc}"


async def _delete_dice_message(client, chat_id, message_id):
    """Delete an unsuccessful dice result reliably in Saved Messages and private chats."""
    message_id = int(message_id)

    # First use Telethon's peer-aware helper. This is important for normal
    # private dialogs where Telegram can apply different revoke semantics.
    try:
        await _tg_call_with_flood_retry(
            lambda: client.delete_messages(chat_id, [message_id], revoke=True),
            label="delete failed dice (peer-aware)",
        )
        return True
    except Exception as first_exc:
        print(f"[DICE] peer-aware delete failed chat={chat_id} message={message_id}: {first_exc}")

    # Raw API fallback for Saved Messages and peers where the convenience
    # wrapper cannot resolve the dialog in time.
    try:
        from telethon.tl.functions.messages import DeleteMessagesRequest
        await _tg_call_with_flood_retry(
            lambda: client(DeleteMessagesRequest(
                id=[message_id],
                revoke=True,
            )),
            label="delete failed dice",
        )
        return True
    except Exception as exc:
        print(f"[DICE] raw delete failed chat={chat_id} message={message_id}: {exc}")
        return False


async def _self_roll_guaranteed_value(event, uid, target):
    """Send a real Telegram dice and reroll until Telegram returns the requested value."""
    try:
        from telethon.tl import types

        target = int(target)
        if target < 1 or target > 6:
            return False

        for _ in range(60):
            msg = await _tg_call_with_flood_retry(
                lambda: event.client.send_file(
                    event.chat_id,
                    types.InputMediaDice("🎲"),
                ),
                label="dice roll",
            )

            value = getattr(getattr(msg, "media", None), "value", None)
            if value == target:
                return True

            # Failed results must never remain visible.  Use the raw Telegram
            # DeleteMessagesRequest as the primary path; it is more reliable
            # than the convenience wrapper for private dialogs/Saved Messages.
            await _delete_dice_message(event.client, event.chat_id, msg.id)
            await asyncio.sleep(0.15)

        return False
    except Exception as exc:
        print(f"[SELF {uid}] forced dice {target} failed: {exc}")
        return False


# Backward-compatible alias for any existing internal references.
async def _self_roll_guaranteed_six(event, uid):
    return await _self_roll_guaranteed_value(event, uid, 6)
























# ============================================================
# MEDIA CONVERSION (SELF)
# ============================================================

MEDIA_CONVERT_MAX_MB = int(os.getenv("MEDIA_CONVERT_MAX_MB", "2048"))
MEDIA_CONVERT_PROGRESS_INTERVAL = float(os.getenv("MEDIA_CONVERT_PROGRESS_INTERVAL", "0.8"))


def _media_conversion_commands():
    # کاربر فقط از دستورات فارسی استفاده می‌کند.
    return {
        "voice_to_mp3": {"ویس به mp3"},
        "mp3_to_voice": {"mp3 به ویس"},
        "video_to_voice": {"ویدیو به ویس"},
        "video_to_mp3": {"ویدیو به mp3"},
    }


def _media_conversion_command(text: str):
    low = (text or "").strip().casefold()
    for operation, aliases in _media_conversion_commands().items():
        if low in {x.casefold() for x in aliases}:
            return operation
    return None


def _message_media_kind(message):
    """Detect Telegram media using Message fields, MIME type and document attributes."""
    if not message:
        return None
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "audio", None):
        return "audio"

    document = getattr(message, "document", None)
    if not document:
        return None

    mime = (getattr(document, "mime_type", None) or "").casefold()
    attrs = getattr(document, "attributes", None) or []
    has_video_attr = False
    has_audio_attr = False
    audio_is_voice = False
    for attr in attrs:
        name = type(attr).__name__.casefold()
        if "video" in name:
            has_video_attr = True
        if "audio" in name:
            has_audio_attr = True
            audio_is_voice = bool(getattr(attr, "voice", False))

    if audio_is_voice:
        return "voice"
    if has_video_attr or mime.startswith("video/"):
        return "video"
    if has_audio_attr or mime.startswith("audio/"):
        return "audio"
    return None


def _message_is_mp3(message):
    """Accept real MP3 audio even when Telegram did not preserve a filename."""
    if _message_media_kind(message) != "audio":
        return False
    document = getattr(message, "document", None)
    mime = (getattr(document, "mime_type", None) or "").casefold()
    if mime in {"audio/mpeg", "audio/mp3", "audio/x-mp3"}:
        return True
    names = []
    file_obj = getattr(message, "file", None)
    if file_obj is not None:
        names.append(getattr(file_obj, "name", None))
    for attr in getattr(document, "attributes", None) or []:
        if type(attr).__name__.casefold().endswith("filename"):
            names.append(getattr(attr, "file_name", None))
    return any(str(name or "").casefold().endswith(".mp3") for name in names)


def _message_media_size(message):
    media = getattr(message, "media", None)
    document = getattr(message, "document", None) or getattr(media, "document", None)
    try:
        return int(getattr(document, "size", 0) or 0)
    except Exception:
        return 0


def _media_progress_text(percent, operation):
    labels = {
        "voice_to_mp3": "🎵 تبدیل ویس به MP3",
        "mp3_to_voice": "🎵 تبدیل MP3 به ویس",
        "video_to_voice": "🎬 ➜ 🎙️ تبدیل ویدیو به ویس",
        "video_to_mp3": "🎬 ➜ 🎵 تبدیل ویدیو به MP3",
    }
    percent = max(0, min(100, int(percent)))
    slots = 16
    filled = round(slots * percent / 100)
    bar = "█" * filled + "░" * (slots - filled)
    return f"{labels.get(operation, '🎵 در حال تبدیل فایل')}\n\n🔄 در حال پردازش...\n\n<code>{bar}</code> {percent}%"


def _media_error_message(exc):
    text = str(exc or "").casefold()
    if "ffmpeg_not_found" in text or "ffprobe_not_found" in text:
        return "❌ موتور تبدیل رسانه روی سرور فعال نیست."
    if "download_failed" in text:
        return "❌ دانلود فایل شکست خورد."
    if "timeout" in text:
        return "❌ زمان پردازش فایل به پایان رسید."
    if "no_audio_track" in text:
        return "❌ فایل صوتی قابل استخراج از این ویدیو پیدا نشد."
    if "invalid_media" in text or "invalid data" in text or "could not find codec" in text:
        return "❌ فایل خراب است یا فرمت آن معتبر نیست."
    if "floodwait" in text:
        return "❌ ارسال فایل به‌دلیل محدودیت تلگرام موقتاً متوقف شد."
    if "file_too_large" in text:
        return f"❌ فایل بیش از حد بزرگ است. حداکثر حجم مجاز تبدیل: {MEDIA_CONVERT_MAX_MB:,} مگابایت."
    if "send_failed" in text:
        return "❌ خطای ارسال فایل رخ داد."
    if "ffmpeg_failed" in text:
        return "❌ تبدیل ناموفق بود."
    return "❌ تبدیل انجام نشد.\nدلیل: خطای پردازش فایل رسانه‌ای."


async def _media_binary_exists(binary):
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False


async def _ffprobe_duration(path):
    if not await _media_binary_exists("ffprobe"):
        raise RuntimeError("ffprobe_not_found")
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore")
        if "Invalid data" in err or "could not find codec" in err:
            raise RuntimeError("invalid_media")
        raise RuntimeError("ffprobe_failed")
    try:
        duration = float(stdout.decode().strip())
        if duration <= 0:
            raise ValueError
        return duration
    except Exception:
        raise RuntimeError("invalid_media")


async def _ffprobe_has_audio(path):
    if not await _media_binary_exists("ffprobe"):
        raise RuntimeError("ffprobe_not_found")
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0 and bool(stdout.decode("utf-8", errors="ignore").strip())


async def _ffmpeg_convert_with_progress(input_path, output_path, operation, duration, progress_cb):
    if not await _media_binary_exists("ffmpeg"):
        raise RuntimeError("ffmpeg_not_found")

    if operation in {"voice_to_mp3", "video_to_mp3"}:
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path), "-vn", "-map", "0:a:0?",
            "-c:a", "libmp3lame", "-b:a", "192k", "-map_metadata", "-1",
            "-progress", "pipe:1", "-nostats", str(output_path),
        ]
    else:
        # Telegram voice notes are Opus in an OGG container. Strip metadata
        # and video completely; -map 0:a:0? makes missing audio a hard error below.
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path), "-vn", "-map", "0:a:0?",
            "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
            "-application", "voip", "-map_metadata", "-1",
            "-progress", "pipe:1", "-nostats", str(output_path),
        ]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    last_ui = 0.0
    last_percent = -1
    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            raw = line.decode("utf-8", errors="ignore").strip()
            if not raw.startswith("out_time_ms="):
                continue
            try:
                out_us = int(raw.split("=", 1)[1])
                percent = int(max(0.0, min(100.0, (out_us / 1_000_000.0) / duration * 100.0)))
            except (ValueError, ZeroDivisionError):
                continue
            now = time.monotonic()
            if percent != last_percent and (now - last_ui) >= MEDIA_CONVERT_PROGRESS_INTERVAL:
                await progress_cb(percent)
                last_ui = now
                last_percent = percent

        await proc.wait()
        stderr = (await stderr_task).decode("utf-8", errors="ignore")
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode != 0:
        low_err = stderr.casefold()
        if "stream map '0:a:0?' matches no streams" in low_err or "matches no streams" in low_err:
            raise RuntimeError("no_audio_track")
        if "invalid data" in low_err or "could not find codec" in low_err:
            raise RuntimeError("invalid_media")
        raise RuntimeError("ffmpeg_failed")
    if not Path(output_path).exists() or Path(output_path).stat().st_size <= 0:
        raise RuntimeError("ffmpeg_failed")
    await progress_cb(100, force=True)


async def _self_media_convert(event, uid, operation):
    if not event.is_reply:
        return "❌ ابتدا روی فایل موردنظر ریپلای کن."

    replied = await event.get_reply_message()
    if not replied:
        return "❌ فایل رسانه‌ای قابل تبدیل پیدا نشد."

    kind = _message_media_kind(replied)
    requirements = {
        "voice_to_mp3": {"voice"},
        "mp3_to_voice": {"audio"},
        "video_to_voice": {"video"},
        "video_to_mp3": {"video"},
    }
    if kind not in requirements.get(operation, set()):
        if operation == "voice_to_mp3":
            return "❌ این فایل برای تبدیل به MP3 مناسب نیست."
        if operation == "mp3_to_voice":
            return "❌ این فایل MP3/Audio برای تبدیل به ویس مناسب نیست."
        return "❌ این فایل برای تبدیل ویدیو به صدا مناسب نیست."

    if operation == "mp3_to_voice" and not _message_is_mp3(replied):
        return "❌ این فایل MP3 نیست و برای این تبدیل مناسب نیست."

    size = _message_media_size(replied)
    if size and size > MEDIA_CONVERT_MAX_MB * 1024 * 1024:
        raise RuntimeError("file_too_large")

    if uid in media_convert_state:
        return "⏳ یک تبدیل رسانه‌ای همین حالا در حال انجام است."

    state = {"status": "processing", "operation": operation, "message_id": int(event.id), "started": time.time()}
    media_convert_state[uid] = state
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_media_{uid}_"))
    input_path = None
    output_path = None

    async def progress_cb(percent, force=False):
        now = time.monotonic()
        if not force and now - state.get("last_ui", 0.0) < MEDIA_CONVERT_PROGRESS_INTERVAL:
            return
        state["last_ui"] = now
        with contextlib.suppress(Exception):
            await event.edit(_media_progress_text(percent, operation), parse_mode="html", buttons=None)

    try:
        if not await _media_binary_exists("ffmpeg") or not await _media_binary_exists("ffprobe"):
            raise RuntimeError("ffmpeg_not_found")

        with contextlib.suppress(Exception):
            await event.edit(_media_progress_text(0, operation), parse_mode="html", buttons=None)

        try:
            input_path = await replied.download_media(file=str(tmp_dir))
        except Exception as exc:
            raise RuntimeError("download_failed") from exc
        if not input_path:
            raise RuntimeError("download_failed")
        input_path = Path(input_path)

        duration = await _ffprobe_duration(input_path)
        if not await _ffprobe_has_audio(input_path):
            raise RuntimeError("no_audio_track")
        suffix = ".mp3" if operation in {"voice_to_mp3", "video_to_mp3"} else ".ogg"
        output_path = tmp_dir / f"converted{suffix}"
        await _ffmpeg_convert_with_progress(input_path, output_path, operation, duration, progress_cb)

        try:
            if operation in {"voice_to_mp3", "video_to_mp3"}:
                # Send as Telegram audio, not as a generic document.  The old
                # force_document=True made Telegram show the .mp3 like an
                # installation/file attachment without the in-app audio player.
                audio_attributes = [
                    types.DocumentAttributeAudio(
                        duration=max(1, int(round(duration))),
                        voice=False,
                    )
                ]
                await event.client.send_file(
                    event.chat_id,
                    str(output_path),
                    caption="🎵 فایل MP3 آماده است.",
                    force_document=False,
                    mime_type="audio/mpeg",
                    attributes=audio_attributes,
                    supports_streaming=True,
                )
            else:
                # Telethon's voice_note=True sends this as a Telegram Voice Message,
                # not as a document merely carrying an .ogg filename.
                await event.client.send_file(
                    event.chat_id, str(output_path),
                    voice_note=True,
                )
        except FloodWaitError as exc:
            raise RuntimeError("floodwait") from exc
        except Exception as exc:
            raise RuntimeError("send_failed") from exc

        return "__SUCCESS__"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[SELF {uid}] media conversion {operation} failed: {exc}")
        return _media_error_message(exc)
    finally:
        media_convert_state.pop(uid, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# ARCHIVE EXTRACTION (ZIP / RAR)
# ============================================================

_UNZIP_COMMANDS = {
    "unzip",
    "unzip + ریپلای",
    "unzip ریپلای",
    "unzip + ریپلی",
    "unzip ریپلی",
    "استخراج",
    "استخراج + ریپلای",
    "استخراج ریپلای",
    "استخراج + ریپلی",
    "استخراج ریپلی",
}


def _safe_archive_target(root: Path, member_name: str) -> Path:
    """Resolve an archive member safely and reject path traversal."""
    raw = str(member_name).replace("\\", "/")
    # Archives are allowed to contain nested directories, but never absolute
    # paths or ../ entries that could escape the temporary extraction folder.
    target = (root / raw).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        raise RuntimeError("archive_path_traversal")
    return target


def _extract_zip_archive(archive_path: Path, output_dir: Path):
    files = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = _safe_archive_target(output_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            files.append(target)
    return files


def _extract_rar_archive(archive_path: Path, output_dir: Path):
    """RAR extraction with rarfile first, then common system extractors."""
    try:
        import rarfile
        files = []
        with rarfile.RarFile(archive_path) as rf:
            for info in rf.infolist():
                if info.isdir():
                    continue
                target = _safe_archive_target(output_dir, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with rf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                files.append(target)
        return files
    except ImportError:
        pass

    # Keep the bot dependency-light: if rarfile is not installed, use an
    # already-installed 7z/7zz/unar binary when available.
    extractor = next((shutil.which(x) for x in ("7z", "7zz", "unar") if shutil.which(x)), None)
    if not extractor:
        raise RuntimeError("rar_backend_missing")

    if Path(extractor).name.lower() in {"7z", "7zz"}:
        cmd = [extractor, "x", "-y", f"-o{output_dir}", str(archive_path)]
    else:
        cmd = [extractor, "-o", str(output_dir), str(archive_path)]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError("rar_extract_failed")

    # Validate the extractor output too, so a malicious archive cannot leave
    # files outside the temporary directory unnoticed.
    root_resolved = output_dir.resolve()
    files = []
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            raise RuntimeError("archive_path_traversal")
        files.append(path)
    return files


def _extract_archive_sync(archive_path: Path, output_dir: Path):
    suffix = archive_path.suffix.casefold()
    if suffix == ".zip":
        return _extract_zip_archive(archive_path, output_dir)
    if suffix == ".rar":
        return _extract_rar_archive(archive_path, output_dir)
    raise RuntimeError("unsupported_archive")


def _archive_progress_text(percent: int, phase: str = "منتظر بمانید", current: int = 0, total: int = 0):
    percent = max(0, min(100, int(percent)))
    slots = 16
    filled = round(slots * percent / 100)
    bar = "█" * filled + "░" * (slots - filled)
    return (
        f"📦 در حال استخراج آرشیو\n"
        f"{bar}\n"
        f"{html.escape(phase)}"
    )


async def _archive_progress_5s(event, started_at: float, phase="در حال استخراج…"):
    """Animate a predictable five-second progress bar without blocking Telethon."""
    while True:
        elapsed = time.monotonic() - started_at
        if elapsed >= 5.0:
            with contextlib.suppress(Exception):
                await event.edit(_archive_progress_text(100, phase))
            return
        percent = int((elapsed / 5.0) * 95)
        with contextlib.suppress(Exception):
            await event.edit(_archive_progress_text(percent, phase))
        await asyncio.sleep(0.6)


async def _self_unzip_reply(event, uid):
    """Extract a replied ZIP/RAR and send every extracted file separately."""
    if not event.is_reply:
        return "❌ روی فایل .zip یا .rar ریپلای کن و سپس «unzip + ریپلی» یا «استخراج + ریپلی» را بفرست."

    replied = await event.get_reply_message()
    if not replied or not getattr(replied, "media", None):
        return "❌ فایل آرشیو پیدا نشد."

    suffix = ""
    name = ""
    document = getattr(replied, "document", None)
    if document:
        for attr in getattr(document, "attributes", None) or []:
            filename = getattr(attr, "file_name", None)
            if filename:
                name = str(filename)
                break
        name = name or "archive"
        suffix = Path(name).suffix.casefold()

    if suffix not in {".zip", ".rar"}:
        return "❌ فقط فایل‌های .zip و .rar قابل استخراج هستند."

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_unzip_{uid}_"))
    archive_path = tmp_dir / (Path(name).name or f"archive{suffix}")
    extract_dir = tmp_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    progress_task = None

    try:
        with contextlib.suppress(Exception):
            await event.edit(_archive_progress_text(0, "منتظر بمانید"))

        downloaded = await replied.download_media(file=str(archive_path))
        if not downloaded:
            return "❌ دانلود آرشیو ناموفق بود."

        # The five-second bar starts after download and covers the extraction
        # phase. Extraction itself runs in a worker thread so Telethon stays
        # responsive and the progress message can keep updating.
        progress_started = time.monotonic()
        progress_task = asyncio.create_task(
            _archive_progress_5s(event, progress_started, "منتظر بمانید")
        )
        try:
            files = await asyncio.to_thread(_extract_archive_sync, archive_path, extract_dir)
        finally:
            if progress_task:
                await progress_task
                progress_task = None

        files = sorted(
            [p for p in files if p.is_file()],
            key=lambda x: str(x).casefold(),
        )
        if not files:
            await event.edit("❌ آرشیو خالی است یا فایل قابل‌ارسالی داخل آن پیدا نشد.")
            return "__DONE__"

        # Send extracted files one-by-one. The progress message is reused for
        # the send phase and shows the actual file counter.
        total = len(files)
        sent = 0
        failed = 0
        await event.edit(_archive_progress_text(0, "منتظر بمانید", 0, total))

        for index, file_path in enumerate(files, 1):
            try:
                rel = file_path.relative_to(extract_dir)
                caption = f"📦 {rel.as_posix()}"
                await event.client.send_file(
                    event.chat_id,
                    str(file_path),
                    force_document=True,
                    caption=caption,
                    reply_to=replied.id,
                )
                sent += 1
            except FloodWaitError as exc:
                wait = max(1, int(getattr(exc, "seconds", 1)))
                await asyncio.sleep(wait)
                try:
                    rel = file_path.relative_to(extract_dir)
                    await event.client.send_file(
                        event.chat_id,
                        str(file_path),
                        force_document=True,
                        caption=f"📦 {rel.as_posix()}",
                        reply_to=replied.id,
                    )
                    sent += 1
                except Exception as exc2:
                    failed += 1
                    print(f"[UNZIP {uid}] resend failed {file_path}: {exc2}")
            except Exception as exc:
                failed += 1
                print(f"[UNZIP {uid}] send failed {file_path}: {exc}")

            percent = int(index * 100 / total)
            with contextlib.suppress(Exception):
                await event.edit(
                    _archive_progress_text(percent, "منتظر بمانید", index, total)
                )

        result = f"✅ استخراج تمام شد.\n📦 ارسال شد: {sent} فایل"
        if failed:
            result += f"\n⚠️ ناموفق: {failed} فایل"
        await event.edit(result)
        return "__DONE__"

    except RuntimeError as exc:
        messages = {
            "archive_path_traversal": "❌ آرشیو نامعتبر است؛ مسیر خطرناک داخل فایل پیدا شد.",
            "rar_backend_missing": "❌ برای استخراج RAR روی سرور، `rarfile` یا یکی از ابزارهای 7z/7zz/unar لازم است.",
            "rar_extract_failed": "❌ استخراج فایل RAR ناموفق بود.",
            "unsupported_archive": "❌ فقط فایل‌های .zip و .rar پشتیبانی می‌شوند.",
        }
        return messages.get(str(exc), "❌ استخراج آرشیو انجام نشد.")
    except zipfile.BadZipFile:
        return "❌ فایل ZIP خراب یا نامعتبر است."
    except Exception as exc:
        print(f"[UNZIP {uid}] extraction failed: {exc}")
        return "❌ استخراج آرشیو انجام نشد؛ فایل ممکن است خراب یا ناقص باشد."
    finally:
        if progress_task:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
        shutil.rmtree(tmp_dir, ignore_errors=True)



# ============================================================
# FREE MEDIA / CRYPTO FEATURES
# ============================================================

_CRYPTO_ALIASES = {
    "btc": ("BTC", "بیت‌کوین"), "بیت کوین": ("BTC", "بیت‌کوین"), "بیتکوین": ("BTC", "بیت‌کوین"),
    "eth": ("ETH", "اتریوم"), "اتریوم": ("ETH", "اتریوم"),
    "sol": ("SOL", "سولانا"), "سول": ("SOL", "سولانا"), "سولانا": ("SOL", "سولانا"),
    "usdt": ("USDT", "تتر"), "تتر": ("USDT", "تتر"),
    "ton": ("TON", "تون‌کوین"), "toncoin": ("TON", "تون‌کوین"),
    "trx": ("TRX", "ترون"), "ترون": ("TRX", "ترون"),
    "xrp": ("XRP", "ریپل"), "ریپل": ("XRP", "ریپل"),
    "doge": ("DOGE", "دوج‌کوین"), "دوج": ("DOGE", "دوج‌کوین"),
    "bnb": ("BNB", "بایننس‌کوین"), "ada": ("ADA", "کاردانو"),
    "dot": ("DOT", "پولکادات"), "avax": ("AVAX", "آوالانچ"), "shib": ("SHIB", "شیبا"),
}

_CRYPTO_GECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "USDT": "tether",
    "TON": "the-open-network", "TRX": "tron", "XRP": "ripple", "DOGE": "dogecoin",
    "BNB": "binancecoin", "ADA": "cardano", "DOT": "polkadot", "AVAX": "avalanche-2",
    "SHIB": "shiba-inu",
}

# Local logo templates. These are real rendering templates, not a fake remote
# template-id mapping. The supported range is exactly the templates below.
LOGO_TEMPLATES = {
    1: ("classic", "کلاسیک"), 2: ("neon", "نئون"), 3: ("minimal", "مینیمال"),
    4: ("badge", "نشان"), 5: ("shadow", "سایه"), 6: ("gradient", "گرادیان"),
    7: ("outline", "خطی"), 8: ("split", "دو رنگ"), 9: ("glow", "درخشش"),
    10: ("terminal", "ترمینال"), 11: ("stamp", "مهر"), 12: ("diamond", "الماس"),
}

def _fa_digits(text):
    return str(text).translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    ))

def _valid_http_url(value: str):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return None
        return value
    except Exception:
        return None

def _http_json_get(url: str, params=None, timeout=CRYPTO_PROVIDER_TIMEOUT):
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = f"{url}{'&' if '?' in url else '?'}{query}" if query else url
    req = urllib.request.Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": "HusteRIX-Diamond-Self/3.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = int(getattr(response, "status", response.getcode()))
        body = response.read()
        if status < 200 or status >= 300:
            raise urllib.error.HTTPError(full_url, status, "HTTP error", response.headers, None)
        if not body:
            raise ValueError("empty_response")
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_json") from exc

def _cmc_price_from_payload(payload, symbol):
    if not isinstance(payload, dict):
        raise ValueError("invalid_json")
    status = payload.get("status") or {}
    if str(status.get("error_code", "0")) != "0":
        raise ValueError(str(status.get("error_message") or "provider_error"))
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("missing_price")
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).upper() != symbol.upper():
            continue
        quotes = item.get("quotes")
        if not isinstance(quotes, list):
            continue
        for quote in quotes:
            if isinstance(quote, dict) and str(quote.get("symbol", "")).upper() == "USD":
                price = quote.get("price")
                if price is not None:
                    return str(price), quote.get("last_updated")
    raise ValueError("missing_price")

async def _get_crypto_price_cmc(symbol: str):
    payload = await asyncio.to_thread(
        _http_json_get,
        f"{CMC_PUBLIC_BASE_URL}/v2/simple/price",
        {"symbol": symbol.upper(), "convert": "USD", "skip_invalid": "true"},
        CRYPTO_PROVIDER_TIMEOUT,
    )
    price, updated = _cmc_price_from_payload(payload, symbol)
    return {"symbol": symbol.upper(), "price": price, "updated": updated, "provider": "CoinMarketCap"}

async def _get_crypto_price_coingecko(symbol: str):
    coin_id = _CRYPTO_GECKO_IDS.get(symbol.upper())
    if not coin_id:
        raise ValueError("missing_price")
    payload = await asyncio.to_thread(
        _http_json_get,
        f"{COINGECKO_PUBLIC_BASE_URL}/simple/price",
        {"ids": coin_id, "vs_currencies": "usd", "include_last_updated_at": "true"},
        CRYPTO_PROVIDER_TIMEOUT,
    )
    item = payload.get(coin_id) if isinstance(payload, dict) else None
    if not isinstance(item, dict) or "usd" not in item:
        raise ValueError("missing_price")
    return {
        "symbol": symbol.upper(),
        "price": str(item["usd"]),
        "updated": item.get("last_updated_at"),
        "provider": "CoinGecko",
    }

async def get_crypto_price(symbol: str):
    """Provider layer: CMC keyless first, CoinGecko public fallback."""
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        raise ValueError("missing_symbol")
    try:
        return await _get_crypto_price_cmc(symbol)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as first_exc:
        try:
            return await _get_crypto_price_coingecko(symbol)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as second_exc:
            raise RuntimeError(
                f"crypto_provider_failed: {type(first_exc).__name__}/{type(second_exc).__name__}"
            ) from second_exc

def _format_price_number(value):
    # Provider value is displayed verbatim. No Rial/Toman conversion or rounding.
    return str(value).strip()

def _format_crypto_updated(value):
    if not value:
        return "همین حالا"
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC")
        return str(value).replace("T", " ").replace("Z", " UTC")
    except Exception:
        return str(value)

def _currency_ui(data, fa_name):
    return (
        "╭━━━━━━━ 💱 <b>قیمت ارز</b> ━━━━━━━╮\n\n"
        f"🪙 <b>{html.escape(fa_name)}</b>  •  <code>{html.escape(data['symbol'])}</code>\n\n"
        f"💵 <b>قیمت:</b> <b>{html.escape(_format_price_number(data['price']))} USD</b>\n\n"
        f"🕒 <b>آخرین بروزرسانی:</b> {html.escape(_format_crypto_updated(data.get('updated')))}\n\n"
        "━━━━━━━━━━━━\n"
        f"⚡ <b>Live Crypto Price</b> • {html.escape(data.get('provider', 'Public API'))}\n"
        "╰━━━━━━━━HusteRIX━━━━━━━━╯"
    )

async def _self_currency_command(event, uid, text):
    m = re.fullmatch(r"قیمت\s+(.+)", text.strip(), flags=re.S | re.I)
    if not m:
        return False
    raw = _fa_digits(m.group(1).strip()).casefold()
    if raw not in _CRYPTO_ALIASES:
        await event.edit(
            "❌ <b>ارز شناخته نشد.</b>\n\n"
            "مثال:\n<code>قیمت BTC</code>\n<code>قیمت SOL</code>\n"
            "<code>قیمت ETH</code>\n<code>قیمت USDT</code>",
            parse_mode="html",
        )
        return True
    symbol, fa_name = _CRYPTO_ALIASES[raw]
    with contextlib.suppress(Exception):
        await event.edit(
            f"💱 <b>در حال دریافت نرخ {html.escape(symbol)}...</b>",
            parse_mode="html",
        )
    try:
        data = await get_crypto_price(symbol)
        await event.edit(_currency_ui(data, fa_name), parse_mode="html")
    except urllib.error.HTTPError as exc:
        await event.edit(f"❌ <b>سرویس نرخ ارز خطا داد؛ HTTP {exc.code}.</b>", parse_mode="html")
    except (urllib.error.URLError, TimeoutError, OSError):
        await event.edit("❌ <b>اتصال به سرویس نرخ ارز برقرار نشد.</b>", parse_mode="html")
    except ValueError:
        await event.edit("❌ <b>پاسخ سرویس نرخ ارز معتبر نبود یا قیمت پیدا نشد.</b>", parse_mode="html")
    except Exception:
        await event.edit("❌ <b>دریافت نرخ ارز ناموفق بود؛ دوباره تلاش کن.</b>", parse_mode="html")
    return True

def _logo_render_sync(template_id: int, text_value: str):
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    template = LOGO_TEMPLATES[template_id][0]
    canvas = Image.new("RGB", (1200, 700), (18, 18, 24))
    draw = ImageDraw.Draw(canvas)
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    font_path = next((x for x in font_candidates if Path(x).exists()), None)
    try:
        font = ImageFont.truetype(font_path, 110) if font_path else ImageFont.load_default()
        small = ImageFont.truetype(font_path, 34) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
        small = font

    bbox = draw.textbbox((0, 0), text_value, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = (1200 - tw) // 2, (700 - th) // 2 - 20

    if template == "classic":
        draw.rounded_rectangle((100, 100, 1100, 600), radius=45, outline=(240, 240, 240), width=6)
        fill = (245, 245, 245)
    elif template == "neon":
        fill = (80, 220, 255)
        for width, alpha in ((40, 40), (24, 70), (12, 110)):
            glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            gd.text((x, y), text_value, font=font, fill=(*fill, alpha))
            glow = glow.filter(ImageFilter.GaussianBlur(width))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")
            draw = ImageDraw.Draw(canvas)
    elif template == "minimal":
        fill = (250, 250, 250)
        draw.line((160, 530, 1040, 530), fill=fill, width=4)
    elif template == "badge":
        draw.ellipse((120, 120, 1080, 580), outline=(240, 190, 70), width=10)
        fill = (240, 190, 70)
    elif template == "shadow":
        fill = (255, 255, 255)
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.text((x + 18, y + 18), text_value, font=font, fill=(0, 0, 0, 180))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")
        draw = ImageDraw.Draw(canvas)
    elif template == "gradient":
        for i in range(canvas.height):
            c = int(40 + (i / canvas.height) * 150)
            draw.line((0, i, canvas.width, i), fill=(c, 70, 180))
        fill = (255, 255, 255)
    elif template == "outline":
        fill = (18, 18, 24)
        stroke = (255, 255, 255)
        draw.text((x, y), text_value, font=font, fill=fill, stroke_width=5, stroke_fill=stroke)
        fill = None
    elif template == "split":
        draw.rectangle((0, 0, 600, 700), fill=(35, 110, 210))
        draw.rectangle((600, 0, 1200, 700), fill=(220, 70, 100))
        fill = (255, 255, 255)
    elif template == "glow":
        fill = (255, 210, 80)
        glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((x, y), text_value, font=font, fill=(255, 210, 80, 210))
        glow = glow.filter(ImageFilter.GaussianBlur(28))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), glow).convert("RGB")
        draw = ImageDraw.Draw(canvas)
    elif template == "terminal":
        fill = (80, 255, 130)
        draw.rectangle((80, 90, 1120, 610), outline=fill, width=4)
        draw.text((120, 120), "$ logo", font=small, fill=fill)
    elif template == "stamp":
        fill = (235, 235, 235)
        draw.rectangle((120, 120, 1080, 580), outline=fill, width=12)
        draw.text((160, 540), "HusteRIX", font=small, fill=fill)
    else:  # diamond
        fill = (240, 210, 80)
        draw.polygon([(600, 80), (1080, 350), (600, 620), (120, 350)], outline=fill, width=10)

    if fill is not None:
        draw.text((x, y), text_value, font=font, fill=fill)
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()

async def generate_logo(template_id: int, text: str):
    if template_id not in LOGO_TEMPLATES:
        raise ValueError("invalid_logo_template")
    if not text.strip():
        raise ValueError("empty_logo_text")
    return await asyncio.to_thread(_logo_render_sync, int(template_id), text.strip())

async def send_generated_image(event, media, caption, filename="generated.jpg"):
    """Shared Telegram media layer for generated bytes."""
    from io import BytesIO
    if isinstance(media, (bytes, bytearray)):
        stream = BytesIO(media)
        stream.name = filename
        await event.client.send_file(event.chat_id, stream, caption=caption, parse_mode="html")
        return True
    if isinstance(media, (str, Path)):
        await event.client.send_file(event.chat_id, str(media), caption=caption, parse_mode="html")
        return True
    if _valid_http_url(media):
        await event.client.send_file(event.chat_id, media, caption=caption, parse_mode="html")
        return True
    raise ValueError("unsupported_media")

async def _self_logo_command(event, uid, text):
    m = re.fullmatch(r"لوگو\s+([0-9۰-۹]{1,3})\s+(.+)", text.strip(), flags=re.S | re.I)
    if not m:
        return False
    logo_id = int(_fa_digits(m.group(1)))
    logo_text = m.group(2).strip()
    if logo_id not in LOGO_TEMPLATES:
        await event.edit(
            f"❌ شماره لوگو باید بین <b>۱ تا {len(LOGO_TEMPLATES)}</b> باشد.",
            parse_mode="html",
        )
        return True
    with contextlib.suppress(Exception):
        await event.edit("🎨 <b>در حال ساخت لوگو…</b>", parse_mode="html")
    try:
        media = await generate_logo(logo_id, logo_text)
        caption = (
            "🎨 <b>Logo Generator</b>\n\n"
            f"✦ <b>متن:</b> {html.escape(logo_text)}\n"
            f"✦ <b>طرح:</b> #{logo_id}"
        )
        await send_generated_image(event, media, caption, f"logo_{logo_id}.png")
        with contextlib.suppress(Exception):
            await event.delete()
    except ImportError:
        await event.edit("❌ برای ساخت لوگو نصب Pillow لازم است.", parse_mode="html")
    except Exception:
        await event.edit("❌ <b>ساخت لوگو ناموفق بود؛ دوباره تلاش کن.</b>", parse_mode="html")
    return True


async def _fake_hack_prank(event, uid):
    """Purely fictional entertainment sequence; no real access or scanning occurs."""
    if not event.is_reply:
        with contextlib.suppress(Exception):
            await event.edit(
                "🎭 <b>حالت هک نمایشی</b>\n\n"
                "روی پیام کاربر ریپلای کن و فقط <code>هک</code> بفرست.",
                parse_mode="html",
            )
        return

    replied = await event.get_reply_message()
    if not replied:
        with contextlib.suppress(Exception):
            await event.edit("❌ این دستور باید روی پیام یک کاربر ریپلای شود.")
        return

    chat_id = event.chat_id
    if chat_id is None:
        return

    # The command itself disappears.  A fresh message is created as a reply to
    # the target and then edited in place, so the target relationship is visible.
    with contextlib.suppress(Exception):
        await event.delete()

    stages = [
        "INITIALIZING SECURE SESSION",
        "ROUTING TRAFFIC THROUGH 7 NODES",
        "BYPASSING FIREWALL LAYER 01/04",
        "BYPASSING FIREWALL LAYER 02/04",
        "ENUMERATING PROTECTED TABLES",
        "DECRYPTING INDEX MANIFEST",
        "MOUNTING ARCHIVE VOLUME",
        "EXTRACTING RECORD SEGMENTS",
        "VERIFYING CHECKSUMS",
        "PACKING ARCHIVE",
        "FINALIZING TRANSFER",
    ]

    progress = None
    try:
        progress = await event.client.send_message(
            chat_id,
            "🛰️ <b>SECURE ACCESS INITIALIZING…</b>",
            parse_mode="html",
            reply_to=int(replied.id),
        )
    except Exception:
        return

    started = time.monotonic()
    steps = 15
    for i in range(steps):
        percent = min(99, int((i + 1) * 99 / steps))
        filled = round(16 * percent / 100)
        bar = "█" * filled + "░" * (16 - filled)
        stage = stages[min(i, len(stages) - 1)]
        elapsed = time.monotonic() - started
        remaining = max(0.0, steps - elapsed)
        text = (
            "🛰️ <b>REMOTE ACCESS PROTOCOL</b>\n\n"
            f"<code>{bar}</code> <b>{percent}%</b>\n\n"
            f"<code>[{stage}]</code>\n"
            f"<code>NODE: {i + 1:02d}/15   ETA: {remaining:04.1f}s</code>"
        )
        with contextlib.suppress(Exception):
            await progress.edit(text, parse_mode="html")
        await asyncio.sleep(1)

    size_gb = round(random.uniform(18.0, 39.0), 2)
    final_text = (
        "🟢 <b>ACCESS PROTOCOL COMPLETE</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✓ PERIMETER BYPASSED\n"
        "✓ SECURITY LAYERS OVERRIDDEN\n"
        "✓ DATABASE INDEX MOUNTED\n"
        "✓ PROTECTED RECORDS ENUMERATED\n"
        "✓ ARCHIVE INTEGRITY VERIFIED\n"
        "✓ ENCRYPTED PACKAGE CREATED\n"
        "✓ TRANSFER CHANNEL CLOSED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>STATUS:</b> <code>ACCESS GRANTED</code>\n"
        f"<b>ARCHIVE:</b> <code>global_records_{size_gb:.2f}GB.enc</code>\n"
        f"<b>SIZE:</b> <code>{size_gb:.2f} GB</code>\n"
        "<b>INTEGRITY:</b> <code>SHA-256 VERIFIED</code>\n"
        "<b>TRANSFER:</b> <code>COMPLETE</code>"
    )
    with contextlib.suppress(Exception):
        await progress.edit(final_text, parse_mode="html")


async def _delete_all_profile_photos(client):
    try:
        photos = await client(functions.photos.GetUserPhotosRequest(
            user_id=await client.get_input_entity("me"),
            offset=0,
            max_id=0,
            limit=20,
        ))
        ids = []
        for photo in getattr(photos, "photos", []) or []:
            if isinstance(photo, types.Photo):
                ids.append(types.InputPhoto(
                    id=photo.id,
                    access_hash=photo.access_hash,
                    file_reference=photo.file_reference,
                ))
        if ids:
            await client(functions.photos.DeletePhotosRequest(id=ids))
    except Exception as exc:
        print(f"[PROFILE] delete photos failed: {exc}")


async def _update_profile_birthday(client, birthday):
    try:
        from telethon.tl import functions as tl_functions
        if not hasattr(tl_functions.account, "UpdateBirthdayRequest"):
            return
        req_cls = tl_functions.account.UpdateBirthdayRequest
        if birthday:
            bday = types.Birthday(
                day=int(getattr(birthday, "day", 1)),
                month=int(getattr(birthday, "month", 1)),
                year=int(getattr(birthday, "year", 0) or 0),
            )
        else:
            # Telegram uses an empty birthday object to clear the date.
            bday = types.Birthday(day=1, month=1, year=0)
        await client(req_cls(birthday=bday))
    except Exception as exc:
        print(f"[PROFILE] birthday update skipped: {exc}")


async def _profile_copy(event, uid):
    if not event.is_reply:
        await event.edit("❌ روی پیام همان کاربر ریپلای کن و «کپی پروفایل» را بفرست.")
        return
    replied = await event.get_reply_message()
    if not replied or not replied.sender_id:
        await event.edit("❌ کاربر هدف پیدا نشد.")
        return
    client = event.client
    try:
        source = await client.get_entity(int(replied.sender_id))
        me = await client.get_me()

        original = {
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
            "about": getattr(me, "about", None) or "",
            "birthday": None,
            "photo_path": None,
        }
        bday = getattr(me, "birthday", None)
        if bday:
            original["birthday"] = {
                "day": int(getattr(bday, "day", 1)),
                "month": int(getattr(bday, "month", 1)),
                "year": int(getattr(bday, "year", 0) or 0),
            }

        profile_dir = BASE_DIR / "profile_copy" / str(uid)
        profile_dir.mkdir(parents=True, exist_ok=True)
        if getattr(me, "photo", None):
            original_photo = await client.download_profile_photo(me, file=str(profile_dir / "original.jpg"))
            if original_photo:
                original["photo_path"] = original_photo

        self_set(uid, "profile_copy_original", json.dumps(original, ensure_ascii=False))

        from telethon.tl.functions.account import UpdateProfileRequest
        await client(UpdateProfileRequest(
            first_name=(getattr(source, "first_name", None) or "")[:64],
            last_name=(getattr(source, "last_name", None) or "")[:64],
            about=(getattr(source, "about", None) or "")[:70],
        ))

        source_birthday = getattr(source, "birthday", None)
        if source_birthday:
            await _update_profile_birthday(client, source_birthday)
        else:
            await _update_profile_birthday(client, None)

        if getattr(source, "photo", None):
            photo_path = await client.download_profile_photo(source, file=str(profile_dir / "source.jpg"))
            if photo_path:
                uploaded = await client.upload_file(photo_path)
                await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
                with contextlib.suppress(Exception):
                    os.remove(photo_path)
        else:
            await _delete_all_profile_photos(client)

        await event.edit("✅ پروفایل کپی شد. آیدی/یوزرنیم دست‌نخورده ماند.")
    except Exception as exc:
        print(f"[PROFILE] copy failed: {exc}")
        await event.edit("❌ کپی پروفایل انجام نشد؛ اطلاعات اصلی دست‌نخورده ماند.")


async def _profile_copy_restore(event, uid):
    raw = self_get(uid, "profile_copy_original", "")
    if not raw:
        await event.edit("❌ پروفایل قبلی برای بازگردانی ذخیره نشده است.")
        return
    try:
        original = json.loads(raw)
        client = event.client
        from telethon.tl.functions.account import UpdateProfileRequest
        await client(UpdateProfileRequest(
            first_name=str(original.get("first_name") or "")[:64],
            last_name=str(original.get("last_name") or "")[:64],
            about=str(original.get("about") or "")[:70],
        ))
        bday = original.get("birthday")
        if bday:
            await _update_profile_birthday(client, types.Birthday(
                day=int(bday.get("day", 1)), month=int(bday.get("month", 1)), year=int(bday.get("year", 0) or 0)
            ))
        else:
            await _update_profile_birthday(client, None)

        await _delete_all_profile_photos(client)
        photo_path = original.get("photo_path")
        if photo_path and Path(photo_path).exists():
            uploaded = await client.upload_file(photo_path)
            await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
        self_set(uid, "profile_copy_original", "")
        await event.edit("✅ پروفایل به حالت قبل برگردانده شد.")
    except Exception as exc:
        print(f"[PROFILE] restore failed: {exc}")
        await event.edit("❌ بازگردانی پروفایل انجام نشد.")


# ============================================================
# EXTRA SELF FEATURES: SPAM / FIRST COMMENT / SECRETARY / GROUP / TAG
# ============================================================

GLOBAL_BAN_KEY = "global_ban_list"
FIRST_COMMENT_CONFIGS_KEY = "first_comment_configs_v2"
SECRETARY_REPLY_KEY = "secretary_reply"
SECRETARY_ENABLED_KEY = "secretary_enabled"
SECRETARY_INTERVAL_KEY = "secretary_interval"

def _json_setting(uid, key, default):
    try:
        value = json.loads(self_get(uid, key, json.dumps(default, ensure_ascii=False)))
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default

def _global_ban_list(uid):
    return {int(x) for x in _json_setting(uid, GLOBAL_BAN_KEY, []) if str(x).lstrip("-").isdigit()}

def _save_global_ban_list(uid, values):
    self_set(uid, GLOBAL_BAN_KEY, json.dumps(sorted({int(x) for x in values})))

def _first_comment_configs(uid):
    raw = _json_setting(uid, FIRST_COMMENT_CONFIGS_KEY, [])
    out=[]
    for x in raw:
        if not isinstance(x, dict): continue
        try: cid=int(x.get("id"))
        except (TypeError,ValueError): continue
        if cid<=0: continue
        x=dict(x); x["id"]=cid; x["text"]=str(x.get("text") or "")[:4096]; x["enabled"]=bool(x.get("enabled",True))
        try: x["discussion_id"]=int(x["discussion_id"]) if x.get("discussion_id") is not None else None
        except (TypeError,ValueError): x["discussion_id"]=None
        out.append(x)
    return out

def _save_first_comment_configs(uid, values):
    seen=set(); out=[]
    for x in values:
        if not isinstance(x,dict): continue
        try: cid=int(x.get("id"))
        except (TypeError,ValueError): continue
        if cid<=0 or cid in seen: continue
        seen.add(cid); out.append(x)
    self_set(uid, FIRST_COMMENT_CONFIGS_KEY, json.dumps(out, ensure_ascii=False))

def _first_comment_config(uid, channel_id):
    try: cid=int(channel_id)
    except (TypeError,ValueError): return None
    return next((x for x in _first_comment_configs(uid) if int(x["id"])==cid), None)

def _upsert_first_comment_config(uid, item):
    cid=int(item["id"]); _save_first_comment_configs(uid,[x for x in _first_comment_configs(uid) if int(x["id"])!=cid]+[item])

def _remove_first_comment_config(uid, channel_id):
    try: cid=int(channel_id)
    except (TypeError,ValueError): return False
    old=_first_comment_configs(uid); new=[x for x in old if int(x["id"])!=cid]
    if len(old)==len(new): return False
    _save_first_comment_configs(uid,new); _first_comment_ui_target.pop(int(uid),None); return True

def _set_comment_target(uid, channel_id): _first_comment_ui_target[int(uid)]=int(channel_id)
def _comment_target(uid): return _first_comment_ui_target.get(int(uid))
def _clear_comment_target(uid): _first_comment_ui_target.pop(int(uid),None)


async def _is_group_admin(client, chat_id, uid):
    try:
        if not chat_id:
            return False
        perms = await client.get_permissions(chat_id, uid)
        return bool(getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False))
    except Exception:
        return False


async def _resolve_user(client, raw, reply_message=None):
    raw = (raw or "").strip()
    if reply_message and reply_message.sender_id:
        return await client.get_entity(int(reply_message.sender_id))
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.lstrip("-").isdigit():
        return await client.get_entity(int(raw))
    if raw:
        return await client.get_entity(raw)
    return None


async def _spam_replied(event, uid, count):
    if not event.is_reply:
        return "❌ این دستور باید روی پیام موردنظر ریپلای شود."
    if count < 1 or count > 1000:
        return "❌ تعداد تکرار باید بین ۱ تا ۱۰۰۰ باشد."
    replied = await event.get_reply_message()
    if not replied:
        return "❌ پیام ریپلای‌شده پیدا نشد."
    if uid in _spam_tasks and not _spam_tasks[uid].done():
        return "⏳ یک اسپم در حال اجراست."

    chat_id = event.chat_id
    async def worker():
        try:
            # Re-send content using the SELF account. Never use the bot client.
            text = replied.raw_text or ""
            if getattr(replied, "media", None):
                path = None
                try:
                    path = await replied.download_media(file=str(BASE_DIR / "tmp_spam" / str(uid)))
                    if path:
                        for _ in range(count):
                            await _tg_call_with_flood_retry(
                                lambda p=path: event.client.send_file(chat_id, p, caption=text[:4096]),
                                label="spam send",
                                max_retries=5,
                            )
                    else:
                        raise RuntimeError("media_download_failed")
                finally:
                    if path:
                        with contextlib.suppress(Exception):
                            Path(path).unlink(missing_ok=True)
            else:
                for _ in range(count):
                    await _tg_call_with_flood_retry(
                        lambda: event.client.send_message(chat_id, text),
                        label="spam send",
                        max_retries=5,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SPAM {uid}] failed: {exc}")

    task = asyncio.create_task(worker())
    _spam_tasks[uid] = task
    with contextlib.suppress(Exception):
        await event.delete()
    return f"✅ اسپم {count:,} تکرار با اکانت SELF شروع شد."


async def _maybe_first_comment(event, uid):
    configs={int(x["id"]):x for x in _first_comment_configs(uid) if x.get("enabled") and str(x.get("text") or "").strip()}
    if not configs: return
    client=event.client; msg=event.message
    chat=None
    with contextlib.suppress(Exception): chat=await event.get_chat()
    chat_id=getattr(chat,"id",None)
    is_broadcast=isinstance(chat,types.Channel) and bool(getattr(chat,"broadcast",False)) and not bool(getattr(chat,"megagroup",False))
    is_discussion=isinstance(chat,types.Channel) and bool(getattr(chat,"megagroup",False))
    fwd=getattr(msg,"fwd_from",None); forward=getattr(msg,"forward",None)
    source=None
    for obj in (fwd,forward):
        for peer in (getattr(obj,"from_id",None),getattr(obj,"saved_from_peer",None)):
            val=getattr(peer,"channel_id",None)
            if val is not None:
                source=int(val); break
        if source is None:
            val=getattr(obj,"channel_id",None)
            if val is not None: source=int(val)
        if source is not None: break
    peer_channel=getattr(getattr(msg,"peer_id",None),"channel_id",None)
    if source is None and is_broadcast and peer_channel is not None and int(peer_channel) in configs: source=int(peer_channel)
    if source is None and is_broadcast and chat_id is not None and int(chat_id) in configs: source=int(chat_id)
    if source is None or source not in configs: return
    cfg=configs[source]; original=None
    for obj in (fwd,forward):
        val=getattr(obj,"channel_post",None)
        if val is not None:
            original=int(val); break
    if original is None and is_broadcast: original=int(msg.id)
    did=cfg.get("discussion_id")
    try: did=int(did) if did is not None else None
    except (TypeError,ValueError): did=None
    try:
        channel=await client.get_entity(source)
        if not did:
            full=await client(functions.channels.GetFullChannelRequest(channel=channel)); did=getattr(getattr(full,"full_chat",None),"linked_chat_id",None)
            if did:
                cfg["discussion_id"]=int(did); _upsert_first_comment_config(uid,cfg)
    except Exception as exc:
        print(f"[COMMENT {uid}] resolve source failed: {type(exc).__name__}: {exc}"); return
    if not did: return
    try: discussion=await client.get_entity(int(did))
    except Exception as exc: print(f"[COMMENT {uid}] discussion failed: {exc}"); return
    reply_to=None
    if is_discussion and chat_id is not None and int(chat_id)==int(getattr(discussion,"id",0)) and (fwd is not None or forward is not None): reply_to=int(msg.id)
    if reply_to is None and original is not None:
        for delay in (0,0.8,1.5,2.5,4.0):
            if delay: await asyncio.sleep(delay)
            try:
                result=await client(functions.messages.GetDiscussionMessageRequest(peer=channel,msg_id=int(original)))
                messages=getattr(result,"messages",None) or []
                candidates=[m for m in messages if getattr(getattr(m,"peer_id",None),"channel_id",None)==int(getattr(discussion,"id",0))]
                if not candidates: candidates=[m for m in messages if int(getattr(m,"id",0) or 0)!=int(original)]
                if candidates: reply_to=int(candidates[0].id); break
            except Exception as exc: print(f"[COMMENT {uid}] lookup failed: {type(exc).__name__}: {exc}")
    if reply_to is None and is_discussion and chat_id is not None and int(chat_id)==int(getattr(discussion,"id",0)): reply_to=int(msg.id)
    if reply_to is None: return
    text=str(cfg.get("text") or "").strip()[:4096]; key=(int(source),int(reply_to),text)
    if key in _first_comment_sent_cache: return
    async def send():
        return await client(SendMessageRequest(peer=discussion,message=text,random_id=random.getrandbits(64),reply_to=types.InputReplyToMessage(reply_to_msg_id=int(reply_to))))
    try:
        sent=await _tg_call_with_flood_retry(send,label="first comment",max_retries=5); _first_comment_sent_cache.add(key)
        if len(_first_comment_sent_cache)>5000: _first_comment_sent_cache.clear()
        print(f"[COMMENT {uid}] sent channel={source} reply_to={reply_to} message={getattr(sent,'id',None)}")
    except Exception as exc: print(f"[COMMENT {uid}] send failed: {type(exc).__name__}: {exc}")


async def _handle_group_command(event, uid, text):
    low = text.casefold().strip()
    client = event.client

    # Global ban enforcement runs for every incoming message elsewhere too.
    if low in {"پین", "پین + ریپلای"}:
        if not event.is_group or not event.is_reply:
            await event.edit("❌ داخل گروه روی پیام موردنظر ریپلای کن.")
            return True
        if not await _is_group_admin(client, event.chat_id, uid):
            await event.edit("❌ فقط ادمین گروه می‌تواند پین کند.")
            return True
        replied = await event.get_reply_message()
        try:
            await client.pin_message(event.chat_id, replied.id, notify=False)
            await event.edit("📌 پیام با موفقیت سنجاق شد.")
        except Exception as exc:
            await event.edit(f"❌ پین انجام نشد: {html.escape(str(exc))}")
        return True

    if low in {"حذف پین", "حذف پین + ریپلای"}:
        if not event.is_group or not event.is_reply:
            await event.edit("❌ داخل گروه روی پیام موردنظر ریپلای کن.")
            return True
        if not await _is_group_admin(client, event.chat_id, uid):
            await event.edit("❌ فقط ادمین گروه می‌تواند پین را حذف کند.")
            return True
        replied = await event.get_reply_message()
        try:
            await client.unpin_message(event.chat_id, message=replied.id)
            await event.edit("📌 سنجاق پیام حذف شد.")
        except Exception as exc:
            await event.edit(f"❌ حذف پین انجام نشد: {html.escape(str(exc))}")
        return True

    if low in {"بن", "سیک", "بن + ریپلای", "سیک + ریپلای"}:
        if not event.is_group or not event.is_reply:
            await event.edit("❌ این دستور را داخل گروه و با ریپلای روی کاربر استفاده کن.")
            return True
        if not await _is_group_admin(client, event.chat_id, uid):
            await event.edit("❌ فقط ادمین گروه می‌تواند بن کند.")
            return True
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        if not target or target == uid:
            await event.edit("❌ کاربر هدف معتبر نیست.")
            return True
        try:
            await client.edit_permissions(event.chat_id, target, view_messages=False, send_messages=False)
            await event.edit(f"🚫 کاربر `{target}` از گروه بن شد.")
        except Exception as exc:
            await event.edit(f"❌ بن انجام نشد: {html.escape(str(exc))}")
        return True

    if low in {"آن بن", "ان بن", "آن‌بن", "ان‌بن", "آن بن + ریپلای"}:
        if not event.is_group or not event.is_reply:
            await event.edit("❌ داخل گروه روی کاربر ریپلای کن.")
            return True
        if not await _is_group_admin(client, event.chat_id, uid):
            await event.edit("❌ فقط ادمین گروه می‌تواند آن‌بن کند.")
            return True
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        try:
            await client.edit_permissions(event.chat_id, target, view_messages=True, send_messages=True)
            await event.edit(f"✅ کاربر `{target}` آن‌بن شد.")
        except Exception as exc:
            await event.edit(f"❌ آن‌بن انجام نشد: {html.escape(str(exc))}")
        return True

    m = re.fullmatch(r"بن سراسری(?:\s+(.+))?", text, flags=re.S | re.I)
    if m:
        raw = (m.group(1) or "").strip()
        if not raw and not event.is_reply:
            await event.edit("❌ آیدی یا یوزرنیم را بده یا روی پیام کاربر ریپلای کن.")
            return True
        try:
            replied = await event.get_reply_message() if event.is_reply else None
            target = await _resolve_user(client, raw, replied)
            target_id = int(target.id)
            bans = _global_ban_list(uid)
            bans.add(target_id)
            _save_global_ban_list(uid, bans)
            if event.is_group and await _is_group_admin(client, event.chat_id, uid) and target_id != uid:
                with contextlib.suppress(Exception):
                    await client.edit_permissions(event.chat_id, target_id, view_messages=False, send_messages=False)
            await event.edit(f"🚫 کاربر `{target_id}` به لیست بن سراسری اضافه شد.")
        except Exception as exc:
            await event.edit(f"❌ ثبت بن سراسری ناموفق بود: {html.escape(str(exc))}")
        return True

    m = re.fullmatch(r"حذف بن سراسری(?:\s+(.+))?", text, flags=re.S | re.I)
    if m:
        raw = (m.group(1) or "").strip()
        try:
            replied = await event.get_reply_message() if event.is_reply else None
            target = await _resolve_user(client, raw, replied)
            target_id = int(target.id)
            bans = _global_ban_list(uid)
            if target_id not in bans:
                await event.edit("❌ این کاربر در لیست بن سراسری نیست.")
                return True
            bans.discard(target_id)
            _save_global_ban_list(uid, bans)
            await event.edit(f"✅ کاربر `{target_id}` از لیست بن سراسری حذف شد.")
        except Exception as exc:
            await event.edit(f"❌ حذف بن سراسری ناموفق بود: {html.escape(str(exc))}")
        return True

    if low == "لیست بن سراسری":
        bans = sorted(_global_ban_list(uid))
        if not bans:
            await event.edit("🚫 لیست بن سراسری خالی است.")
        else:
            await event.edit("🚫 <b>لیست بن سراسری</b>\n\n" + "\n".join(f"{i}. <code>{x}</code>" for i, x in enumerate(bans, 1)), parse_mode="html")
        return True

    tag_match = re.fullmatch(r"تگ(?:\s+([0-9۰-۹]+))?", _fa_digits(text))
    if tag_match or low == "همه":
        if not event.is_group:
            await event.edit("❌ تگ اعضا فقط داخل گروه قابل استفاده است.")
            return True
        count = None if low == "همه" else int(tag_match.group(1) or 0)
        if count is not None and not 1 <= count <= 1000:
            await event.edit("❌ تعداد تگ باید بین ۱ تا ۱۰۰۰ باشد.")
            return True
        replied = await event.get_reply_message() if event.is_reply else None
        members = []
        async for member in client.iter_participants(event.chat_id):
            if getattr(member, "bot", False) or int(member.id) == int(uid):
                continue
            members.append(member)
            if count is not None and len(members) >= count:
                break
        with contextlib.suppress(Exception):
            await event.delete()
        if not members:
            await client.send_message(event.chat_id, "❌ عضوی برای تگ پیدا نشد.")
            return True
        for start in range(0, len(members), 15):
            chunk = members[start:start + 15]
            lines = []
            for member in chunk:
                name = html.escape((getattr(member, "first_name", None) or getattr(member, "username", None) or "کاربر").strip())
                lines.append(f'<a href="tg://user?id={int(member.id)}">{name}</a>')
            kwargs = {"parse_mode": "html"}
            if replied:
                kwargs["reply_to"] = replied.id
            await client.send_message(event.chat_id, "\n".join(lines), **kwargs)
        return True

    return False


async def _handle_first_comment_command(event, uid, text):
    low=text.casefold().strip()
    if low in {"تنظیم کامنت","تنظیم کامنت + ریپلای","تنظیم کامنت ریپلای"}:
        if not event.is_reply: await event.edit("❌ روی پیام متنی موردنظر ریپلای کن."); return True
        cid=_comment_target(uid); cfg=_first_comment_config(uid,cid) if cid else None
        if not cfg: await event.edit("❌ ابتدا از پنل «💬 کامنت اول» یک کانال را انتخاب کن."); return True
        replied=await event.get_reply_message()
        if not replied or not (replied.raw_text or "").strip(): await event.edit("❌ پیام ریپلای‌شده باید متنی باشد."); return True
        cfg["text"]=replied.raw_text.strip()[:4096]; cfg["enabled"]=True; _upsert_first_comment_config(uid,cfg)
        await event.edit(f"✅ <b>متن کامنت ذخیره شد.</b>\n\n📢 {html.escape(str(cfg.get('title') or 'کانال'))}\n🟢 فعال شد.",parse_mode="html"); return True
    if low.startswith("حذف کامنت اول"):
        raw=text[len("حذف کامنت اول"):].strip()
        if not raw: await event.edit("❌ آیدی یا یوزرنیم کانال را وارد کن."); return True
        try:
            ent=await event.client.get_entity(raw); ok=_remove_first_comment_config(uid,int(ent.id)); await event.edit("✅ تنظیمات کامنت کانال حذف شد." if ok else "❌ این کانال تنظیم نشده بود.")
        except Exception as exc: await event.edit(f"❌ حذف انجام نشد: {html.escape(str(exc))}")
        return True
    if low=="لیست کامنت":
        cfgs=_first_comment_configs(uid)
        if not cfgs: await event.edit("💬 لیست کامنت اول خالی است."); return True
        lines=["💬 <b>لیست کامنت اول</b>",""]
        for i,cfg in enumerate(cfgs,1):
            st="🟢 فعال" if cfg.get("enabled") and cfg.get("text") else ("🟡 بدون متن" if cfg.get("enabled") else "🔴 خاموش")
            lines.append(f"{i}. 📢 <b>{html.escape(str(cfg.get('title') or cfg.get('id')))}</b> • {st}")
        await event.edit("\n".join(lines),parse_mode="html"); return True
    if low=="پاکسازی لیست کامنت":
        _save_first_comment_configs(uid,[]); _clear_comment_target(uid); await event.edit("✅ تمام تنظیمات کامنت اول پاک شد."); return True
    if low.startswith("تنظیم کامنت اول"):
        raw=text[len("تنظیم کامنت اول"):].strip()
        if not raw: await event.edit("❌ آیدی یا یوزرنیم کانال را وارد کن."); return True
        try:
            ent=await event.client.get_entity(raw)
            full=await event.client(functions.channels.GetFullChannelRequest(channel=ent)); did=getattr(getattr(full,"full_chat",None),"linked_chat_id",None)
            if not did: await event.edit("❌ این کانال Discussion متصل ندارد."); return True
            d=await event.client.get_entity(int(did)); old=_first_comment_config(uid,int(ent.id)) or {}
            item={"id":int(ent.id),"access_hash":getattr(ent,"access_hash",None),"title":getattr(ent,"title","کانال"),"username":getattr(ent,"username",None),"discussion_id":int(did),"discussion_access_hash":getattr(d,"access_hash",None),"text":str(old.get("text") or ""),"enabled":True}
            _upsert_first_comment_config(uid,item); _set_comment_target(uid,int(ent.id)); await event.edit("✅ کانال ثبت شد. حالا روی پیام متن ریپلای کن و «تنظیم کامنت» بفرست.")
        except Exception as exc: await event.edit(f"❌ ثبت کانال ناموفق بود: {html.escape(str(exc))}")
        return True
    return False


async def _handle_secretary_command(event, uid, text):
    low = text.casefold().strip()
    if low == "منشی روشن":
        if not _secretary_reply(uid):
            await event.edit("❌ ابتدا با «تنظیم منشی» پاسخ منشی را تنظیم کن.")
            return True
        self_set(uid, SECRETARY_ENABLED_KEY, "on")
        await event.edit("🤵 منشی روشن شد. فقط در پیوی فعال است.")
        return True
    if low == "منشی خاموش":
        self_set(uid, SECRETARY_ENABLED_KEY, "off")
        await event.edit("🤵 منشی خاموش شد.")
        return True
    m = re.fullmatch(r"تنظیم زمان منشی\s+([0-9۰-۹]+)", _fa_digits(text))
    if m:
        minutes = int(m.group(1))
        if not 5 <= minutes <= 60:
            await event.edit("❌ زمان منشی باید بین ۵ تا ۶۰ دقیقه باشد.")
            return True
        self_set(uid, SECRETARY_INTERVAL_KEY, str(minutes))
        await event.edit(f"✅ فاصله پاسخ منشی روی {minutes} دقیقه تنظیم شد.")
        return True
    if low in {"تنظیم منشی", "تنظیم منشی + ریپلای", "تنظیم منشی ریپلای"}:
        if not event.is_reply:
            await event.edit("❌ روی پیام متنی یا مدیای موردنظر ریپلای کن.")
            return True
        replied = await event.get_reply_message()
        if not replied:
            await event.edit("❌ پیام پیدا نشد.")
            return True
        data = {"kind": "text", "text": replied.raw_text or "", "path": None, "caption": replied.raw_text or ""}
        if getattr(replied, "media", None):
            try:
                media_dir = BASE_DIR / "secretary_media" / str(uid)
                media_dir.mkdir(parents=True, exist_ok=True)
                path = await replied.download_media(file=str(media_dir / "reply"))
                if path:
                    data["kind"] = "media"
                    data["path"] = str(path)
            except Exception as exc:
                await event.edit(f"❌ ذخیره مدیا ناموفق بود: {html.escape(str(exc))}")
                return True
        _save_secretary_reply(uid, data)
        await event.edit("✅ پاسخ منشی ذخیره شد. برای فعال‌سازی: «منشی روشن»")
        return True
    return False

async def self_handle_outgoing(event, uid):
    text = (event.raw_text or "").strip()
    low = text.casefold()
    if not text:
        return

    # Feature commands must run before the general outgoing-message logic.
    if await _self_currency_command(event, uid, text):
        return
    if await _self_logo_command(event, uid, text):
        return

    if await _handle_first_comment_command(event, uid, text):
        return
    if await _handle_secretary_command(event, uid, text):
        return
    if await _handle_group_command(event, uid, text):
        return

    m = re.fullmatch(r"تکرار\s+([0-9۰-۹]+)", _fa_digits(text))
    if m:
        count = int(m.group(1))
        await event.edit(await _spam_replied(event, uid, count))
        return

    if low == "کپی پروفایل":
        await _profile_copy(event, uid)
        return

    if low == "حذف کپی پروفایل":
        await _profile_copy_restore(event, uid)
        return

    if low in _UNZIP_COMMANDS:
        result = await _self_unzip_reply(event, uid)
        if result != "__DONE__":
            with contextlib.suppress(Exception):
                await event.edit(result)
        return

    if low == "دانلود":
        await event.edit(await _self_save_replied_message(event, uid))
        return

    media_operation = _media_conversion_command(text)
    if media_operation:
        if uid in media_convert_state:
            with contextlib.suppress(Exception):
                await event.edit("⏳ یک تبدیل رسانه‌ای همین حالا در حال انجام است.")
            return
        result = await _self_media_convert(event, uid, media_operation)
        if result == "__SUCCESS__":
            with contextlib.suppress(Exception):
                await event.edit("✅ تبدیل با موفقیت انجام شد.")
        else:
            with contextlib.suppress(Exception):
                await event.edit(result)
        return

    if event.is_reply and re.fullmatch(r"متن(?:\s*(?:\+\s*)?ریپ(?:ل|لی)|\s*\+\s*ریپ(?:ل|لی))?", low):
        # Reply to the original voice/audio and transcribe it locally.
        replied = await event.get_reply_message()
        if not replied or _message_media_kind(replied) not in {"voice", "audio"}:
            with contextlib.suppress(Exception):
                await event.edit("❌ روی ویس یا فایل صوتی ریپلای کن و «متن + ریپلی» را بفرست.")
            return

        with contextlib.suppress(Exception):
            await event.edit("🎙️ <b>در حال تبدیل ویس به متن…</b>\n<i>لطفاً چند لحظه صبر کن.</i>", parse_mode="html")
        try:
            result = await asyncio.wait_for(
                _self_transcribe_reply(event, uid),
                timeout=float(os.getenv("STT_TIMEOUT_SECONDS", "1200")),
            )
        except asyncio.TimeoutError:
            stt_state.pop(uid, None)
            result = "❌ تبدیل ویس به متن بیش از حد طول کشید؛ دوباره تلاش کن."
        except Exception as exc:
            stt_state.pop(uid, None)
            print(f"[SELF {uid}] STT command failed: {exc}")
            result = "❌ تبدیل ویس به متن با خطا متوقف شد؛ دوباره تلاش کن."
        with contextlib.suppress(Exception):
            await event.edit(result, parse_mode="html")
        return

    if low in {"ocr", "او سی آر"} and event.is_reply:
        await event.edit(await _self_ocr_reply(event, uid), parse_mode="md")
        return

    group_match = re.fullmatch(r"ساخت\s+گروه\s+(.+)", text, flags=re.S)
    if group_match:
        await event.edit(await _self_create_chat_or_channel(event, uid, "گروه", group_match.group(1)))
        return

    channel_match = re.fullmatch(r"ساخت\s+چنل\s+(.+)", text, flags=re.S)
    if channel_match:
        await event.edit(await _self_create_chat_or_channel(event, uid, "چنل", channel_match.group(1)))
        return

    dice_match = re.fullmatch(r"تاس\s+([1-6۱-۶])", text)
    if dice_match:
        chat_id = event.chat_id
        target_raw = dice_match.group(1)
        target = int(target_raw.translate(str.maketrans("۱۲۳۴۵۶", "123456")))

        with contextlib.suppress(Exception):
            await event.delete()

        ok = await _self_roll_guaranteed_value(event, uid, target)
        if not ok:
            await event.client.send_message(
                chat_id,
                f"❌ تلگرام اجازه تولید تاس {target} را نداد."
            )
        return

    if low == "هک":
        await _fake_hack_prank(event, uid)
        return

    if low == "پنل":
        try:
            # The self account invokes the bot's inline mode and inserts the
            # result into this chat. The bot does NOT need to be a member here.
            await send_self_inline_result(event, "پنل")
            with contextlib.suppress(Exception):
                await event.delete()
        except Exception as exc:
            print(f"[SELF {uid}] inline panel failed: {exc}")
            with contextlib.suppress(Exception):
                await event.edit("❌ پنل شیشه‌ای ارسال نشد. حالت Inline ربات را در BotFather فعال کنید.")
        return

    if low == "راهنما":
        try:
            await send_self_inline_result(event, "راهنما")
            with contextlib.suppress(Exception):
                await event.delete()
        except Exception as exc:
            print(f"[SELF {uid}] inline guide failed: {exc}")
            with contextlib.suppress(Exception):
                await event.edit("❌ راهنما ارسال نشد. حالت Inline ربات را در BotFather فعال کنید.")
        return

    if low in {"استیکر", "تبدیل عکس به استیکر", "عکس به استیکر"}:
        result = await _self_image_to_sticker(event, uid, keep_reply=False)
        with contextlib.suppress(Exception):
            await event.delete()
        if result.startswith("❌"):
            await event.client.send_message(event.chat_id, result)
        return

    if low in {"استیکر + ریپلای", "استیکر ریپلای"}:
        result = await _self_image_to_sticker(event, uid, keep_reply=True)
        with contextlib.suppress(Exception):
            await event.delete()
        if result.startswith("❌"):
            await event.client.send_message(event.chat_id, result)
        return

    if low in {"عکس", "تبدیل استیکر به عکس", "استیکر به عکس"}:
        result = await _self_sticker_to_photo(event, uid, keep_reply=False)
        with contextlib.suppress(Exception):
            await event.delete()
        if result.startswith("❌"):
            await event.client.send_message(event.chat_id, result)
        return

    if low in {"عکس + ریپلای", "عکس ریپلای", "استیکر به عکس + ریپلای"}:
        result = await _self_sticker_to_photo(event, uid, keep_reply=True)
        with contextlib.suppress(Exception):
            await event.delete()
        if result.startswith("❌"):
            await event.client.send_message(event.chat_id, result)
        return

    if low in {"تنظیم بنر فور", "تنظیم بنر کپی"}:
        if not event.is_reply:
            await event.edit("❌ روی پیام بنر ریپلای کن.")
            return
        replied = await event.get_reply_message()
        if not replied:
            await event.edit("❌ پیام بنر پیدا نشد.")
            return
        banners = self_banners(uid)
        banner_id = _next_banner_id(banners)
        mode = "forward" if low.endswith("فور") else "copy"
        banner = {
            "id": banner_id,
            "mode": mode,
            "source_chat_id": int(event.chat_id),
            "source_msg_id": int(replied.id),
            "text": replied.raw_text or "",
            "media_path": None,
            "interval": 60,
            "targets": [],
            "enabled": True,
            "last_sent": 0,
        }
        if mode == "copy" and getattr(replied, "media", None):
            media_dir = _banner_media_dir(uid)
            media_path = await replied.download_media(file=str(media_dir / f"{banner_id}"))
            if media_path:
                banner["media_path"] = str(media_path)
        banners.append(banner)
        self_save_banners(uid, banners)
        await event.edit(f"✅ بنر #{banner_id} با حالت «{'فوروارد' if mode == 'forward' else 'کپی'}» ثبت شد.")
        return

    m = re.fullmatch(r"حذف بنر\s+(\d+)", _fa_digits(text))
    if m:
        bid = int(m.group(1))
        banners = self_banners(uid)
        new = [b for b in banners if int(b.get("id", 0)) != bid]
        if len(new) == len(banners):
            await event.edit("❌ بنر موردنظر پیدا نشد.")
            return
        old_b = _banner_by_id(uid, bid)
        if old_b and old_b.get("media_path"):
            with contextlib.suppress(Exception):
                Path(old_b["media_path"]).unlink(missing_ok=True)
        self_save_banners(uid, new)
        await event.edit(f"✅ بنر #{bid} حذف شد.")
        return

    if low == "پاکسازی لیست بنر ها":
        for b in self_banners(uid):
            if b.get("media_path"):
                with contextlib.suppress(Exception):
                    Path(b["media_path"]).unlink(missing_ok=True)
        self_save_banners(uid, [])
        await event.edit("✅ لیست تمام بنرها پاک شد.")
        return

    m = re.fullmatch(r"تنظیم عدد بنر\s+(\d+)\s+(\d+)\s+دقیقه", _fa_digits(text))
    if m:
        bid, minutes = int(m.group(1)), int(m.group(2))
        banners = self_banners(uid)
        banner = _banner_from_list(banners, bid)
        if not banner or minutes < 1:
            await event.edit("❌ بنر یا زمان نامعتبر است.")
            return
        banner["interval"] = minutes
        self_save_banners(uid, banners)
        sent = failed = 0
        if self_get(uid, "banner_auto", "off") == "on" and banner.get("targets"):
            client = _get_tabchi_client(uid)
            if client:
                sent, failed = await _banner_dispatch_configured_now(client, uid, banner)
                self_save_banners(uid, banners)
        extra = f"\n📨 ارسال فوری: {sent} مقصد" if sent else ""
        if failed:
            extra += f"\n⚠️ ناموفق: {failed} مقصد"
        await event.edit(f"✅ فاصله ارسال بنر #{bid} روی {minutes} دقیقه تنظیم شد.{extra}")
        return

    m = re.fullmatch(r"تنظیم گپ هدف بنر\s+(\d+)", _fa_digits(text))
    if m:
        bid = int(m.group(1))
        banners = self_banners(uid)
        banner = _banner_from_list(banners, bid)
        if not banner or event.chat_id is None or not event.is_group:
            await event.edit("❌ این دستور را داخل گروه هدف اجرا کن.")
            return
        chat_id = int(event.chat_id)
        targets = {int(x) for x in banner.get("targets", []) if int(x) != 0}
        targets.add(chat_id)
        banner["targets"] = sorted(targets)
        self_save_banners(uid, banners)
        sent = failed = 0
        if self_get(uid, "banner_auto", "off") == "on":
            client = _get_tabchi_client(uid)
            if client:
                sent, failed = await _banner_dispatch_configured_now(client, uid, banner)
                self_save_banners(uid, banners)
        extra = f"\n📨 ارسال فوری: {sent} مقصد" if sent else ""
        if failed:
            extra += f"\n⚠️ ناموفق: {failed} مقصد"
        await event.edit(f"✅ این گپ به مقصدهای بنر #{bid} اضافه شد.\n🎯 تعداد مقصدها: {len(banner['targets'])}{extra}")
        return

    m = re.fullmatch(r"حذف گپ هدف بنر\s+(\d+)", _fa_digits(text))
    if m:
        bid = int(m.group(1))
        banners = self_banners(uid)
        banner = _banner_from_list(banners, bid)
        if not banner or event.chat_id is None or not event.is_group:
            await event.edit("❌ این دستور را داخل گروه هدف اجرا کن.")
            return
        chat_id = int(event.chat_id)
        banner["targets"] = [int(x) for x in banner.get("targets", []) if int(x) not in {0, chat_id}]
        self_save_banners(uid, banners)
        await event.edit(f"✅ این گپ از مقصدهای بنر #{bid} حذف شد.\n🎯 تعداد مقصدها: {len(banner['targets'])}")
        return

    m = re.fullmatch(r"تنظیم هدف بنر\s+(\d+)\s+تمام گپ ها", _fa_digits(text))
    if m:
        bid = int(m.group(1))
        banners = self_banners(uid)
        banner = _banner_from_list(banners, bid)
        if not banner:
            await event.edit("❌ بنر موردنظر پیدا نشد.")
            return
        client = _get_tabchi_client(uid)
        if not client:
            await event.edit("❌ سلف فعال نیست.")
            return
        targets = set()
        async for dialog in client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            if getattr(dialog, "is_group", False) and not getattr(entity, "broadcast", False):
                try:
                    targets.add(int(dialog.id))
                except (TypeError, ValueError):
                    pass
        banner["targets"] = sorted(x for x in targets if x != 0)
        self_save_banners(uid, banners)
        sent = failed = 0
        if self_get(uid, "banner_auto", "off") == "on" and banner["targets"]:
            client = _get_tabchi_client(uid)
            if client:
                sent, failed = await _banner_dispatch_configured_now(client, uid, banner)
                self_save_banners(uid, banners)
        extra = f"\n📨 ارسال فوری: {sent} مقصد" if sent else ""
        if failed:
            extra += f"\n⚠️ ناموفق: {failed} مقصد"
        await event.edit(f"✅ بنر #{bid} برای {len(banner['targets'])} گپ تنظیم شد.{extra}")
        return

    m = re.fullmatch(r"فور بنر در\s+(\d+)\s+پیوی اخیر", _fa_digits(text))
    if m:
        bid, count = int(m.group(1)), int(m.group(2))
        banner = _banner_by_id(uid, bid)
        if not banner or count < 1:
            await event.edit("❌ بنر یا تعداد نامعتبر است.")
            return
        client = _get_tabchi_client(uid)
        if not client:
            await event.edit("❌ سلف فعال نیست.")
            return
        targets = await _banner_recent_pv(client, count)
        sent, failed = await _banner_dispatch_now(client, uid, banner, targets)
        await event.edit(f"✅ بنر #{bid} به {sent} پیوی اخیر ارسال شد.\n❌ ناموفق: {failed}")
        return

    if low == "وضعیت تبچی":
        status = "روشن ✅" if self_get(uid, "banner_auto", "off") == "on" else "خاموش ❌"
        banners = self_banners(uid)
        await event.edit(
            f"📢 <b>وضعیت تبچی</b>\n\n"
            f"🔘 ارسال خودکار بنرها: {status}\n"
            f"📦 تعداد بنرهای ثبت‌شده: {len(banners)}"
        )
        return

    if low == "لیست بنر هام":
        banners = self_banners(uid)
        if not banners:
            await event.edit("📢 <b>لیست بنرها خالی است.</b>", parse_mode="html")
            return
        body = ["📢 <b>بنرهای فعال</b>\n"]
        for b in banners:
            body.append(
                f"\n<b>#{b['id']}</b> • {'فوروارد' if b.get('mode') == 'forward' else 'کپی'}"
                f" • هر {int(b.get('interval', 60))} دقیقه • مقصد: {len(b.get('targets', []))}"
            )
        await event.edit("".join(body), parse_mode="html")
        return

    switches = {
        "بولد روشن": ("bold", "on"), "بولد خاموش": ("bold", "off"),
        "فونت فارسی روشن": ("persian_font", "on"), "فونت فارسی خاموش": ("persian_font", "off"),
        "ترنسلیت روشن": ("translate", "on"), "ترنسلیت خاموش": ("translate", "off"),
        "تبچی روشن": ("banner_auto", "on"), "تبچی خاموش": ("banner_auto", "off"),
        "پاسخ خودکار روشن": ("auto_reply", "on"), "پاسخ خودکار خاموش": ("auto_reply", "off"),
        "سین روشن": ("auto_read", "on"), "سین خاموش": ("auto_read", "off"),
        "تایپینگ روشن": ("typing", "on"), "تایپینگ خاموش": ("typing", "off"),
        "حالت بازی روشن": ("game_mode", "on"), "حالت بازی خاموش": ("game_mode", "off"),
        "ساعت روشن": ("time_name", "on"), "ساعت خاموش": ("time_name", "off"),
    }
    if low in switches:
        key, val = switches[low]
        if key == "banner_auto" and val == "on":
            client = _get_tabchi_client(uid)
            if not client:
                await event.edit("❌ سلف فعال نیست.")
                return
            self_set(uid, key, val)
            sent, failed = await _banner_dispatch_all_configured(client, uid)
            status = f"روشن ✅\n📨 ارسال فوری: {sent} مقصد"
            if failed:
                status += f"\n⚠️ ناموفق: {failed}"
        else:
            self_set(uid, key, val)
            status = "روشن" if val == "on" else "خاموش"

        with contextlib.suppress(Exception):
            await event.edit(f"✅ {text}\nوضعیت: {status}")
        return

    if low.startswith("پاسخ خودکار جدید"):
        keyword = text[len("پاسخ خودکار جدید"):].strip().casefold()
        if not keyword:
            await event.edit("❌ بعد از «پاسخ خودکار جدید» کلمه را بنویس.")
            return
        mapping = self_auto_reply_map(uid)
        mapping[keyword] = mapping.get(keyword, "")
        self_save_auto_reply_map(uid, mapping)
        await event.edit(f"✅ کلمه «{html.escape(keyword)}» برای پاسخ خودکار ثبت شد.")
        return

    if low.startswith("ذخیره پاسخ خودکار"):
        if not event.is_reply:
            await event.edit("❌ این دستور باید روی پیام پاسخ ریپلای شود.")
            return
        keyword = text[len("ذخیره پاسخ خودکار"):].strip().casefold()
        replied = await event.get_reply_message()
        if not keyword or not replied or not (replied.raw_text or "").strip():
            await event.edit("❌ کلمه را مشخص کن و روی پیام متنی موردنظر ریپلای کن.")
            return
        mapping = self_auto_reply_map(uid)
        if keyword not in mapping:
            mapping[keyword] = ""
        mapping[keyword] = replied.raw_text.strip()
        self_save_auto_reply_map(uid, mapping)
        await event.edit(f"✅ پاسخ خودکار برای «{html.escape(keyword)}» ذخیره شد.")
        return

    if low.startswith("حذف پاسخ خودکار"):
        keyword = text[len("حذف پاسخ خودکار"):].strip().casefold()
        mapping = self_auto_reply_map(uid)
        if not keyword or keyword not in mapping:
            await event.edit("❌ این کلمه در لیست پاسخ خودکار وجود ندارد.")
            return
        mapping.pop(keyword, None)
        self_save_auto_reply_map(uid, mapping)
        await event.edit(f"✅ پاسخ خودکار «{html.escape(keyword)}» حذف شد.")
        return

    if low == "لیست پاسخ خودکار":
        mapping = self_auto_reply_map(uid)
        if not mapping:
            await event.edit("💬 <b>لیست پاسخ خودکار خالی است.</b>", parse_mode="html")
            return
        body = ["💬 <b>لیست پاسخ خودکار</b>\n"]
        for i, (keyword, reply) in enumerate(mapping.items(), 1):
            body.append(f"\n{i}. <code>{html.escape(keyword)}</code> → {html.escape(reply[:120]) if reply else '❌ بدون پاسخ'}")
        await event.edit("".join(body), parse_mode="html")
        return

    if low == "پینگ":
        started = time.perf_counter()
        with contextlib.suppress(Exception):
            await event.edit("🏓 در حال محاسبه پینگ...")
        latency = round((time.perf_counter() - started) * 1000, 2)
        await event.edit(f"🏓 پینگ سلف: {latency} ms")
        return

    if low.startswith("فونت ساعت"):
        value = SELF_FONT_ALIASES.get(text[len("فونت ساعت"):].strip().casefold(), text[len("فونت ساعت"):].strip().casefold())
        if value not in SELF_CLOCK_FONTS:
            await event.edit("❌ فونت نامعتبر است.\n" + " / ".join(SELF_FONT_ALIASES))
            return
        self_set(uid, "clock_font", value)
        await event.edit(self_font_preview(uid, "clock"), parse_mode="html")
        return

    if low.startswith("فونت انگلیسی"):
        value = SELF_ENGLISH_FONT_ALIASES.get(text[len("فونت انگلیسی"):].strip().casefold(), text[len("فونت انگلیسی"):].strip().casefold())
        if value not in SELF_ENGLISH_FONTS:
            await event.edit("❌ فونت نامعتبر است.\n" + " / ".join(SELF_ENGLISH_FONT_ALIASES))
            return
        self_set(uid, "english_font", value)
        await event.edit(self_font_preview(uid, "english"), parse_mode="html")
        return

    reaction_match = re.fullmatch(r"ریاکشن(?:\s+(.+?))?", text)
    if reaction_match:
        emoji = (reaction_match.group(1) or "❤️").strip()
        # Telegram's supported reaction set changes over time.  Never keep a
        # small hard-coded whitelist here.  Accept the complete user-supplied
        # emoji sequence and let Telegram validate whether it is a reaction.
        emoji = re.sub(r"\s+", "", emoji)
        if not emoji or len(emoji) > 32:
            await event.edit("❌ ایموجی ریاکشن نامعتبر است. مثال: ریاکشن 🔥")
            return
        if not event.is_reply:
            await event.edit("❌ روی پیام کاربر ریپلای کن و سپس «ریاکشن 🔥» را بفرست.")
            return
        replied = await event.get_reply_message()
        if not replied or not replied.sender_id:
            await event.edit("❌ کاربر پیدا نشد.")
            return
        target = int(replied.sender_id)
        targets = self_reaction_targets(uid)
        targets.add(target)
        self_save_reaction_targets(uid, targets)
        normalized_emoji = "❤️" if emoji == "❤" else emoji
        self_set_reaction(uid, target, normalized_emoji)

        # Validate immediately instead of waiting for the next incoming message.
        try:
            await _tg_call_with_flood_retry(
                lambda: event.client(
                    SendReactionRequest(
                        peer=event.chat_id,
                        msg_id=int(replied.id),
                        reaction=[ReactionEmoji(emoticon=normalized_emoji)],
                    )
                ),
                label="reaction test",
                max_retries=3,
            )
        except Exception as exc:
            # Keep the configuration only if Telegram accepts the reaction.
            # Otherwise roll it back so a bad emoji cannot poison future jobs.
            self_remove_reaction(uid, target)
            await event.edit(
                "❌ این ایموجی در ریاکشن‌های قابل‌استفاده تلگرام نیست یا "
                f"تلگرام آن را نپذیرفت: {html.escape(str(exc))}"
            )
            return

        await event.edit(
            f"✅ ریاکشن {normalized_emoji} برای کاربر `{target}` فعال شد."
        )
        return

    if low in {"حذف ریاکشن", "ریاکشن خاموش", "حذف ریاکشن ❤️", "حذف ریاکشن + ریپلای"}:
        if not event.is_reply:
            await event.edit("❌ روی پیام همان کاربر ریپلای کن.")
            return
        replied = await event.get_reply_message()
        if replied and replied.sender_id:
            target = int(replied.sender_id)
            targets = self_reaction_targets(uid)
            targets.discard(target)
            self_save_reaction_targets(uid, targets)
            self_remove_reaction(uid, target)
            await event.edit("✅ ریاکشن خودکار این کاربر حذف شد.")
        return

    if low in {"قفل چت", "قفل چت + ریپلای"}:
        if not event.is_private or not event.is_reply:
            await event.edit("❌ در پیوی، روی پیام همان کاربر ریپلای کن و «قفل چت» را بفرست.")
            return
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        if not target or target == uid:
            await event.edit("❌ کاربر هدف پیدا نشد.")
            return
        targets = self_chat_lock_targets(uid)
        targets.add(target)
        self_save_chat_lock_targets(uid, targets)
        await event.edit(f"🔒 قفل چت برای `{target}` فعال شد. پیام‌های بعدی این کاربر دوطرفه پاک می‌شوند.")
        return

    if low in {"بازکردن قفل چت", "باز کردن قفل چت", "قفل چت خاموش", "قفل چت خاموش + ریپلای"}:
        if not event.is_private or not event.is_reply:
            await event.edit("❌ در پیوی روی پیام همان کاربر ریپلای کن.")
            return
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        targets = self_chat_lock_targets(uid)
        targets.discard(target)
        self_save_chat_lock_targets(uid, targets)
        await event.edit(f"🔓 قفل چت برای `{target}` خاموش شد.")
        return

    if low in {"بلاک + ریپلای", "بلاک ریپلای", "بلاک"} and event.is_reply:
        if not (event.is_group or event.is_channel):
            await event.edit("❌ این دستور برای گروه/سوپرگروه است.")
            return
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        if not target or target == uid:
            await event.edit("❌ کاربر هدف پیدا نشد.")
            return
        try:
            await _tg_call_with_flood_retry(
                lambda: event.client(functions.contacts.BlockRequest(id=target)),
                label="block user",
            )
            await event.edit(f"🚫 کاربر `{target}` بلاک شد.")
        except Exception as exc:
            await event.edit(f"❌ بلاک انجام نشد: {exc}")
        return

    if text.startswith(("/", ".")):
        return

    transformed = text
    changed = False
    if self_get(uid, "translate") == "on":
        translated = await self_translate(event.client, transformed)
        if translated:
            transformed = translated
            changed = True
    if self_get(uid, "persian_font") == "on":
        transformed = self_stretch(transformed)
        changed = True
    if self_get(uid, "english_font", "normal") != "normal":
        transformed = self_transform_english(transformed, uid)
        changed = True
    if self_get(uid, "bold") == "on":
        await event.edit(f"<strong>{transformed}</strong>", parse_mode="html")
        return
    if changed:
        await event.edit(transformed)


# ============================================================
# SELF INCOMING / PRESENCE
# ============================================================

def _cache_private_message(uid, message):
    """Keep only incoming private messages for deleted-message archiving."""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    sender_id = getattr(message, "sender_id", None)
    # Never cache the owner's own messages. This prevents self-deletions from
    # ever becoming archive candidates.
    if not chat_id or not message_id or not sender_id or int(sender_id) == int(uid):
        return
    key = (int(uid), int(chat_id))
    bucket = _deleted_message_cache.setdefault(key, [])
    bucket.append(message)
    _deleted_message_index[(int(uid), int(message_id))] = int(chat_id)
    if len(bucket) > 5:
        stale = bucket[:-5]
        del bucket[:-5]
        for old in stale:
            old_id = getattr(old, "id", None)
            if old_id is not None:
                _deleted_message_index.pop((int(uid), int(old_id)), None)


async def _archive_messages_to_saved(client, uid, messages):
    """Copy archived messages to Saved Messages and always keep author identity visible."""
    saved = 0
    seen = set()
    for msg in sorted(messages, key=lambda m: getattr(m, "id", 0)):
        msg_id = getattr(msg, "id", None)
        if msg_id in seen:
            continue
        seen.add(msg_id)

        sender_id = getattr(msg, "sender_id", None)
        # Only the other participant's messages may be archived.
        if not sender_id or int(sender_id) == int(uid):
            continue
        if sender_id:
            try:
                sender = await client.get_entity(int(sender_id))
                username = getattr(sender, "username", None)
                first_name = getattr(sender, "first_name", None) or "کاربر"
                sender_label = f"@{username}" if username else f"{first_name} | ID: {int(sender_id)}"
            except Exception:
                sender_label = f"ID: {int(sender_id)}"
        else:
            sender_label = "نامشخص"
        author_footer = f"👤 نویسنده: {sender_label}"

        # Deliberately copy instead of forwarding.  This keeps Saved Messages
        # clean while the explicit author footer guarantees attribution even
        # when Telegram cannot create a forward for a deleted message.
        try:
            body = (msg.raw_text or "").strip()
            caption = f"{body}\n\n{author_footer}" if body else author_footer
            if getattr(msg, "media", None):
                path = await msg.download_media()
                if path:
                    try:
                        await client.send_file("me", path, caption=caption)
                    finally:
                        with contextlib.suppress(Exception):
                            os.remove(path)
                    saved += 1
            elif body:
                await client.send_message("me", caption)
                saved += 1
        except Exception as exc:
            print(f"[SELF {uid}] deleted-chat archive {msg_id or '?'}: {exc}")
    return saved


async def _archive_last_five_before_delete(client, uid, chat_id, deleted_ids=None):
    """Archive deleted messages; snapshot five only when the whole private history is gone."""
    try:
        deleted_ids = {int(x) for x in (deleted_ids or [])}
        cached = list(_deleted_message_cache.get((int(uid), int(chat_id)), []))
        deleted_messages = [m for m in cached if getattr(m, "id", 0) in deleted_ids]

        # MessageDeleted does not reliably expose whether the user chose
        # "delete entire chat".  The most reliable post-delete signal is that
        # no message remains in the private dialog.  Only in that case do we
        # take the five-message snapshot. A normal message deletion archives
        # only the message(s) actually deleted and never unrelated messages.
        remaining = None
        query_ok = False
        try:
            query_ok = True
            async for _ in client.iter_messages(chat_id, limit=1):
                remaining = True
                break
        except Exception as exc:
            query_ok = False
            print(f"[SELF {uid}] deleted-chat remaining-message check failed: {exc}")
        whole_chat_cleared = query_ok and remaining is not True

        if whole_chat_cleared and cached:
            snapshot = sorted(cached, key=lambda m: getattr(m, "id", 0))[-5:]
            return await _archive_messages_to_saved(client, uid, snapshot)

        return await _archive_messages_to_saved(client, uid, deleted_messages)
    except Exception as exc:
        print(f"[SELF {uid}] deleted-chat archive failed: {exc}")
        return 0

async def self_handle_incoming(event, uid):
    client = event.client

    sender_id = int(event.sender_id) if event.sender_id else 0
    if event.is_private and sender_id and sender_id != int(uid):
        _cache_private_message(uid, event.message)
    if (event.is_group or event.is_channel) and sender_id and sender_id in _global_ban_list(uid) and sender_id != int(uid):
        with contextlib.suppress(Exception):
            if event.is_group:
                await client.edit_permissions(event.chat_id, sender_id, view_messages=False, send_messages=False)
        return

    if event.is_private and sender_id in self_chat_lock_targets(uid) and sender_id != int(uid):
        # Delete this incoming message for both sides when Telegram allows revoke.
        with contextlib.suppress(Exception):
            await _tg_call_with_flood_retry(
                lambda: client.delete_messages(event.chat_id, event.id, revoke=True),
                label="chat lock incoming delete",
            )
        return

    if self_get(uid, "auto_read", "off") == "on":
        with contextlib.suppress(Exception):
            await client.send_read_acknowledge(event.chat_id, max_id=event.id)

    if event.sender_id and int(event.sender_id) in self_reaction_targets(uid):
        target_sender = int(event.sender_id)
        emoji = self_reaction_map(uid).get(target_sender, "❤️")
        try:
            await _tg_call_with_flood_retry(
                lambda: client(SendReactionRequest(
                    peer=event.peer_id,
                    msg_id=event.id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                )),
                label="automatic reaction",
                max_retries=3,
            )
        except Exception as exc:
            # A reaction can become unavailable after it was configured.
            # Disable only that broken mapping instead of breaking all
            # incoming-message processing.
            print(
                f"[REACTION {uid}] emoji {emoji!r} failed for {target_sender}: {exc}"
            )
            self_remove_reaction(uid, target_sender)
            self_save_reaction_targets(
                uid,
                self_reaction_targets(uid) - {target_sender},
            )

    if event.is_private and self_get(uid, SECRETARY_ENABLED_KEY, "off") == "on" and event.sender_id and int(event.sender_id) != int(uid):
        sender = int(event.sender_id)
        key = (int(uid), sender)
        now = time.time()
        interval = max(5, min(60, int(self_get(uid, SECRETARY_INTERVAL_KEY, "5") or 5))) * 60
        last = float(_secretary_reply_cache.get(key, 0) or 0)
        if now - last >= interval:
            reply = _secretary_reply(uid)
            if reply:
                try:
                    if reply.get("kind") == "media" and reply.get("path") and Path(reply["path"]).exists():
                        await client.send_file(event.chat_id, reply["path"], caption=(reply.get("caption") or "")[:4096])
                    else:
                        await client.send_message(event.chat_id, reply.get("text") or reply.get("caption") or "")
                    _secretary_reply_cache[key] = now
                except Exception as exc:
                    print(f"[SECRETARY {uid}] reply failed: {exc}")

    if event.is_private and self_get(uid, "auto_reply", "off") == "on" and event.sender_id and int(event.sender_id) != int(uid):
        incoming_text = (event.raw_text or "").strip().casefold()
        replies = self_auto_reply_map(uid)
        if incoming_text in replies and replies.get(incoming_text):
            cache_key = (int(uid), int(event.sender_id), incoming_text)
            if cache_key not in _self_reply_cache:
                _self_reply_cache.add(cache_key)
                with contextlib.suppress(Exception):
                    await event.respond(replies[incoming_text])
                asyncio.create_task(_clear_self_reply_cache(cache_key))

async def _clear_self_reply_cache(key):
    await asyncio.sleep(60)
    _self_reply_cache.discard(key)

async def _send_presence(client, entity, game=False):
    try:
        from telethon.tl.types import SendMessageGamePlayAction
        action = SendMessageGamePlayAction() if game else SendMessageTypingAction()
        await client(SetTypingRequest(peer=entity, action=action))
        return True
    except Exception:
        return False

async def _presence_loop(client, uid):
    while True:
        try:
            typing_on = self_get(uid, "typing", "off") == "on"
            game_on = self_get(uid, "game_mode", "off") == "on"
            if not typing_on and not game_on:
                await asyncio.sleep(2)
                continue
            async for dialog in client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if not entity or getattr(entity, "bot", False) or getattr(entity, "id", None) == uid:
                    continue
                await _send_presence(client, entity, game=game_on)
                await asyncio.sleep(0.05)
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SELF {uid}] presence loop: {exc}")
            await asyncio.sleep(3)

# ============================================================
# SELF WORKER
# ============================================================

def time_name_enabled(user_id: int) -> bool:
    return get_setting(user_id, "time_name", "on") == "on"


async def update_time_name(user_id: int, client):
    if not time_name_enabled(user_id):
        return
    try:
        me = await client.get_me()
        if not me:
            return

        first = me.first_name or "کاربر"
        clean = _clean_clock_suffix(first)
        # IMPORTANT: use the selected clock font here.  The previous worker
        # hard-coded ASCII digits, so the panel preview changed but the name
        # always received the default/plain clock.
        new_first = f"{clean[:55]} {self_clock(user_id)}"

        if new_first != first:
            from telethon.tl.functions.account import UpdateProfileRequest
            await client(UpdateProfileRequest(first_name=new_first))
    except Exception as exc:
        print(f"[SELF {user_id}] time-name update skipped: {exc}")


async def self_worker(user_id: int, session_string: str, sub_type: int = 0):
    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
        device_model="Diamond Self",
        system_version="Python",
        app_version="1.0"
    )

    self_clients[user_id] = client
    presence_task = None

    @client.on(events.NewMessage(outgoing=True))
    async def _self_outgoing(event):
        try:
            # Outgoing messages belong to the account owner and must never be
            # placed in the deleted-message archive cache.
            await self_handle_outgoing(event, user_id)
            await _maybe_first_comment(event, user_id)
        except Exception as exc:
            print(f"[SELF {user_id}] outgoing handler error: {exc}")

    @client.on(events.NewMessage(incoming=True))
    async def _self_incoming(event):
        try:
            await self_handle_incoming(event, user_id)
            # FIRST COMMENT must also react to NEW POSTS received by the SELF
            # account in a configured broadcast channel. Previously this feature
            # was called only from the outgoing handler, so a normal channel post
            # was never processed unless SELF itself forwarded it somewhere.
            await _maybe_first_comment(event, user_id)
        except Exception as exc:
            print(f"[SELF {user_id}] incoming handler error: {exc}")

    @client.on(events.MessageDeleted)
    async def _self_deleted(event):
        try:
            deleted_ids = [int(x) for x in (getattr(event, "deleted_ids", None) or [])]
            if not deleted_ids:
                return

            # MessageDeleted is not a reliable source of peer/chat_id for normal
            # private chats. Resolve the chat from the short-lived message index
            # populated when NewMessage arrived.
            chats = {}
            for message_id in deleted_ids:
                chat_id = _deleted_message_index.get((int(user_id), message_id))
                if chat_id is not None:
                    chats.setdefault(int(chat_id), []).append(message_id)

            for chat_id, ids in chats.items():
                # Chat-lock intentionally deletes incoming messages and must not
                # archive those messages into Saved Messages.
                if chat_id in self_chat_lock_targets(user_id):
                    continue
                await _archive_last_five_before_delete(client, user_id, chat_id, ids)
        except Exception as exc:
            print(f"[SELF {user_id}] deleted-message handler error: {exc}")

    try:
        await client.connect()

        if not await client.is_user_authorized():
            deactivate_session(user_id)
            print(f"[SELF {user_id}] session is no longer authorized")
            return

        self_workers[user_id] = asyncio.current_task()
        presence_task = asyncio.create_task(_presence_loop(client, user_id))
        print(f"[SELF {user_id}] started")

        last_clock_value = None

        while True:
            if not get_active_session(user_id):
                break

            balance = get_balance(user_id)
            if balance < 2:
                print(f"[SELF {user_id}] balance ended; stopping")
                deactivate_session(user_id)
                break

            clock_value = self_clock(user_id) if time_name_enabled(user_id) else None
            # Check every second but call Telegram only when the displayed minute changes.
            # This removes the old 15s loop + 30s polling drift while avoiding API spam.
            if clock_value and clock_value != last_clock_value:
                await update_time_name(user_id, client)
                last_clock_value = clock_value

            try:
                await _banner_worker_tick(client, user_id)
            except Exception as exc:
                print(f"[BANNER {user_id}] worker tick error: {type(exc).__name__}: {exc}")

            # Billing: 2.5 diamonds/hour, accumulated safely using whole-diamond balance.
            # We charge floor(total_elapsed * 2.5), so the average rate is exactly 2.5/hour.
            start = get_active_session(user_id)
            if not start:
                break

            start_time = int(start[2])
            elapsed_hours = int((time.time() - start_time) // 3600)
            due_total = (elapsed_hours + 1) * float(SELF_HOURLY_COST)
            charged_total = float(get_setting(user_id, "charged_diamonds", "0") or 0)
            if due_total > charged_total:
                charge = due_total - charged_total
                with connect_db(user_id) as db:
                    db.execute(
                        "UPDATE users SET balance=MAX(balance-?,0) WHERE user_id=?",
                        (charge, user_id)
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('charged_diamonds',?)",
                        (str(due_total),)
                    )
                if get_balance(user_id) <= 0:
                    deactivate_session(user_id)
                    break

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[SELF {user_id}] worker error: {exc}")
    finally:
        if presence_task:
            presence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await presence_task
        self_workers.pop(user_id, None)
        self_clients.pop(user_id, None)
        with contextlib.suppress(Exception):
            await client.disconnect()
        print(f"[SELF {user_id}] stopped")


async def start_self_worker(user_id: int, session_string: str, sub_type: int = 0):
    old = self_workers.get(user_id)
    if old and not old.done():
        old.cancel()
        with contextlib.suppress(Exception):
            await old

    task = asyncio.create_task(
        self_worker(user_id, session_string, sub_type)
    )
    self_workers[user_id] = task


async def stop_self_worker(user_id: int):
    task = self_workers.get(user_id)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    deactivate_session(user_id)


# ============================================================
# LOGIN FLOW
# ============================================================

async def code_timeout(user_id: int):
    await asyncio.sleep(180)
    state = pending.get(user_id)
    if state and state.get("step") in {"code", "password"}:
        client = state.get("client")
        if client:
            with contextlib.suppress(Exception):
                await client.disconnect()
        pending.pop(user_id, None)
        with contextlib.suppress(Exception):
            await bot.send_message(
                user_id,
                "⏰ زمان ورود تمام شد. دوباره از ابتدا تلاش کنید."
            )


async def begin_self_login(user_id: int, event=None):
    # Never start a new login/activation flow when this user already has
    # an active self. The management panel remains available separately.
    if get_active_session(user_id):
        if event:
            await safe_answer(event, "❌ شما یه سلف فعال دارید!", True)
        else:
            await bot.send_message(user_id, "❌ شما یه سلف فعال دارید!")
        return

    balance = get_balance(user_id)
    if balance < MIN_SELF_BALANCE:
        text = (
            "❌ موجودی شما کافی نیست.\n\n"
            f"💎 موجودی: {_fmt_diamonds(balance)}\n"
            f"💎 حداقل موجودی لازم: {MIN_SELF_BALANCE:,} الماس\n"
            f"💎 هزینه: {SELF_HOURLY_COST:g} الماس در ساعت"
        )
        if event:
            await safe_answer(event, text, True)
        else:
            await bot.send_message(user_id, text)
        return

    phone = get_phone_number(user_id)
    if not phone:
        if event:
            await safe_answer(event, "❌ ابتدا شماره موبایل ایران خود را ثبت کنید.", True)
        else:
            await bot.send_message(user_id, "❌ ابتدا با /start شماره موبایل خود را ثبت کنید.")
        return

    pending.pop(user_id, None)
    if event:
        await safe_answer(event, "⏳ در حال ارسال کد ورود به شماره ثبت‌شده…")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        pending[user_id] = {
            "step": "code",
            "sub_type": 0,
            "timer": asyncio.create_task(code_timeout(user_id)),
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
        }
        await bot.send_message(
            user_id,
            "🔐 کد ورود به شماره ثبت‌شده ارسال شد.\n\n"
            "کد را به صورت اعداد ارسال کن. برای اینکه کد را سریع و بدون اشتباه وارد کنی، می‌توانی مثل نمونه زیر ارسال کنی:\n"
            "`1.2.4.3.5`\n\n"
            "اگر ورود دو مرحله‌ای فعال باشد، بعد از کد رمز دو مرحله‌ای را می‌پرسم."
        )
    except PhoneNumberInvalidError:
        await bot.send_message(user_id, "❌ شماره ثبت‌شده معتبر نیست. دوباره شماره خودت را از طریق دکمه اشتراک‌گذاری ثبت کن.")
        with contextlib.suppress(Exception):
            await client.disconnect()
    except FloodWaitError as exc:
        await bot.send_message(user_id, f"⏳ تلگرام موقتاً محدود کرده است.\nدوباره بعد از {exc.seconds} ثانیه تلاش کن.")
        with contextlib.suppress(Exception):
            await client.disconnect()
    except Exception as exc:
        print(f"[SELF LOGIN {user_id}] {exc}")
        await bot.send_message(user_id, "❌ ارسال کد ورود ناموفق بود. چند لحظه بعد دوباره تلاش کن.")
        with contextlib.suppress(Exception):
            await client.disconnect()
        pending.pop(user_id, None)


async def finish_login(user_id: int):
    state = pending.get(user_id)
    if not state or not state.get("client"):
        return

    client = state["client"]
    session_string = client.session.save()

    if not session_string:
        await bot.send_message(user_id, "❌ ساخت SessionString ناموفق بود.")
        return

    # Re-check here as well so a pending login can never activate a second
    # self if another activation/backup restore became active meanwhile.
    if get_active_session(user_id):
        await bot.send_message(user_id, "❌ شما یه سلف فعال دارید!")
        with contextlib.suppress(Exception):
            await client.disconnect()
        pending.pop(user_id, None)
        return

    # Charge only after successful authorization.
    if get_balance(user_id) < MIN_SELF_BALANCE:
        await bot.send_message(user_id, "❌ موجودی شما دیگر کافی نیست.")
        with contextlib.suppress(Exception):
            await client.disconnect()
        pending.pop(user_id, None)
        return

    # First 2.5 diamonds are charged immediately when activation succeeds.
    activation_cost = float(SELF_HOURLY_COST)
    change_balance(user_id, -activation_cost)
    save_active_session(user_id, session_string, state.get("sub_type", 0))
    set_setting(user_id, "charged_diamonds", str(activation_cost))

    await start_self_worker(
        user_id,
        session_string,
        state.get("sub_type", 0)
    )

    await bot.send_message(
        user_id,
        "✅ سلف با موفقیت فعال شد!\n\n"
        f"💎 {SELF_HOURLY_COST:g} الماس همان لحظه فعال‌سازی کسر شد؛ از این پس هر ساعت {SELF_HOURLY_COST:g} الماس کسر می‌شود."
    )

    with contextlib.suppress(Exception):
        await client.disconnect()

    pending.pop(user_id, None)


# ============================================================
# INLINE MODE
# ============================================================

@bot.on(events.InlineQuery)
async def inline_query_handler(event):
    """Return the self panel only through Telegram inline mode."""
    query = (event.text or "").strip().casefold()
    print(f"[INLINE QUERY] uid={getattr(event, 'sender_id', '?')} query={query!r}")
    if query not in {"پنل", "راهنما"}:
        await event.answer([], cache_time=0, private=True)
        return

    uid = int(event.sender_id)
    if is_banned(uid):
        result = event.builder.article(
            title="🚫 دسترسی مسدود است",
            description="حساب شما توسط مدیریت مسدود شده است.",
            text="🚫 شما توسط ادمین مسدود شده‌اید."
        )
        await event.answer([result], cache_time=0, private=True)
        return

    if not has_registered_phone(uid):
        result = event.builder.article(
            title="📱 ابتدا شماره را ثبت کنید",
            description="برای استفاده از پنل، ابتدا شماره موبایل خود را ثبت کنید.",
            text="📱 برای استفاده از پنل، ابتدا در گفت‌وگوی ربات /start را بزنید و شماره خود را ثبت کنید."
        )
        await event.answer([result], cache_time=0, private=True)
        return

    if query == "راهنما":
        result = event.builder.article(
            title="📚 راهنمای قابلیت‌ها",
            description="قابلیت موردنظر را از منوی راهنما انتخاب کنید.",
            text="📚 <b>راهنمای قابلیت‌ها</b>\n\nقابلیت موردنظر را انتخاب کن:",
            parse_mode="html",
            buttons=self_feature_guide_buttons(uid),
        )
    else:
        result = event.builder.article(
            title="⚙️ پنل سلف",
            description="پنل تنظیمات سلف را همین‌جا با Inline باز کن.",
            text=self_panel_text(uid),
            parse_mode="html",
            buttons=self_panel_buttons(uid),
        )
    await event.answer([result], cache_time=0, private=True)


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

@bot.on(events.NewMessage)
async def on_message(event):
    user_id = event.sender_id

    if not user_id:
        return

    if event.is_private:
        await private_message(event)
    elif event.is_group or event.is_channel:
        await group_commands(event)


async def process_referral(user_id: int, referrer: int):
    if referrer <= 0 or referrer == user_id:
        return False
    init_user_db(user_id)
    init_user_db(referrer)
    # The referred user can only be assigned once.
    with connect_db(user_id) as db: user_row = db.execute("SELECT invited_by FROM users WHERE user_id=?", (user_id,)).fetchone()
    if user_row and int(user_row[0] or 0) != 0:
        return False
    # Store the relationship in the referrer's database, where referral counts live.
    with connect_db(referrer) as rdb:
        exists = rdb.execute("SELECT 1 FROM referrals WHERE referred_id=?", (user_id,)).fetchone()
        if exists:
            return False
        rdb.execute("INSERT INTO referrals(referrer_id,referred_id,reward_claimed) VALUES(?,?,1)", (referrer, user_id))
        rdb.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REFERRAL_REWARD, referrer))
    with connect_db(user_id) as udb:
        udb.execute("UPDATE users SET invited_by=? WHERE user_id=?", (referrer, user_id))
    with contextlib.suppress(Exception):
        await bot.send_message(referrer, f"🎉 دعوت موفق بود!\n\n💎 {REFERRAL_REWARD:,} الماس به موجودی شما اضافه شد.")
    return True


async def private_message(event):
    user_id = event.sender_id
    text = event.raw_text or ""

    init_user_db(user_id)

    if not is_bot_enabled() and user_id not in ADMINS:
        await event.reply(BOT_UPDATE_TEXT)
        return

    # --------------------------------------------------------
    # START + REFERRAL
    # --------------------------------------------------------
    if text.startswith("/start"):
        if is_banned(user_id):
            await event.reply("🚫 شما توسط ادمین مسدود شده‌اید.")
            return

        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            with contextlib.suppress(Exception):
                referrer = int(parts[1])
                if referrer != user_id and not has_registered_phone(user_id):
                    set_setting(user_id, "pending_referrer", str(referrer))
                else:
                    await process_referral(user_id, referrer)

        if not await ensure_force_join(user_id):
            return

        if not has_registered_phone(user_id):
            await send_phone_request(user_id)
            return

        await send_main(user_id, user_id)
        return

    # --------------------------------------------------------
    # REQUIRED IRANIAN PHONE REGISTRATION
    # --------------------------------------------------------
    if event.contact:
        contact = event.contact
        contact_user_id = getattr(contact, "user_id", None)
        phone = normalize_iran_phone(getattr(contact, "phone_number", ""))
        if contact_user_id not in (None, user_id):
            await event.reply("❌ فقط شماره خودت را از دکمه اشتراک‌گذاری ارسال کن.")
            return
        if not phone:
            await event.reply("❌ فقط شماره موبایل ایران با پیش‌شماره +98 پذیرفته می‌شود.")
            return
        save_phone_number(user_id, phone)
        pending_referrer = get_setting(user_id, "pending_referrer")
        if pending_referrer:
            with contextlib.suppress(Exception):
                await process_referral(user_id, int(pending_referrer))
            set_setting(user_id, "pending_referrer", "")
        await bot.send_message(user_id, "✅ شماره شما ثبت شد.", buttons=Button.clear())
        if not await ensure_force_join(user_id):
            return
        await send_main(user_id, user_id)
        return

    # --------------------------------------------------------
    # ADMIN INPUTS: FORCE JOIN / BACKUP
    # --------------------------------------------------------
    state = pending.get(user_id)

    if user_id in ADMINS and state and state.get("step") == "force_join_add":
        raw = (event.raw_text or "").strip()
        try:
            entity = None
            url = ""

            # Private channel/group: forward any message from it to the bot.
            fwd = getattr(event.message, "fwd_from", None)
            fwd_peer = getattr(fwd, "from_id", None) if fwd else None
            if fwd_peer is not None:
                entity = await bot.get_entity(fwd_peer)
                url = ""
                # Generate a direct invite link when the bot has permission.
                with contextlib.suppress(Exception):
                    invite = await bot(functions.messages.ExportChatInviteRequest(peer=entity))
                    url = getattr(invite, "link", None) or ""
            else:
                # Public channel/group: @username or https://t.me/username
                username = raw.split("/")[-1].lstrip("@").strip()
                if not username or " " in username:
                    raise RuntimeError("invalid_username")
                entity = await bot.get_entity("@" + username)
                username_value = getattr(entity, "username", None) or username
                url = f"https://t.me/{username_value}"

            if not isinstance(entity, (types.Channel, types.Chat)):
                raise RuntimeError("not_supported_chat")

            title = getattr(entity, "title", None) or getattr(entity, "username", None) or "گروه/کانال"
            channel = {
                "id": int(entity.id),
                "title": title,
                "username": getattr(entity, "username", None),
                "url": url,
                "private": not bool(getattr(entity, "username", None)),
            }
            channels = [c for c in get_force_join_channels() if int(c.get("id", 0)) != channel["id"]]
            channels.append(channel)
            save_force_join_channels(channels)
            pending.pop(user_id, None)
            link_note = " لینک دعوت خصوصی هم ساخته شد." if channel["private"] and url else ""
            await event.reply(
                f"✅ «{channel['title']}» به جوین اجباری اضافه شد.{link_note}",
                buttons=[[btn("📢 مدیریت جوین اجباری", b"force_join", "primary")]],
            )
        except Exception as exc:
            print(f"[FORCE JOIN] add failed: {exc}")
            await event.reply(
                "❌ افزودن انجام نشد. برای کانال خصوصی، یک پیام از همان کانال را فوروارد کن "
                "و مطمئن شو بات داخل کانال/گروه دسترسی لازم برای بررسی عضویت و ساخت لینک دعوت را دارد."
            )
        return

    if user_id in ADMINS and state and state.get("step") == "backup_restore":
        document = getattr(event, "document", None)
        if not document:
            await event.reply("❌ فایل ZIP بکاپ را ارسال کن.")
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_backup_upload_{user_id}_"))
        archive_path = tmp_dir / "backup.zip"
        try:
            downloaded = await event.download_media(file=str(archive_path))
            if not downloaded:
                raise RuntimeError("download_failed")
            await asyncio.to_thread(inspect_backup_sync, archive_path)
            await bot.send_message(user_id, "⏳ بکاپ معتبر است؛ در حال توقف Workerها و بازگردانی اطلاعات...")
            await stop_all_self_workers_for_backup()
            manifest = await asyncio.to_thread(restore_backup_sync, archive_path)
            pending.pop(user_id, None)
            await bot.send_message(
                user_id,
                "✅ بکاپ با موفقیت بازگردانی شد.\n"
                "⚙️ اطلاعات کاربران، موجودی‌ها، sessionها و تنظیمات برگشتند.\n"
                "🔄 در حال بازیابی Workerهای فعال..."
            )
            await restore_workers()
            await send_main(user_id, user_id)
        except Exception as exc:
            print(f"[BACKUP] restore failed: {exc}")
            await bot.send_message(user_id, "❌ بازگردانی انجام نشد؛ بکاپ فعلی دست‌نخورده باقی ماند.")
        finally:
            pending.pop(user_id, None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    # --------------------------------------------------------
    # RECEIPT
    # --------------------------------------------------------
    state = pending.get(user_id)
    if state and state.get("step") == "receipt":
        if not (event.photo or event.document):
            await event.reply("📸 لطفاً عکس رسید پرداخت را ارسال کنید.")
            return

        receipt = None
        try:
            receipt = await event.download_media()
            amount = int(state["amount"])
            diamonds = int(state["diamonds"])

            sender = await event.get_sender()
            username = getattr(sender, "username", None)

            info = (
                "🧾 **رسید پرداخت جدید**\n\n"
                f"🆔 آیدی: `{user_id}`\n"
                f"👤 نام: {getattr(sender, 'first_name', 'کاربر')}\n"
                f"📱 یوزرنیم: @{username if username else 'ندارد'}\n\n"
                f"💎 الماس: {diamonds:,}\n"
                f"💰 مبلغ: {amount:,} تومان\n"
                f"💳 کارت: {CARD_NUMBER}\n"
                f"👤 صاحب کارت: {CARD_HOLDER}"
            )

            buttons = [
                [
                    btn(
                        "✅ تأیید",
                        f"pay_confirm_{user_id}_{diamonds}".encode(),
                        "success"
                    ),
                    btn(
                        "❌ رد",
                        f"pay_reject_{user_id}".encode(),
                        "danger"
                    ),
                ]
            ]

            delivered = 0
            for admin_id in ADMINS:
                try:
                    await bot.send_file(
                        admin_id,
                        receipt,
                        caption=info,
                        buttons=buttons
                    )
                    delivered += 1
                except Exception as send_exc:
                    print(f"[RECEIPT] local delivery failed for {admin_id}: {send_exc}")
                    with contextlib.suppress(Exception):
                        await bot.send_file(
                            admin_id,
                            event.media,
                            caption=info,
                            buttons=buttons
                        )
                        delivered += 1
            if delivered:
                await event.reply(
                    "✅ رسید شما برای مدیریت ارسال شد.\n"
                    "لطفاً منتظر تأیید پرداخت باشید."
                )
                pending.pop(user_id, None)
            else:
                await event.reply("❌ ارسال رسید به مدیریت انجام نشد. لطفاً چند لحظه بعد دوباره ارسال کنید.")

        except Exception as exc:
            print(f"[RECEIPT] {exc}")
            await event.reply("❌ ارسال رسید ناموفق بود.")
        finally:
            if receipt and os.path.exists(receipt):
                with contextlib.suppress(Exception):
                    os.remove(receipt)
        return

    # --------------------------------------------------------
    # SELF LOGIN
    # --------------------------------------------------------
    if state:
        step = state.get("step")

        if step == "phone":
            pending.pop(user_id, None)
            await event.reply("📱 شماره را دستی وارد نکن. با /start دکمه «اشتراک‌گذاری شماره» را بزن.")
            return

        if step == "code":
            raw_code = text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
            code = re.sub(r"[.\s-]", "", raw_code)
            client = state.get("client")

            if not re.fullmatch(r"\d{5}", code):
                await event.reply("❌ فرمت کد نامعتبر است. کد را مثل نمونه ارسال کن: `1.2.4.3.5`")
                return

            if not client:
                pending.pop(user_id, None)
                await event.reply("❌ نشست ورود پیدا نشد. دوباره تلاش کنید.")
                return

            timer = state.get("timer")
            if timer:
                timer.cancel()

            try:
                await client.sign_in(
                    phone=state["phone"],
                    code=code,
                    phone_code_hash=state.get("phone_code_hash")
                )
                await finish_login(user_id)

            except SessionPasswordNeededError:
                state["step"] = "password"
                state["timer"] = asyncio.create_task(code_timeout(user_id))
                await event.reply("🔑 رمز دو مرحله‌ای را ارسال کنید.")

            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                await event.reply("❌ کد اشتباه یا منقضی شده است.")
                with contextlib.suppress(Exception):
                    await client.disconnect()
                pending.pop(user_id, None)

            except Exception as exc:
                await event.reply(f"❌ خطا در ورود: {exc}")
                with contextlib.suppress(Exception):
                    await client.disconnect()
                pending.pop(user_id, None)
            return

        if step == "password":
            client = state.get("client")
            try:
                await client.sign_in(password=text.strip())
                await finish_login(user_id)
            except Exception as exc:
                await event.reply(f"❌ رمز صحیح نیست یا ورود ناموفق بود: {exc}")
            return

    await admin_text_flow(event)


async def admin_text_flow(event):
    user_id = event.sender_id
    text = event.raw_text.strip()

    if user_id not in ADMINS:
        return

    state = pending.get(user_id)
    if not state:
        return

    step = state.get("step")

    if step == "add_balance_user":
        try:
            target = int(text)
            init_user_db(target)
            state["target_id"] = target
            state["step"] = "add_balance_amount"
            await event.reply("💎 مقدار الماس را وارد کنید:")
        except ValueError:
            await event.reply("❌ آیدی نامعتبر است.")
        return

    if step == "add_balance_amount":
        try:
            amount = int(text)
            if amount <= 0:
                raise ValueError

            target = int(state["target_id"])
            init_user_db(target)
            change_balance(target, amount)

            await event.reply(
                f"✅ {amount:,} الماس به `{target}` اضافه شد."
            )
            pending.pop(user_id, None)
        except ValueError:
            await event.reply("❌ مقدار نامعتبر است.")
        return

    if step == "remove_balance_user":
        try:
            target = int(text)
            init_user_db(target)
            state["target_id"] = target
            state["step"] = "remove_balance_amount"
            await event.reply("💎 مقدار الماس را وارد کنید:")
        except ValueError:
            await event.reply("❌ آیدی نامعتبر است.")
        return

    if step == "remove_balance_amount":
        try:
            amount = int(text)
            if amount <= 0:
                raise ValueError

            target = int(state["target_id"])
            init_user_db(target)
            balance = get_balance(target)
            if amount > balance:
                await event.reply(
                    f"❌ موجودی کاربر کافی نیست.\n"
                    f"💎 موجودی فعلی: {_fmt_diamonds(balance)}"
                )
                return

            change_balance(target, -amount)

            await event.reply(
                f"✅ {amount:,} الماس از `{target}` کسر شد."
            )
            pending.pop(user_id, None)
        except ValueError:
            await event.reply("❌ مقدار نامعتبر است.")
        return

    if step == "ban_user":
        try:
            target = int(text)
            set_banned(target, True)
            await event.reply(f"🚫 کاربر `{target}` مسدود شد.")
            pending.pop(user_id, None)
        except ValueError:
            await event.reply("❌ آیدی نامعتبر است.")


# ============================================================
# GROUP COMMANDS
# ============================================================

async def group_commands(event):
    if not (event.is_group or event.is_channel):
        return

    # BOT game/balance features are strictly limited to the official group.
    if not await is_official_group_event(event):
        return

    text = (event.raw_text or "").strip()
    user_id = event.sender_id

    if not user_id:
        return

    if not is_bot_enabled():
        # During updates the official group remains responsive only with the
        # maintenance message; SELF functionality is unaffected.
        if text == "بازی" or text.startswith("بازی ") or text == "موجودی" or text.startswith("انتقال "):
            await event.reply(BOT_UPDATE_TEXT)
        return

    if text == "موجودی":
        # Always show the balance of the person who typed "موجودی".
        # Replying to another user must never change the target.
        target = user_id
        balance = get_balance(target)
        try:
            target_entity = await bot.get_entity(int(target))
            username = getattr(target_entity, "username", None)
        except Exception:
            username = None
        identity = f"@{username}" if username else str(int(target))
        buttons = [[btn(f"💎 {_fmt_diamonds(balance)}", f"balance_{target}".encode(), "primary")]]
        await event.reply(
            f"🎖️ **موجودی الماس**\n\n"
            f"👤 آیدی: **{identity}**",
            buttons=buttons
        )
        return

    game = re.fullmatch(r"بازی\s+(\d+)", text)
    if game:
        amount = int(game.group(1))

        if amount < MIN_GAME:
            await event.reply(
                f"❌ حداقل مبلغ بازی {MIN_GAME:,} الماس است."
            )
            return

        balance = get_balance(user_id)
        if balance < amount:
            await event.reply(
                f"❌ موجودی کافی نیست.\n"
                f"💎 موجودی: {_fmt_diamonds(balance)}"
            )
            return

        change_balance(user_id, -amount)

        total = amount * 2
        tax = max(1, int(total * GAME_TAX))
        prize = total - tax

        text_game = (
            "<b>💎 بازی\n"
            f" ‌{_fmt_diamonds(amount)}\n\n"
            "🎉 جایزه برنده:\n"
            f"{_fmt_diamonds(prize)} 💎\n"
            "💰 مالیات:\n"
            f"{_fmt_diamonds(tax)} 💎\n\n"
            "𝗛𝘂𝘀𝘁𝗲𝗥𝗜𝗫 𝗗𝗶𝗺𝗼𝗻𝗱 𝗦𝗲𝗹𝗳\n\n"
            "💎 💎 💎\n"
            "برای شروع بازی، نفر دوم روی پیوستن بزند.</b>"
        )

        buttons = [
            [
                btn(
                    "پیوستن",
                    f"game_join_{amount}_{user_id}".encode(), "success"
                ),
                btn(
                    "لغو",
                    f"game_cancel_{amount}_{user_id}".encode(), "danger"
                ),
            ]
        ]

        msg = await event.reply(text_game, parse_mode="html", buttons=buttons)

        key = (event.chat_id, msg.id)
        task = asyncio.create_task(
            game_timeout(event.chat_id, msg.id, user_id, amount)
        )
        active_games[key] = {
            "organizer": user_id,
            "amount": amount,
            "task": task
        }
        return

    transfer = re.fullmatch(r"انتقال\s+(\d+)", text)
    if transfer:
        amount = int(transfer.group(1))

        if amount < MIN_TRANSFER:
            await event.reply(
                f"❌ حداقل انتقال {MIN_TRANSFER:,} الماس است."
            )
            return

        if not event.is_reply:
            await event.reply(
                "❌ روی پیام کاربر ریپلای کنید و سپس دستور «انتقال 500» را بفرستید."
            )
            return

        reply = await event.get_reply_message()
        if not reply or not reply.sender_id:
            await event.reply("❌ گیرنده پیدا نشد.")
            return

        receiver = reply.sender_id

        if receiver == user_id:
            await event.reply("❌ نمی‌توانید به خودتان انتقال دهید.")
            return

        tax = max(1, int(amount * TRANSFER_TAX))
        total = amount + tax

        balance = get_balance(user_id)
        if balance < total:
            await event.reply(
                f"❌ موجودی کافی نیست.\n\n"
                f"💎 موجودی: {_fmt_diamonds(balance)}\n"
                f"💎 مبلغ انتقال: {amount:,}\n"
                f"🧾 مالیات: {tax:,}\n"
                f"📉 کسر کل: {total:,}"
            )
            return

        # Atomic enough for per-user SQLite databases.
        change_balance(user_id, -total)
        init_user_db(receiver)
        change_balance(receiver, amount)

        await event.reply(
            "✅ **انتقال انجام شد.**\n\n"
            f"👤 فرستنده: `{user_id}`\n"
            f"👥 گیرنده: `{receiver}`\n"
            f"💎 مبلغ خالص: {amount:,}\n"
            f"🧾 مالیات: {tax:,}\n"
            f"📉 کسر کل: {total:,}"
        )
        return


async def game_timeout(chat_id, message_id, organizer_id, amount):
    await asyncio.sleep(GAME_TIMEOUT)

    key = (chat_id, message_id)
    game = active_games.pop(key, None)

    if not game:
        return

    change_balance(organizer_id, amount)

    with contextlib.suppress(Exception):
        await bot.delete_messages(chat_id, message_id)

    with contextlib.suppress(Exception):
        await bot.send_message(
            organizer_id,
            f"❌ نبرد به دلیل عدم حضور حریف لغو شد.\n"
            f"💎 {amount:,} الماس به حساب شما برگشت."
        )


# ============================================================
# CALLBACKS
# ============================================================

@bot.on(events.CallbackQuery)
async def callbacks(event):
    data = event.data.decode("utf-8", errors="ignore")
    user_id = event.sender_id

    # Admin can always enter the management panel and switch update mode.
    if data == "bot_toggle":
        if user_id not in ADMINS:
            await safe_answer(event, "❌ دسترسی ندارید.", True)
            return
        new_state = not is_bot_enabled()
        set_bot_enabled(new_state)
        if not new_state:
            await cancel_all_active_games_for_update()
        status = "روشن ✅" if new_state else "خاموش ❌"
        await safe_answer(event, f"ربات {status}")
        buttons = [
            [btn("➕ اضافه کردن الماس", b"add_balance", "success"), btn("➖ کاهش الماس", b"remove_balance", "danger")],
            [btn("💾 Backups", b"backups", "primary")],
            [btn("🔓 رفع مسدودی", b"unban_user", "success"), btn("📢 جوین اجباری", b"force_join", "primary")],
            [btn("🚫 مسدود کردن کاربر", b"ban_user", "danger"), btn("📊 آمار کاربران", b"admin_stats", "primary")],
            [btn("🟢 روشن کردن بات" if not new_state else "🔴 خاموش کردن بات", b"bot_toggle", "success" if not new_state else "danger")],
            [btn("🔙 برگشت", b"back", "danger")],
        ]
        await edit_or_send(event, "🛠 **مدیریت**\n\n" + f"🤖 وضعیت بات: **{status}**\n\nیک گزینه را انتخاب کنید:", buttons)
        return

    # Game/balance callback buttons are valid only inside the official group.
    if data.startswith(("game_join_", "game_cancel_", "game_noop_", "balance_")):
        if not await is_official_group_event(event):
            await safe_answer(event, "❌ این قابلیت فقط در گپ رسمی ربات فعال است.", True)
            return
        if not is_bot_enabled():
            await safe_answer(event, BOT_UPDATE_TEXT, True)
            return

    if data == "fj_check":
        # Verify FIRST.  Never delete/skip the force-join gate while any
        # required channel is still missing.
        if not await ensure_force_join(user_id, event):
            await safe_answer(event, "❌ هنوز در همه کانال‌ها عضو نشده‌اید.", True)
            return

        await safe_answer(event, "✅ عضویت تأیید شد.")
        with contextlib.suppress(Exception):
            await event.delete()
        if not has_registered_phone(user_id):
            await send_phone_request(user_id)
        else:
            await send_main(user_id, user_id)
        return

    if data.startswith("sp:"):
        await handle_self_panel_callback(event)
        return

    if data.startswith("game_noop_"):
        await safe_answer(event)
        return

    if not has_registered_phone(user_id):
        await safe_answer(event, "📱 ابتدا شماره موبایل ایران خود را ثبت کنید.", True)
        with contextlib.suppress(Exception):
            await send_phone_request(user_id)
        return

    if not await ensure_force_join(user_id, event):
        return

    if data == "buy_self":
        await begin_self_login(user_id, event)
        return

    if data == "user_account":
        if is_banned(user_id):
            await safe_answer(event, "🚫 شما مسدود هستید.", True)
            return

        balance = get_balance(user_id)
        session = get_active_session(user_id)

        if session:
            elapsed = max(0, int(time.time()) - int(session[2]))
            days = elapsed // 86400
            hours = (elapsed % 86400) // 3600
            self_status = "فعال ✅"
            duration = f"{days} روز و {hours} ساعت"
        else:
            self_status = "غیرفعال ❌"
            duration = "—"

        balance_value_toman = balance * DIAMOND_PRICE_TOMAN
        text = (
            "👤 **حساب کاربری**\n\n"
            f"🆔 آیدی: `{user_id}`\n"
            f"💎 موجودی: `{_fmt_diamonds(balance)}` الماس\n"
            f"💰 ارزش موجودی: `{balance_value_toman:,}` تومان\n"
            f"🔐 وضعیت سلف: `{self_status}`\n"
            f"⏱ مدت فعالیت: `{duration}`"
        )

        buttons = [
            [btn("💳 خرید موجودی", b"buy_balance", "success")],
            [btn("🔙 برگشت", b"back", "primary")]
        ]
        await edit_or_send(event, text, buttons)
        await safe_answer(event)
        return

    if data == "manage_self":
        await show_manage_self(event)
        return

    if data == "refresh_self":
        session = get_active_session(user_id)
        if not session:
            await safe_answer(event, "❌ سلف فعالی پیدا نشد.", True)
            return
        try:
            await safe_answer(event, "⏳ در حال بروزرسانی سلف...")
            await start_self_worker(user_id, session[0], int(session[1]))
            await event.edit(
                "✅ **سلف با موفقیت بروزرسانی شد.**\n\n"
                "🔐 Session قبلی حفظ شد و Worker دوباره اجرا شد.",
                buttons=[[btn("🔙 برگشت", b"back", "primary")]]
            )
        except Exception as exc:
            print(f"[SELF {user_id}] refresh failed: {exc}")
            await safe_answer(event, "❌ بروزرسانی سلف ناموفق بود.", True)
        return

    if data == "toggle_time":
        current = get_setting(user_id, "time_name", "on")
        new = "off" if current == "on" else "on"
        set_setting(user_id, "time_name", new)

        await safe_answer(
            event,
            f"🕐 ساعت کنار نام: {'فعال ✅' if new == 'on' else 'غیرفعال ❌'}"
        )
        await show_manage_self(event)
        return

    if data == "disable_self":
        await stop_self_worker(user_id)
        await edit_or_send(
            event,
            "✅ سلف با موفقیت غیرفعال شد.",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data == "back":
        await event.delete()
        await send_main(user_id, user_id)
        return

    if data == "referral_system":
        init_user_db(user_id)
        with connect_db(user_id) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=?",
                (user_id,)
            ).fetchone()[0]

        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={user_id}"

        text = (
            "👥 **زیرمجموعه‌گیری**\n\n"
            f"🎁 پاداش هر دعوت: {REFERRAL_REWARD:,} الماس\n"
            f"📊 تعداد دعوت‌ها: {count:,}\n\n"
            f"🔗 لینک دعوت:\n`{link}`"
        )
        await edit_or_send(
            event,
            text,
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data == "buy_balance":
        purchase_state[user_id] = "0"
        await show_buy_balance(event)
        return

    if data.startswith("num_"):
        value = data[4:]
        current = str(purchase_state.get(user_id, "0"))

        if value == "00":
            if current != "0":
                current += "00"
        else:
            current = value if current == "0" else current + value

        if len(current) > 10:
            await safe_answer(event, "❌ مقدار بیش از حد بزرگ است.", True)
            return

        current = current.lstrip("0") or "0"
        purchase_state[user_id] = current
        await show_buy_balance(event)
        return

    if data == "clear_amount":
        purchase_state[user_id] = "0"
        await show_buy_balance(event)
        return

    if data == "confirm_amount":
        try:
            diamonds = int(purchase_state.get(user_id, "0"))
        except ValueError:
            diamonds = 0

        if diamonds < MIN_DIAMOND_PURCHASE:
            await safe_answer(event, f"❌ حداقل خرید {MIN_DIAMOND_PURCHASE:,} الماس است.", True)
            return

        amount = diamonds * DIAMOND_PRICE_TOMAN

        purchase_state[user_id] = {
            "diamonds": diamonds,
            "amount": amount
        }

        text = (
            "💳 **فاکتور خرید**\n\n"
            f"💎 تعداد الماس: {diamonds:,}\n"
            f"💰 مبلغ: {amount:,} تومان\n\n"
            f"💳 شماره کارت:\n`{CARD_NUMBER}`\n"
            f"👤 صاحب کارت: {CARD_HOLDER}\n\n"
            "پس از پرداخت، روی دکمه پرداخت بزنید و عکس رسید را ارسال کنید."
        )

        buttons = [
            [btn("📸 پرداخت و ارسال رسید", b"proceed_payment", "success")],
            [btn("❌ لغو", b"cancel_payment", "danger")]
        ]
        await edit_or_send(event, text, buttons)
        return

    if data == "proceed_payment":
        state = purchase_state.get(user_id)
        if not isinstance(state, dict):
            await safe_answer(event, "❌ فاکتور پیدا نشد.", True)
            return

        purchase_state.pop(user_id, None)
        pending[user_id] = {
            "step": "receipt",
            "diamonds": state["diamonds"],
            "amount": state["amount"]
        }

        await edit_or_send(
            event,
            f"📸 مبلغ `{state['amount']:,}` تومان را پرداخت کنید و سپس عکس رسید را ارسال کنید.\n\n"
            f"💎 معادل: {state['diamonds']:,} الماس\n"
            f"💳 کارت: `{CARD_NUMBER}`\n"
            f"👤 صاحب کارت: {CARD_HOLDER}"
        )
        return

    if data == "cancel_payment":
        purchase_state.pop(user_id, None)
        await edit_or_send(
            event,
            "❌ خرید لغو شد.",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data.startswith("balance_"):
        target = int(data.split("_", 1)[1])
        balance = get_balance(target)
        await safe_answer(event, f"💎 موجودی: {_fmt_diamonds(balance)} الماس")
        return

    # --------------------------------------------------------
    # GAME JOIN
    # --------------------------------------------------------
    if data.startswith("game_join_"):
        parts = data.split("_")
        if len(parts) != 4:
            await safe_answer(event, "❌ اطلاعات بازی نامعتبر است.", True)
            return

        amount = int(parts[2])
        organizer = int(parts[3])
        joiner = user_id
        key = (event.chat_id, event.message_id)

        if amount < MIN_GAME:
            await safe_answer(
                event,
                f"❌ حداقل مبلغ بازی {MIN_GAME:,} الماس است.",
                True
            )
            return

        if joiner == organizer:
            await safe_answer(event, "❌ برگزارکننده نمی‌تواند وارد بازی خودش شود.", True)
            return

        game = active_games.get(key)
        if not game:
            await safe_answer(event, "❌ این بازی منقضی شده است.", True)
            return

        # Lock before the reveal delay so the same game cannot be joined twice.
        if game.get("resolving"):
            await safe_answer(event, "⏳ در حال انتخاب برنده ...", True)
            return
        game["resolving"] = True

        if get_balance(joiner) < amount:
            game.pop("resolving", None)
            await safe_answer(event, "❌ موجودی کافی ندارید.", True)
            return

        change_balance(joiner, -amount)

        total = amount * 2
        tax = max(1, int(total * GAME_TAX))
        prize = total - tax

        # Exactly 50/50: one unbiased random bit chooses either player.
        winner = organizer if secrets.randbelow(2) == 0 else joiner
        loser = joiner if winner == organizer else organizer

        # Keep the existing game message; only replace it during the 3-second reveal.
        with contextlib.suppress(Exception):
            await event.edit("درحال انتخاب برنده ...", buttons=None)

        await asyncio.sleep(3)

        if not is_bot_enabled():
            # Update mode may have been enabled during the 3-second reveal.
            # The organizer is refunded by cancel_all_active_games_for_update();
            # refund the joiner here because this callback already deducted it.
            change_balance(joiner, amount)
            return

        change_balance(winner, prize)

        winner_balance = get_balance(winner)
        loser_balance = get_balance(loser)
        winner_name = await user_name(winner)
        loser_name = await user_name(loser)

        active_games.pop(key, None)
        game["task"].cancel()

        # Result layout matches the reference UI: label + value in two columns.
        game_result_buttons = [
            [
                btn("🎉 جایزه برنده", b"game_noop_prize", "success"),
                btn(f"💎 {prize:,}", b"game_noop_prize_value", "success"),
            ],
            [
                btn("💎 موجودی برنده", b"game_noop_winner", "primary"),
                btn(f"💎 {_fmt_diamonds(winner_balance)}", b"game_noop_winner_value", "primary"),
            ],
            [
                btn("❌ موجودی بازنده", b"game_noop_loser", "danger"),
                btn(f"💎 {_fmt_diamonds(loser_balance)}", b"game_noop_loser_value", "danger"),
            ],
        ]
        await bot.edit_message(
            event.chat_id,
            event.message_id,
            "🎉 **نتیجه بازی مشخص شد**\n\n"
            f"🎉 **برنده:** {winner_name}\n"
            f"❌ **بازنده:** {loser_name}",
            buttons=game_result_buttons,
        )

        await safe_answer(event, "✅ بازی به پایان رسید.")
        return


    # --------------------------------------------------------
    # GAME CANCEL
    # --------------------------------------------------------
    if data.startswith("game_cancel_"):
        parts = data.split("_")
        if len(parts) != 4:
            return

        amount = int(parts[2])
        organizer = int(parts[3])
        key = (event.chat_id, event.message_id)

        if user_id != organizer:
            await safe_answer(
                event,
                "❌ فقط برگزارکننده می‌تواند بازی را لغو کند.",
                True
            )
            return

        game = active_games.pop(key, None)
        if not game:
            await safe_answer(event, "❌ بازی قبلاً پایان یافته.", True)
            return

        game["task"].cancel()
        change_balance(organizer, amount)

        with contextlib.suppress(Exception):
            await bot.delete_messages(event.chat_id, event.message_id)

        await bot.send_message(
            organizer,
            f"❌ بازی لغو شد.\n💎 {amount:,} الماس برگشت داده شد."
        )
        await safe_answer(event, "✅ بازی لغو شد.")
        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------
    if data == "admin_panel":
        if user_id not in ADMINS:
            await safe_answer(event, "❌ دسترسی ندارید.", True)
            return

        buttons = [
            [btn("➕ اضافه کردن الماس", b"add_balance", "success"), btn("➖ کاهش الماس", b"remove_balance", "danger")],
            [btn("💾 Backups", b"backups", "primary")],
            [btn("🔓 رفع مسدودی", b"unban_user", "success"), btn("📢 جوین اجباری", b"force_join", "primary")],
            [btn("🚫 مسدود کردن کاربر", b"ban_user", "danger"), btn("📊 آمار کاربران", b"admin_stats", "primary")],
            [btn("🟢 روشن کردن بات" if not is_bot_enabled() else "🔴 خاموش کردن بات", b"bot_toggle", "success" if not is_bot_enabled() else "danger")],
            [btn("🔙 برگشت", b"back", "danger")],
        ]
        await edit_or_send(event, "🛠 **مدیریت**\n\nیک گزینه را انتخاب کنید:", buttons)
        return

    if data == "force_join":
        if user_id not in ADMINS:
            return
        channels = get_force_join_channels()
        rows = []
        for c in channels:
            rows.append([
                btn(f"📢 {c.get('title', c.get('username', 'کانال'))}", f"fj_info:{c.get('id')}", "primary")
            ])
        rows.append([btn("➕ افزودن کانال", b"fj_add", "success"), btn("🗑 حذف همه", b"fj_clear", "danger")])
        rows.append([btn("🔙 برگشت", b"admin_panel", "danger")])
        await edit_or_send(
            event,
            "📢 **جوین اجباری**\n\n"
            "کانال‌های فعال با رنگ بنفش نمایش داده می‌شوند.\n"
            "برای افزودن، لینک عمومی کانال مثل `@channel` یا `https://t.me/channel` را بفرست.",
            rows
        )
        return

    if data == "fj_add":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "force_join_add"}
        await edit_or_send(event, "📢 لینک عمومی را بفرست؛ برای چنل/گروه خصوصی، یک پیام از همان‌جا را فوروارد کن:", [[btn("🔙 برگشت", b"force_join", "danger")]])
        return

    if data.startswith("fj_info:"):
        if user_id not in ADMINS:
            return
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            await safe_answer(event, "❌ کانال نامعتبر است.", True)
            return
        channel = next((c for c in get_force_join_channels() if int(c.get("id", 0)) == cid), None)
        if not channel:
            await safe_answer(event, "❌ کانال پیدا نشد.", True)
            return
        title = html.escape(str(channel.get("title") or "کانال"))
        username = str(channel.get("username") or "")
        url = _channel_url(channel)
        kind = "خصوصی" if channel.get("private") else "عمومی"
        text = (
            f"📢 <b>اطلاعات جوین اجباری</b>\n\n"
            f"🏷 نام: <b>{title}</b>\n"
            f"🔐 نوع: <b>{kind}</b>\n"
            f"🆔 آیدی: <code>{int(channel.get('id', 0))}</code>\n"
            f"🔗 لینک: <code>{html.escape(url or 'ندارد')}</code>"
        )
        await event.edit(
            text,
            parse_mode="html",
            buttons=[
                [btn("🗑 حذف جوین اجباری", f"fj_remove:{cid}", "danger")],
                [btn("🔙 بازگشت", b"force_join", "primary")],
            ],
        )
        return

    if data.startswith("fj_remove:"):
        if user_id not in ADMINS:
            return
        cid = int(data.split(":", 1)[1])
        channels = [c for c in get_force_join_channels() if int(c.get("id", 0)) != cid]
        save_force_join_channels(channels)
        await safe_answer(event, "✅ کانال حذف شد.")
        await event.edit("📢 **جوین اجباری**", buttons=[
            [btn("➕ افزودن کانال", b"fj_add", "success"), btn("🗑 حذف همه", b"fj_clear", "danger")],
            [btn("🔙 برگشت", b"admin_panel", "danger")]
        ])
        return

    if data == "fj_clear":
        if user_id not in ADMINS:
            return
        save_force_join_channels([])
        await safe_answer(event, "✅ همه جوین‌های اجباری حذف شدند.")
        await edit_or_send(event, "📢 **جوین اجباری**\n\nهیچ کانالی تنظیم نشده است.", [
            [btn("➕ افزودن کانال", b"fj_add", "success")],
            [btn("🔙 برگشت", b"admin_panel", "danger")]
        ])
        return

    if data == "backups":
        if user_id not in ADMINS:
            return
        await edit_or_send(
            event,
            "💾 **Backups**\n\n"
            "بکاپ شامل database_users، موجودی کاربران، وضعیت سلف، sessionها، تنظیمات و جوین اجباری است.",
            [
                [btn("بکاپ‌گیری", b"backup_create", "success"), btn("بارگزاری بکاپ", b"backup_restore", "primary")],
                [btn("بازگشت", b"admin_panel", "danger")]
            ]
        )
        return

    if data == "backup_create":
        if user_id not in ADMINS:
            return
        await safe_answer(event, "⏳ در حال ساخت بکاپ...")
        try:
            path = await asyncio.to_thread(create_backup_sync)
            await bot.send_file(user_id, str(path), caption="💾 بکاپ کامل ربات آماده است.")
            with contextlib.suppress(Exception):
                path.unlink()
            await event.edit("✅ بکاپ کامل با موفقیت ارسال شد.", buttons=[[btn("Backups", b"backups", "danger")]])
        except Exception as exc:
            print(f"[BACKUP] create failed: {exc}")
            await event.edit("❌ ساخت بکاپ ناموفق بود.", buttons=[[btn("Backups", b"backups", "danger")]])
        return

    if data == "backup_restore":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "backup_restore"}
        await edit_or_send(
            event,
            "🟣 **بارگزاری بکاپ**\n\nفایل ZIP بکاپ را همین‌جا ارسال کن.\n"
            "قبل از بازگردانی، Workerهای سلف متوقف و بعد از اتمام دوباره بازیابی می‌شوند.",
            [[btn("لغو", b"backups", "danger")]]
        )
        return

    if data == "admin_stats":
        if user_id not in ADMINS:
            return
        count = len(list(DATA_DIR.glob("user_*.db")))
        active = len(all_active_sessions())
        total_diamonds = total_diamonds_in_circulation()
        await edit_or_send(
            event,
            f"📊 **آمار مدیریت**\n\n👥 کاربران ثبت‌شده: `{count}`\n⚙️ سلف‌های فعال: `{active}`\n📢 جوین‌های اجباری: `{len(get_force_join_channels())}`\n💎 کل الماس‌های در گردش: `{_fmt_diamonds(total_diamonds)}`",
            [[btn("🔙 مدیریت", b"admin_panel", "danger")]]
        )
        return

    if data == "add_balance":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "add_balance_user"}
        await edit_or_send(
            event,
            "➕ آیدی عددی کاربر را ارسال کنید:",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data == "remove_balance":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "remove_balance_user"}
        await edit_or_send(
            event,
            "➖ آیدی عددی کاربر را ارسال کنید:",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data == "ban_user":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "ban_user"}
        await edit_or_send(
            event,
            "🚫 آیدی عددی کاربر را ارسال کنید:",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data == "unban_user":
        if user_id not in ADMINS:
            return
        pending[user_id] = {"step": "unban_user"}
        await edit_or_send(
            event,
            "🔓 آیدی عددی کاربر را ارسال کنید:",
            [[btn("🔙 برگشت", b"back", "primary")]]
        )
        return

    if data.startswith("pay_confirm_"):
        if user_id not in ADMINS:
            return

        parts = data.split("_")
        target = int(parts[2])
        diamonds = int(parts[3])

        init_user_db(target)
        change_balance(target, diamonds)

        purchase_state.pop(target, None)

        with contextlib.suppress(Exception):
            await bot.send_message(
                target,
                f"✅ پرداخت شما تأیید شد.\n"
                f"💎 {diamonds:,} الماس به حساب شما اضافه شد.\n\n"
                "🔄 منوی اصلی شما خودکار بروزرسانی شد."
            )
            # Behave like /start after approval so the user immediately gets
            # the normal main buttons without having to send /start manually.
            await send_main(target, target)

        await safe_answer(event, "✅ پرداخت تأیید شد و منوی کاربر بروزرسانی شد.")
        with contextlib.suppress(Exception):
            await event.edit(
                f"✅ پرداخت کاربر `{target}` تأیید شد.\n"
                f"💎 {diamonds:,} الماس اضافه شد."
            )
        return

    if data.startswith("pay_reject_"):
        if user_id not in ADMINS:
            return

        target = int(data.split("_")[2])

        with contextlib.suppress(Exception):
            await bot.send_message(
                target,
                "❌ پرداخت شما رد شد.\nلطفاً با پشتیبانی تماس بگیرید."
            )

        await safe_answer(event, "❌ پرداخت رد شد.")
        with contextlib.suppress(Exception):
            await event.edit(f"❌ پرداخت کاربر `{target}` رد شد.")
        return


async def show_manage_self(event):
    user_id = event.sender_id
    session = get_active_session(user_id)
    balance = get_balance(user_id)

    if not session:
        text = (
            "⚙️ **مدیریت سلف**\n\n"
            "🔐 وضعیت: غیرفعال ❌\n"
            f"💎 موجودی: {_fmt_diamonds(balance)}\n\n"
            f"حداقل موجودی فعال‌سازی: {MIN_SELF_BALANCE:,} الماس\n"
            f"هزینه استفاده: {SELF_HOURLY_COST:g} الماس در ساعت"
        )
        buttons = [
            [btn("💎 فعال‌سازی سلف", b"buy_self", "success")],
            [btn("🔙 برگشت", b"back", "primary")]
        ]
    else:
        elapsed = max(0, int(time.time()) - int(session[2]))
        days = elapsed // 86400
        hours = (elapsed % 86400) // 3600
        status = "فعال ✅"
        clock = get_setting(user_id, "time_name", "on") == "on"

        text = (
            "⚙️ **مدیریت سلف**\n\n"
            f"🔐 وضعیت: {status}\n"
            f"⏱ مدت فعالیت: {days} روز و {hours} ساعت\n"
            f"💎 موجودی: {_fmt_diamonds(balance)}\n"
            f"🕐 ساعت کنار نام: {'فعال ✅' if clock else 'غیرفعال ❌'}\n"
        )
        buttons = [
            [btn("🔄 بروزرسانی / ری‌استارت", b"refresh_self", "primary")],
            [btn("🔓 خاموش کردن سلف", b"disable_self", "danger")],
            [btn("🔙 برگشت", b"back", "primary")]
        ]

    await edit_or_send(event, text, buttons)


async def show_buy_balance(event):
    user_id = event.sender_id
    value = str(purchase_state.get(user_id, "0"))

    try:
        diamonds = int(value)
    except ValueError:
        diamonds = 0

    amount = diamonds * DIAMOND_PRICE_TOMAN

    buttons = [
        [
            btn("1", b"num_1", "primary"),
            btn("2", b"num_2", "primary"),
            btn("3", b"num_3", "primary"),
        ],
        [
            btn("4", b"num_4", "primary"),
            btn("5", b"num_5", "primary"),
            btn("6", b"num_6", "primary"),
        ],
        [
            btn("7", b"num_7", "primary"),
            btn("8", b"num_8", "primary"),
            btn("9", b"num_9", "primary"),
        ],
        [
            btn("0", b"num_0", "primary"),
            btn("00", b"num_00", "primary"),
        ],
        [
            btn("✅ تأیید", b"confirm_amount", "success"),
            btn("🗑 حذف", b"clear_amount", "danger"),
        ],
        [btn("🔙 برگشت", b"back", "primary")]
    ]

    text = (
        "💳 **خرید موجودی**\n\n"
        f"💎 تعداد الماس: {diamonds:,}\n"
        f"💰 مبلغ: {amount:,} تومان\n\n"
        f"📌 حداقل خرید: {MIN_DIAMOND_PURCHASE:,} الماس\n"
        f"💵 {MIN_DIAMOND_PURCHASE:,} الماس = {MIN_DIAMOND_PURCHASE * DIAMOND_PRICE_TOMAN:,} تومان\n\n"
        "تعداد الماس را انتخاب کنید:"
    )

    await edit_or_send(event, text, buttons)


# ============================================================
# ADMIN COMMANDS
# ============================================================

@bot.on(events.NewMessage(pattern=r"^/panel$"))
async def panel(event):
    if not event.is_private:
        return
    if event.sender_id not in ADMINS:
        await event.reply("❌ شما دسترسی ندارید.")
        return
    buttons = [[btn("🛠 پنل مدیریت", b"admin_panel", "primary")]]
    await event.reply("🛠 برای ورود به پنل مدیریت:", buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/sioh\s+(\d+)\s+(\d+)$"))
async def sioh(event):
    user_id = event.sender_id
    amount = int(event.pattern_match.group(1))
    target = int(event.pattern_match.group(2))

    if amount <= 0:
        await event.reply("❌ مقدار باید بیشتر از صفر باشد.")
        return

    if user_id == target:
        await event.reply("❌ نمی‌توانید به خودتان انتقال دهید.")
        return

    if get_balance(user_id) < amount:
        await event.reply("❌ موجودی کافی نیست.")
        return

    init_user_db(target)
    change_balance(user_id, -amount)
    change_balance(target, amount)

    await event.reply(
        f"✅ انتقال انجام شد.\n"
        f"💎 {amount:,} الماس به `{target}` منتقل شد."
    )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def restore_workers():
    restored = 0

    for user_id, session_string, sub_type in all_active_sessions():
        try:
            if get_balance(user_id) <= 0:
                deactivate_session(user_id)
                continue

            await start_self_worker(
                user_id,
                session_string,
                sub_type
            )
            restored += 1
        except Exception as exc:
            print(f"[RESTORE {user_id}] {exc}")

    print(f"[RESTORE] {restored} self worker(s) restored.")


async def main():
    for admin in ADMINS:
        init_user_db(admin)

    print("=" * 55)
    print("🤖 Diamond Bot is starting...")
    print(f"👤 Admins: {ADMINS}")
    print("📁 Database:", DATA_DIR)
    print("=" * 55)

    await bot.start(bot_token=BOT_TOKEN)

    me = await bot.get_me()
    print(f"✅ Bot: @{me.username if me else 'unknown'}")
    await resolve_official_group_id()

    await restore_workers()

    print("🚀 Bot is running.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")

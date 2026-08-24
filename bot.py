
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

from telethon import TelegramClient, events, Button, functions, types
from telethon.tl.functions.messages import (
    SetTypingRequest,
    SendReactionRequest,
    TranslateTextRequest,
    GetInlineBotResultsRequest,
    SendInlineBotResultRequest,
)
from telethon.tl.types import SendMessageTypingAction, ReactionEmoji, TextWithEntities
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
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

# Optional high-accuracy cloud speech-to-text. Set OPENAI_API_KEY in the server environment.
# If it is not configured, the bot falls back to local faster-whisper.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

# Free providers for currency and local media features
# Currency uses CoinMarketCap's current keyless public endpoint first and
# CoinGecko's public endpoint as a fallback. No API key is required.
CMC_PUBLIC_BASE_URL = "https://pro-api.coinmarketcap.com/public-api"
COINGECKO_PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
CRYPTO_PROVIDER_TIMEOUT = 12

TRANSFER_TAX = 0.10
GAME_TAX = 0.05
GAME_TIMEOUT = 300
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
_channel_save_tasks = {}
_self_reply_cache = set()
_inline_bot_cache = {}
_cleanup_tasks = {}
_cleanup_panel_messages = {}
# Independent state for media conversions; never shares channel-save state.
media_convert_state = {}
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
    try:
        return Button.inline(text, data, style=chosen)
    except Exception:
        return Button.inline(text, data)


# ============================================================
# UI
# ============================================================

def main_buttons(user_id: int):
    rows = [
        [btn("💎 خرید سلف", b"buy_self", "success")],
        [
            btn("👤 حساب کاربری", b"user_account", "primary"),
            btn("⚙️ مدیریت سلف", b"manage_self", "primary"),
        ],
        [btn("👥 زیرمجموعه‌گیری", b"referral_system", "success")],
    ]
    if user_id in ADMINS:
        rows.append([btn("🛠 پنل مدیریت", b"admin_panel", "primary")])
    return rows


async def send_main(target, user_id: int, text="به سلف‌ساز خوش آمدید"):
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
}
SELF_FONT_ALIASES = {
    "عادی":"normal","بولد":"bold","دوبل":"double","سانس":"sans","سانس بولد":"sans_bold",
    "مونو":"mono","فول":"full","دایره":"circled","منفی":"negative"
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
        "clock":{"normal":"عادی","bold":"بولد","double":"دوبل","sans":"سانس","sans_bold":"سانس بولد","mono":"مونو","full":"فول","circled":"دایره","negative":"منفی"},
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
            btn(f"🔤 انگلیسی: {_font_label('english', english)}", _self_cb(uid, "engfont"), "primary"),
            btn(f"🌐 ترجمه {'روشن' if self_get(uid,'translate')=='on' else 'خاموش'}", _self_cb(uid, "translate"), toggle_style("translate")),
        ],
        [
            btn(f"❤️ ریاکشن {'فعال' if reaction_on else 'خاموش'}", _self_cb(uid, "reaction"), "success" if reaction_on else "danger"),
            btn(f"👁 سین {'روشن' if self_get(uid,'auto_read')=='on' else 'خاموش'}", _self_cb(uid, "read"), toggle_style("auto_read")),
        ],
        [
            btn(f"⌨️ تایپینگ {'روشن' if self_get(uid,'typing')=='on' else 'خاموش'}", _self_cb(uid, "typing"), toggle_style("typing")),
            btn(f"🎮 بازی {'روشن' if self_get(uid,'game_mode')=='on' else 'خاموش'}", _self_cb(uid, "game"), toggle_style("game_mode")),
        ],
        [
            btn(f"🤖 تبچی {'روشن' if self_get(uid,'auto_reply')=='on' else 'خاموش'}", _self_cb(uid, "autoreply"), toggle_style("auto_reply")),
            btn("💾 ذخیره چنل", _self_cb(uid, "channel_save"), "primary"),
        ],
        [
            btn("💱 نرخ ارز", _self_cb(uid, "currency"), "success"),
            btn("🎨 لوگوساز", _self_cb(uid, "logo"), "primary"),
        ],
        [
            btn("🧹 پاکسازی", _self_cb(uid, "cleanup"), "danger"),
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


def self_guide_text(page=1):
    pages = [
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۱ از ۸</i>\n\n"
            "<blockquote>🧭 <b>شروع سریع</b>\n"
            "این راهنما قابلیت‌ها را کوتاه و مرحله‌به‌مرحله توضیح می‌دهد.</blockquote>\n\n"
            "🕐 <b>ساعت روی نام</b>\n"
            "فعال/غیرفعال‌کردن ساعت ایران روی نام پروفایل.\n\n"
            "🔤 <b>فونت ساعت</b>\n"
            "ظاهر اعداد ساعت را از پنل تغییر بده.\n\n"
            "🅱 <b>بولد</b>\n"
            "برای پررنگ‌کردن متن‌های ارسالی.\n\n"
            "🅵 <b>فونت فارسی</b>\n"
            "برای ظاهر کشیده و متفاوت متن فارسی."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۲ از ۸</i>\n\n"
            "🌐 <b>ترجمه</b>\n"
            "<blockquote>متن‌های ارسالی را به انگلیسی ترجمه می‌کند.</blockquote>\n\n"
            "👁 <b>سین</b>\n"
            "پیام‌های دریافتی را به‌عنوان خوانده‌شده علامت می‌زند.\n\n"
            "⌨️ <b>تایپینگ</b>\n"
            "وضعیت تایپ‌کردن را در گفتگو نمایش می‌دهد.\n\n"
            "🎮 <b>حالت بازی</b>\n"
            "به‌جای تایپینگ، وضعیت بازی را نمایش می‌دهد."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۳ از ۸</i>\n\n"
            "🤖 <b>تبچی</b>\n"
            "پاسخ خودکار را روشن یا خاموش کن.\n\n"
            "<code>تبچی متن سلام، فعلاً در دسترس نیستم.</code>\n"
            "متن پاسخ خودکار را تغییر می‌دهد.\n\n"
            "❤️ <b>ریاکشن خودکار</b>\n"
            "<blockquote>روی پیام کاربر ریپلای کن و ریاکشن دلخواه را تنظیم کن.</blockquote>\n\n"
            "<u>حذف ریاکشن</u> یا <u>ریاکشن خاموش</u>\n"
            "تنظیم ریاکشن آن کاربر را پاک می‌کند."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۴ از ۸</i>\n\n"
            "🎙️ <b>ویس → متن</b>\n\n"
            "<code>متن</code>\n"
            "روی ویس یا فایل صوتی ریپلای کن.\n\n"
            "<code>متن + ریپلی</code>\n"
            "یا\n"
            "<code>متن + ریپلای</code>\n"
            "نتیجه را روی همان ویس ریپلای می‌کند.\n\n"
            "✨ <i>برای فارسی، در صورت تنظیم API، موتور دقیق ابری استفاده می‌شود.</i>"
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۵ از ۸</i>\n\n"
            "🎵 <b>تبدیل رسانه</b>\n\n"
            "<code>ویس به mp3</code>\n"
            "ویس را به MP3 تبدیل می‌کند.\n\n"
            "<code>mp3 به ویس</code>\n"
            "MP3 را به ویس تلگرام تبدیل می‌کند.\n\n"
            "<code>ویدیو به ویس</code>\n"
            "صدای ویدیو را جدا و به‌صورت ویس ارسال می‌کند.\n\n"
            "<code>ویدیو به mp3</code>\n"
            "صدای ویدیو را به MP3 تبدیل می‌کند."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۶ از ۸</i>\n\n"
            "📦 <b>استخراج آرشیو</b>\n\n"
            "<code>unzip + ریپلی</code>\n"
            "یا\n"
            "<code>استخراج + ریپلای</code>\n\n"
            "روی ZIP/RAR ریپلای کن.\n"
            "فایل‌های استخراج‌شده یکی‌یکی ارسال می‌شوند.\n\n"
            "📊 <b>پیشرفت</b>\n"
            "<blockquote>نمایش درصد به‌صورت ساده و خلوت؛ بدون نوار شلوغ.</blockquote>\n\n"
            "🖼️ <b>OCR</b>\n"
            "روی تصویر ریپلای کن و <code>OCR</code> یا <code>او سی آر</code> بفرست."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۷ از ۸</i>\n\n"
            "💾 <b>ذخیره چنل</b>\n"
            "از پنل، «ذخیره چنل» را انتخاب کن.\n\n"
            "📢 فقط کانال‌های خصوصیِ عضو اکانت نمایش داده می‌شوند.\n\n"
            "🎯 <b>تعداد</b>\n"
            "از ۱ تا ۱۰۰۰ مورد را می‌توانی انتخاب کنی.\n\n"
            "🗑️ <b>آرشیو حذف‌شده‌ها</b>\n"
            "پیام‌های حذف‌شده در پیوی به‌صورت خودکار آرشیو می‌شوند."
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۸ از ۹</i>\n\n"
            "💱 <b>نرخ ارز</b>\n"
            "<code>قیمت BTC</code>\n"
            "<code>قیمت ETH</code>\n"
            "<code>قیمت SOL</code>\n"
            "<code>قیمت USDT</code>\n"
            "قیمت لحظه‌ای از سرویس عمومی بدون API Key دریافت می‌شود.\n\n"
            "🎨 <b>لوگوساز</b>\n"
            "<code>لوگو 12 HusteRIX</code>\n"
            "۱۲ قالب داخلی و رایگان.\n\n"
        ),
        (
            "📚 <b>راهنمای سلف</b>\n"
            "<i>صفحه ۹ از ۹</i>\n\n"
            "🧹 <b>پاکسازی</b>\n"
            "چت‌ها، گپ‌ها، کانال‌ها، مخاطبین و ربات‌ها را جداگانه پاکسازی کن.\n\n"
            "🔒 <b>قفل چت</b>\n"
            "روی پیام کاربر ریپلای کن و <code>قفل چت</code> بفرست.\n\n"
            "💎 <b>انتقال الماس</b>\n"
            "<code>انتقال ۵۰۰</code>\n\n"
            "🎲 <b>تاس</b>\n"
            "<code>تاس ۱</code> تا <code>تاس ۶</code>\n\n"
            "<blockquote>✨ برای برگشت به پنل، دکمه «بازگشت» را بزن.</blockquote>"
        ),
    ]
    page = max(1, min(int(page), len(pages)))
    return pages[page - 1]

def self_guide_buttons(uid, page=1):
    total_pages = 9
    page = max(1, min(int(page), total_pages))
    nav = []
    if page > 1:
        nav.append(btn("◀️ قبلی", _self_cb(uid, f"guide_page:{page-1}"), "danger"))
    if page < total_pages:
        nav.append(btn("بعدی ▶️", _self_cb(uid, f"guide_page:{page+1}"), "success"))
    rows = [nav] if nav else []
    rows.append([btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")])
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


async def send_self_panel(chat_id: int, uid: int, reply_to=None):
    """Inline panels are bot messages; Telegram does not deliver callback queries to user accounts."""
    return await bot.send_message(
        chat_id,
        self_panel_text(uid),
        parse_mode="html",
        buttons=self_panel_buttons(uid),
        reply_to=reply_to,
    )


async def send_self_guide(chat_id: int, uid: int, reply_to=None, page=1):
    return await bot.send_message(
        chat_id,
        self_guide_text(page),
        parse_mode="html",
        buttons=self_guide_buttons(uid, page),
        reply_to=reply_to,
    )



def self_chat_lock_targets(uid):
    try:
        raw = json.loads(self_get(uid, "chat_lock_targets", "[]"))
        return {int(x) for x in raw}
    except Exception:
        return set()


def self_save_chat_lock_targets(uid, targets):
    self_set(uid, "chat_lock_targets", json.dumps(sorted(int(x) for x in targets)))


async def _private_channel_entries(client, limit=None):
    """Return ONLY joined private broadcast channels.

    A Telegram channel is public when it has a public username.
    Megagroups/supergroups are deliberately excluded even though Telethon
    exposes them through the channel dialog type as well.
    """
    items = []
    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if not isinstance(entity, types.Channel):
            continue
        if not getattr(dialog, "is_channel", False):
            continue
        # Only Telegram broadcast channels are eligible.
        # Supergroups/megagroups must never appear here.
        if getattr(entity, "megagroup", False):
            continue
        if not getattr(entity, "broadcast", False):
            continue
        # A channel with a public username is public. Private channels do not
        # have a public username, so reject every channel that exposes one.
        if bool(getattr(entity, "username", None)):
            continue
        # Broadcast + no username => private channel.
        title = (getattr(entity, "title", None) or getattr(dialog, "name", None) or "بدون نام").strip()
        display_id = getattr(dialog, "id", None)
        if display_id is None:
            display_id = -1000000000000 - int(entity.id)
        items.append((int(display_id), title, entity))
    items.sort(key=lambda x: x[1].casefold())
    return items[:limit] if limit else items


def _private_channel_button_id(entity):
    return int(getattr(entity, "id", 0))


def _private_channel_access_hash(entity):
    value = getattr(entity, "access_hash", None)
    return int(value) if value is not None else None


def self_channel_save_state(uid):
    try:
        raw = self_get(uid, "channel_save_state", "{}")
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def self_set_channel_save_state(uid, state):
    self_set(uid, "channel_save_state", json.dumps(state or {}, ensure_ascii=False))


def self_clear_channel_save_state(uid):
    self_set_channel_save_state(uid, {})


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


async def _cleanup_run(uid, target, panel_chat_id=None, panel_message_id=None):
    client = self_clients.get(uid)
    if not client:
        self_set(uid, "cleanup_progress", "❌ سلف فعال نیست")
        return

    last_panel_update = 0.0

    async def progress(text, force=False):
        nonlocal last_panel_update
        self_set(uid, "cleanup_progress", text)
        if panel_chat_id and panel_message_id:
            now = time.monotonic()
            # Never edit the same Telegram message dozens/hundreds of times per
            # second.  State is still saved on every call; UI is throttled.
            if not force and (now - last_panel_update) < 0.75:
                return
            last_panel_update = now
            with contextlib.suppress(Exception):
                await bot.edit_message(
                    panel_chat_id, panel_message_id, self_panel_text(uid),
                    parse_mode="html", buttons=self_panel_buttons(uid)
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


async def _cleanup_account(uid, panel_chat_id=None, panel_message_id=None):
    # Backward-compatible entry point: "all" is the old full-cleanup behavior.
    await _cleanup_run(uid, "all", panel_chat_id, panel_message_id)


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

    # During an active channel-save operation, stale/queued channel callbacks
    # must not be allowed to mutate or restart the flow.
    if action.startswith("channel_") and self_channel_save_state(uid).get("step") == "processing":
        await safe_answer(event, "⏳ ذخیره مدیا در حال انجام است.", True)
        return True

    await safe_answer(event)

    if action == "close":
        # The panel is a bot-owned inline-result message.  Do not delete it:
        # edit it and remove every button, so the user gets a visible
        # confirmation instead of a dead/unchanged inline panel.
        await safe_answer(event, "پنل با موفقیت بسته شد.")
        with contextlib.suppress(Exception):
            await event.edit("✅ پنل با موفقیت بسته شد.", parse_mode="html", buttons=None)
        return True
    if action == "guide":
        try:
            await event.edit(
                self_guide_text(1),
                parse_mode="html",
                buttons=self_guide_buttons(uid, 1),
            )
        except Exception as exc:
            print(f"[SELF {uid}] guide callback failed: {exc}")
            await safe_answer(event, "❌ راهنما باز نشد؛ دوباره تلاش کن.", True)
        return True
    if action == "currency":
        await event.edit(
            "💱 <b>نرخ لحظه‌ای ارز</b>\n\n"
            "قیمت را با این دستور بگیر:\n\n"
            "<code>قیمت BTC</code>\n<code>قیمت ETH</code>\n<code>قیمت SOL</code>\n<code>قیمت USDT</code>\n\n"
            "⚡ دریافت مستقیم از سرویس عمومی بدون API Key؛ در صورت خطا fallback فعال است.",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True
    if action == "logo":
        await event.edit(
            "🎨 <b>لوگوساز</b>\n\n"
            "ساخت لوگو با ۱۲ قالب داخلی و رایگان:\n<code>لوگو 12 HusteRIX</code>",
            parse_mode="html",
            buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
        )
        return True


    if action.startswith("guide_page:"):
        try:
            page = int(action.split(":", 1)[1])
        except ValueError:
            page = 1
        page = max(1, min(page, 9))
        try:
            await event.edit(
                self_guide_text(page),
                parse_mode="html",
                buttons=self_guide_buttons(uid, page),
            )
        except Exception as exc:
            print(f"[SELF {uid}] guide page {page} callback failed: {exc}")
            await safe_answer(event, "❌ صفحه راهنما باز نشد؛ دوباره تلاش کن.", True)
        return True
    if action == "panel":
        await event.edit(self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action == "cleanup":
        await event.edit(
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
        await event.edit(
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
            await event.edit("⏳ یک پاکسازی همین الان در حال اجراست.", parse_mode="html", buttons=self_panel_buttons(uid))
            return True
        client = self_clients.get(uid)
        if not client:
            await event.edit("❌ سلف فعال نیست.", buttons=self_panel_buttons(uid))
            return True
        _cleanup_panel_messages[uid] = (event.chat_id, event.message_id)
        task = asyncio.create_task(_cleanup_run(uid, target, event.chat_id, event.message_id))
        _cleanup_tasks[uid] = task
        await event.edit("⏳ پاکسازی شروع شد…\nپیشرفت لحظه‌ای در همین پنل نمایش داده می‌شود.", parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action == "channel_save":
        client = self_clients.get(uid)
        if not client:
            await event.edit("❌ سلف فعال نیست. ابتدا سلف را فعال کن.", parse_mode="html", buttons=self_panel_buttons(uid))
            return True
        channels = await _private_channel_entries(client)
        if not channels:
            await event.edit(
                "💾 <b>ذخیره از چنل خصوصی</b>\n\n❌ هیچ چنل خصوصیِ عضوشده‌ای برای این اکانت پیدا نشد.",
                parse_mode="html",
                buttons=[[btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")]],
            )
            return True
        self_set_channel_save_state(uid, {"step": "select_channel"})
        buttons = []
        for display_id, title, entity in channels:
            buttons.append([btn(f"{display_id} | {title}", _self_cb(uid, f"channel_pick:{int(entity.id)}"), "primary")])
        buttons.append([btn("🔙 بازگشت", _self_cb(uid, "panel"), "primary")])
        await event.edit(
            "💾 <b>چنل خصوصی را انتخاب کن</b>\n\n"
            "فقط چنل‌های خصوصی که همین اکانت داخلشان عضو است نمایش داده می‌شوند.",
            parse_mode="html", buttons=buttons,
        )
        return True

    if action.startswith("channel_pick:"):
        try:
            entity_id = int(action.split(":", 1)[1])
            client = self_clients.get(uid)
            if not client:
                raise ValueError("سلف فعال نیست")

            # Resolve from the account's actual dialogs, not from an arbitrary
            # numeric ID, so a stale/forged callback cannot select another chat.
            entity = None
            async for dialog in client.iter_dialogs():
                candidate = getattr(dialog, "entity", None)
                if not isinstance(candidate, types.Channel):
                    continue
                if getattr(candidate, "id", None) != entity_id:
                    continue
                if getattr(candidate, "megagroup", False):
                    continue
                if getattr(candidate, "username", None):
                    continue
                entity = candidate
                break

            if entity is None:
                raise ValueError("چنل خصوصی معتبر پیدا نشد")

            title = getattr(entity, "title", None) or "بدون نام"
            state = {
                "step": "media",
                "channel_id": int(entity.id),
                "channel_access_hash": _private_channel_access_hash(entity),
                "channel_title": title,
            }
            self_set_channel_save_state(uid, state)
            await event.edit(
                f"📢 <b>{html.escape(title)}</b>\n\nنوع مدیا را انتخاب کن:",
                parse_mode="html",
                buttons=self_channel_media_buttons(uid),
            )
        except Exception as exc:
            print(f"[SELF {uid}] private channel pick failed: {exc}")
            with contextlib.suppress(Exception):
                await event.edit(
                    f"❌ انتخاب چنل ناموفق بود.\n<code>{html.escape(str(exc))}</code>",
                    parse_mode="html",
                    buttons=[
                        [btn("🔙 بازگشت به چنل‌ها", _self_cb(uid, "channel_save"), "primary")],
                        [btn("🏠 پنل", _self_cb(uid, "panel"), "primary")],
                    ],
                )
        return True

    if action.startswith("channel_count:"):
        state = self_channel_save_state(uid)
        if state.get("step") != "count" or not state.get("channel_id"):
            await safe_answer(event, "❌ این مرحله دیگر فعال نیست.", True)
            return True

        command = action.split(":", 1)[1]
        current = str(state.get("count_input") or "0")

        if command == "back_media":
            state.update({"step": "media"})
            state.pop("count_input", None)
            self_set_channel_save_state(uid, state)
            await event.edit(
                f"📢 <b>{html.escape(str(state.get('channel_title') or 'چنل'))}</b>\n\nنوع مدیا را انتخاب کن:",
                parse_mode="html",
                buttons=self_channel_media_buttons(uid),
            )
            return True

        if command == "clear":
            state["count_input"] = "0"
        elif command == "back":
            state["count_input"] = current[:-1] or "0"
        elif command == "confirm":
            try:
                count = int(current)
            except ValueError:
                count = 0
            if count < 1 or count > 1000:
                await safe_answer(event, "⚠️ تعداد باید بین 1 تا 1000 باشد.", True)
                return True

            # Capture the real logged-in self client now. The worker receives this
            # exact object explicitly and never performs a later self_clients lookup.
            client = self_clients.get(uid) or getattr(event, "client", None)
            if client is None:
                await safe_answer(event, "❌ سلف فعال نیست.", True)
                return True

            existing = _channel_save_tasks.get(uid)
            if existing is not None and not existing.done():
                await safe_answer(event, "⏳ یک عملیات ذخیره مدیا در حال انجام است.", True)
                return True

            operation_id = secrets.token_hex(12)
            state.update({
                "step": "processing",
                "uid": int(uid),
                "requested": int(count),
                "media": state.get("media", "all"),
                "channel_id": int(state["channel_id"]),
                "channel_access_hash": state.get("channel_access_hash"),
                "channel_title": str(state.get("channel_title") or "چنل"),
                "panel_chat_id": int(event.chat_id),
                "panel_message_id": int(event.message_id),
                "processing_started": time.time(),
                "operation_id": operation_id,
            })
            self_set_channel_save_state(uid, state)
            worker_state = dict(state)

            # Remove the numeric keyboard before any heavy Telegram work.
            try:
                await _channel_save_ui_edit(
                    worker_state,
                    "⏳ <b>در حال آماده‌سازی ذخیره...</b>",
                    buttons=None,
                )
            except Exception as exc:
                print(
                    "[CHANNEL_SAVE UI] initial processing edit failed: "
                    f"chat_id={worker_state['panel_chat_id']} "
                    f"message_id={worker_state['panel_message_id']} "
                    f"error={exc}"
                )
                if self_channel_save_state(uid).get("operation_id") == operation_id:
                    self_clear_channel_save_state(uid)
                await safe_answer(event, "❌ شروع ذخیره‌سازی ناموفق بود.", True)
                return True

            task = asyncio.create_task(
                _channel_save_worker(
                    client=client,
                    uid=uid,
                    state=worker_state,
                    count=count,
                    operation_id=operation_id,
                )
            )
            _channel_save_tasks[uid] = task
            return True
        else:
            digit = command
            if digit not in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "00"}:
                return True
            if current == "0":
                candidate = digit.lstrip("0") or "0"
            else:
                candidate = current + digit
            candidate = candidate.lstrip("0") or "0"
            if len(candidate) > 4 or int(candidate) > 1000:
                await safe_answer(event, "⚠️ حداکثر تعداد ۱۰۰۰ مورد است.", True)
                return True
            state["count_input"] = candidate

        self_set_channel_save_state(uid, state)
        await event.edit(
            _channel_count_text(state),
            parse_mode="html",
            buttons=_channel_count_buttons(uid, state.get("count_input", "0")),
        )
        return True

    if action == "channel_cancel":
        self_clear_channel_save_state(uid)
        await event.edit(self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
        return True

    if action.startswith("channel_media:"):
        media_kind = action.split(":", 1)[1]
        if media_kind not in {"photos", "videos", "music", "voice", "text", "all"}:
            return True
        state = self_channel_save_state(uid)
        if not state.get("channel_id"):
            await event.edit("❌ ابتدا چنل را از لیست انتخاب کن.", buttons=self_panel_buttons(uid))
            return True
        labels = {
            "photos": "تصویر", "videos": "ویدیو", "music": "موسیقی",
            "voice": "ویس", "text": "متن", "all": "کل مدیاها"
        }
        state.update({
            "step": "count",
            "media": media_kind,
            "count_input": "0",
            "panel_chat_id": int(event.chat_id),
            "panel_message_id": int(event.message_id),
        })
        self_set_channel_save_state(uid, state)
        await event.edit(
            _channel_count_text(state),
            parse_mode="html",
            buttons=_channel_count_buttons(uid, "0"),
        )
        return True

    if action == "lock_help":
        await event.edit(
            self_panel_text(uid) + "\n\n🔒 روی پیام کاربر در پیوی ریپلای کن و بنویس: <b>قفل چت</b>\nبرای خاموش‌کردن: <b>بازکردن قفل چت</b>",
            parse_mode="html", buttons=self_panel_buttons(uid)
        )
        return True

    if action == "block_help":
        await event.edit(
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
        await event.edit(self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
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

        await event.edit(
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
        await event.edit(
            self_panel_text(uid) + "\n\n" + self_font_preview(uid, "english"),
            parse_mode="html",
            buttons=self_panel_buttons(uid),
        )
        return True

    if action == "reaction":
        await event.edit(
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


def _openai_multipart_body(file_path: str, filename: str, language: str = "fa"):
    """Build a multipart/form-data body without requiring an extra HTTP package."""
    boundary = "----HusteRIXSpeechBoundary" + secrets.token_hex(12)
    with open(file_path, "rb") as f:
        audio = f.read()

    fields = [
        ("model", OPENAI_TRANSCRIBE_MODEL),
        ("language", language),
        ("response_format", "json"),
        ("prompt", "این فایل صوتی فارسی است. متن را دقیقاً به فارسی پیاده‌سازی کن؛ لهجه و گفتار محاوره‌ای را حفظ کن و به کردی یا زبان دیگری ترجمه نکن."),
    ]
    chunks = []
    for key, value in fields:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])

    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        audio,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _openai_transcribe_sync(path: str):
    if not OPENAI_API_KEY:
        return None

    body, content_type = _openai_multipart_body(
        path,
        Path(path).name or "voice.ogg",
        language=os.getenv("WHISPER_LANGUAGE", "fa"),
    )
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("text") or "").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"openai_http_{exc.code}: {detail[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"openai_transcription_failed: {exc}") from exc


async def _self_transcribe_reply(event, uid):
    """Voice/audio -> accurate Persian text.

    Primary path: OpenAI transcription API when OPENAI_API_KEY is configured.
    Fallback: local faster-whisper with a stronger default model.
    """
    if not event.is_reply:
        return "❌ روی ویس یا فایل صوتی ریپلای کن و «متن» را بفرست."
    replied = await event.get_reply_message()
    if not replied or _message_media_kind(replied) not in {"voice", "audio"}:
        return "❌ ویس یا فایل صوتی پیدا نشد."

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_stt_{uid}_"))
    try:
        path = await replied.download_media(file=str(tmp_dir))
        if not path:
            return "❌ دانلود ویس ناموفق بود."

        # Cloud transcription is much more reliable for Persian and does not
        # choke on normal 1–2 minute Telegram voice messages.
        if OPENAI_API_KEY:
            try:
                result = await asyncio.to_thread(_openai_transcribe_sync, str(path))
                if result:
                    return f"📝 <b>متن ویس</b>\n\n{html.escape(result)}"
            except Exception as exc:
                print(f"[SELF {uid}] OpenAI transcription failed, using local fallback: {exc}")

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            if OPENAI_API_KEY:
                return "❌ سرویس تبدیل صدا موقتاً در دسترس نیست و موتور محلی هم نصب نشده است."
            return "❌ برای دقت بالاتر، `OPENAI_API_KEY` را تنظیم کن؛ در حالت محلی هم نصب `faster-whisper` لازم است."

        model = getattr(_self_transcribe_reply, "_model", None)
        if model is None:
            model_name = os.getenv("WHISPER_MODEL", "large-v3")
            device = os.getenv("WHISPER_DEVICE", "cpu")
            compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            _self_transcribe_reply._model = model

        def run_transcription():
            segments, _ = model.transcribe(
                str(path),
                language="fa",
                task="transcribe",
                beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "8")),
                best_of=int(os.getenv("WHISPER_BEST_OF", "8")),
                temperature=0,
                vad_filter=True,
                condition_on_previous_text=True,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

        result = await asyncio.to_thread(run_transcription)
        return (
            f"📝 <b>متن ویس</b>\n\n{html.escape(result)}"
            if result
            else "❌ صدایی برای تبدیل به متن پیدا نشد."
        )
    except Exception as exc:
        print(f"[SELF {uid}] transcription failed: {exc}")
        return "❌ تبدیل ویس به متن انجام نشد؛ دوباره تلاش کن."
    finally:
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


def self_channel_media_buttons(uid):
    return [
        [btn("🖼 تصویر", _self_cb(uid, "channel_media:photos")), btn("🎬 ویدیو", _self_cb(uid, "channel_media:videos"))],
        [btn("🎵 موسیقی", _self_cb(uid, "channel_media:music")), btn("🎤 ویس", _self_cb(uid, "channel_media:voice"))],
        [btn("📝 متن", _self_cb(uid, "channel_media:text")), btn("📦 همه", _self_cb(uid, "channel_media:all"))],
        [btn("❌ لغو", _self_cb(uid, "channel_cancel"), "danger")],
    ]


def _channel_count_buttons(uid, value="0"):
    return [
        [btn("1", _self_cb(uid, "channel_count:1")), btn("2", _self_cb(uid, "channel_count:2")), btn("3", _self_cb(uid, "channel_count:3"))],
        [btn("4", _self_cb(uid, "channel_count:4")), btn("5", _self_cb(uid, "channel_count:5")), btn("6", _self_cb(uid, "channel_count:6"))],
        [btn("7", _self_cb(uid, "channel_count:7")), btn("8", _self_cb(uid, "channel_count:8")), btn("9", _self_cb(uid, "channel_count:9"))],
        [btn("0", _self_cb(uid, "channel_count:0")), btn("00", _self_cb(uid, "channel_count:00")), btn("⌫", _self_cb(uid, "channel_count:back"), "danger")],
        [btn("🗑 پاک کردن", _self_cb(uid, "channel_count:clear"), "danger"), btn("✅ تأیید", _self_cb(uid, "channel_count:confirm"), "success")],
        [btn("↩️ برگشت", _self_cb(uid, "channel_count:back_media"), "primary")],
    ]


def _channel_count_text(state):
    labels = {
        "photos": "تصویر", "videos": "ویدیو", "music": "موسیقی",
        "voice": "ویس", "text": "متن", "all": "کل مدیاها"
    }
    media = labels.get(state.get("media"), "مدیا")
    value = str(state.get("count_input") or "0")
    return (
        f"💾 <b>{media}</b>\n\n"
        "چند مورد آخر را ذخیره کنم؟\n\n"
        f"🔢 <b>{value}</b>\n\n"
        "مقدار را با دکمه‌های زیر انتخاب کن و سپس «تأیید» را بزن."
    )


def _channel_progress_text(percent, label="درحال ذخیره سازی…", processed=None, total=None, successful=None, failed=None):
    percent = max(0, min(100, int(percent)))
    slots = 24
    filled = round(slots * percent / 100)
    bar = "▰" * filled + "▱" * (slots - filled)
    counts = ""
    if total is not None:
        counts = f"\n\n📦 <b>{int(processed or 0)}/{int(total)}</b>"
        if successful is not None or failed is not None:
            counts += f"  •  ✅ {int(successful or 0)}  ❌ {int(failed or 0)}"
    return f"💾 <b>ذخیره مدیا</b>\n{bar} <b>{percent}%</b>\n<i>{html.escape(label)}</i>{counts}"


async def _channel_save_ui_edit(state, text, buttons=None, *, retries=2):
    """Edit only the channel-save panel message owned by this operation."""
    chat_id = state.get("panel_chat_id")
    message_id = state.get("panel_message_id")
    uid = state.get("uid", "?")
    if chat_id is None or message_id is None:
        raise RuntimeError(
            f"channel-save panel identity is missing: chat_id={chat_id} message_id={message_id}"
        )

    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            await _tg_call_with_flood_retry(
                lambda: bot.edit_message(
                    int(chat_id),
                    int(message_id),
                    text,
                    parse_mode="html",
                    buttons=buttons,
                ),
                label=f"channel-save UI edit uid={uid}",
            )
            return True
        except Exception as exc:
            last_exc = exc
            print(
                f"[CHANNEL_SAVE UI] edit failed: "
                f"chat_id={chat_id} message_id={message_id} "
                f"attempt={attempt + 1}/{max(1, int(retries))} error={exc}"
            )
            if attempt + 1 < max(1, int(retries)):
                await asyncio.sleep(0.15)

    raise last_exc


def _channel_progress_text(
    percent,
    label="در حال ذخیره…",
    processed=None,
    total=None,
    successful=None,
    failed=None,
):
    percent = max(0, min(100, int(percent)))
    slots = 24
    filled = round(slots * percent / 100)
    bar = "▰" * filled + "▱" * (slots - filled)
    counts = ""
    if total is not None:
        counts = f"\n\n📦 <b>{int(processed or 0)}/{int(total)}</b>"
        if successful is not None or failed is not None:
            counts += (
                f"\n✅ موفق: {int(successful or 0)}"
                f"\n❌ ناموفق: {int(failed or 0)}"
            )
    return f"💾 <b>ذخیره مدیا</b>\n{bar} <b>{percent}%</b>\n<i>{html.escape(label)}</i>{counts}"


class _ChannelProgressController:
    """Single UI owner for the channel-save panel message."""

    def __init__(self, state, *, min_interval=0.5):
        self.state = state
        self.chat_id = int(state["panel_chat_id"])
        self.message_id = int(state["panel_message_id"])
        self.min_interval = float(min_interval)
        self.last_edit = 0.0
        self.last_percent = None
        self.last_processed = -1
        self.last_successful = -1
        self.last_failed = -1

    async def edit(self, text, buttons=None):
        return await _channel_save_ui_edit(self.state, text, buttons=buttons)

    async def update(
        self,
        processed,
        total,
        *,
        successful=0,
        failed=0,
        force=False,
        label="در حال ذخیره…",
    ):
        total = max(0, int(total))
        processed = max(0, min(int(processed), total)) if total else 0
        percent = 100 if total == 0 else int(processed * 100 / total)
        percent = max(0, min(100, percent))
        now = time.monotonic()

        important = (
            force
            or processed == 0
            or processed == 1
            or (total > 0 and processed == total)
            or failed != self.last_failed
        )
        if not important:
            if percent == self.last_percent:
                return
            if (now - self.last_edit) < self.min_interval:
                return

        text = _channel_progress_text(
            percent,
            label=label,
            processed=processed,
            total=total,
            successful=successful,
            failed=failed,
        )
        await self.edit(text, buttons=None)
        self.last_edit = time.monotonic()
        self.last_percent = percent
        self.last_processed = processed
        self.last_successful = successful
        self.last_failed = failed

    async def phase(self, text):
        await self.edit(text, buttons=None)
        self.last_edit = time.monotonic()

    async def finish(self, *, successful, failed, requested, available):
        if available <= 0:
            await self.edit(
                "❌ <b>مدیای قابل ذخیره پیدا نشد.</b>",
                buttons=[[btn("🔙 بازگشت به پنل", _self_cb(int(self.state["uid"]), "panel"), "primary")]],
            )
            return

        # Always expose the terminal 100% state before the final summary.
        await self.update(
            available,
            available,
            successful=successful,
            failed=failed,
            force=True,
            label="ذخیره کامل شد",
        )

        if successful == requested and failed == 0 and available >= requested:
            text = (
                "✅ <b>ذخیره مدیا تکمیل شد</b>\n\n"
                f"📦 درخواست: {requested}\n"
                f"✅ موفق: {successful}\n"
                f"❌ ناموفق: {failed}"
            )
        else:
            text = (
                "⚠️ <b>ذخیره مدیا تکمیل شد</b>\n\n"
                f"📦 درخواست: {requested}\n"
                f"✅ موفق: {successful}\n"
                f"❌ ناموفق: {failed}"
            )
            if available < requested:
                text += f"\nℹ️ مدیای پیدا شده: {available}"

        await self.edit(
            text,
            buttons=[[btn("🔙 بازگشت به پنل", _self_cb(int(self.state["uid"]), "panel"), "primary")]],
        )


async def _channel_progress_done(state, *, successful, failed, requested, available):
    controller = _ChannelProgressController(state)
    await controller.finish(
        successful=successful,
        failed=failed,
        requested=requested,
        available=available,
    )



async def _self_save_channel_media(client, uid, state, count, progress_cb=None):
    """
    Production-safe private-channel saver.

    Flow:
      1) Resolve the private channel.
      2) Scan messages without blocking the UI.
      3) Immediately expose a real 0% progress state once the target set is known.
      4) Save oldest -> newest, updating the same panel after every item.
      5) Never forward; copy to Saved Messages.
    """
    kind = state.get("media", "all")
    channel_id = state.get("channel_id")
    labels = {
        "photos": "تصویر",
        "videos": "ویدیو",
        "music": "موسیقی",
        "voice": "ویس",
        "text": "متن",
        "all": "مدیا",
    }

    if not channel_id:
        return {
            "saved": 0, "failed": 0, "processed": 0, "available": 0,
            "error": "❌ چنل انتخاب نشده است.",
        }

    try:
        access_hash = state.get("channel_access_hash")
        if access_hash is not None:
            entity = await _tg_call_with_flood_retry(
                lambda: client.get_entity(
                    types.InputPeerChannel(int(channel_id), int(access_hash))
                ),
                label="resolve private channel",
            )
        else:
            entity = await _tg_call_with_flood_retry(
                lambda: client.get_entity(int(channel_id)),
                label="resolve private channel",
            )

        # --------------------------------------------------------
        # PHASE 1: collect the requested messages.
        # This phase deliberately has its own live UI because Telegram
        # can take a while to walk a large/private channel history.
        # --------------------------------------------------------
        selected_items = []
        scanned = 0
        last_scan_ui = 0.0

        if progress_cb:
            await progress_cb(
                0, 0,
                successful=0,
                failed=0,
                force=True,
                label=f"🔎 درحال پیدا کردن {count} {labels.get(kind, 'مدیا')} آخر…",
            )

        async for msg in client.iter_messages(
            entity,
            limit=max(count * 8, count + 30),
        ):
            if not msg:
                continue

            scanned += 1

            if kind == "photos":
                matched = bool(getattr(msg, "photo", None))
            elif kind == "videos":
                matched = bool(getattr(msg, "video", None))
            elif kind == "music":
                matched = bool(getattr(msg, "audio", None)) and not bool(
                    getattr(msg, "voice", None)
                )
            elif kind == "voice":
                matched = bool(getattr(msg, "voice", None))
            elif kind == "text":
                matched = bool((msg.raw_text or "").strip()) and not getattr(
                    msg, "media", None
                )
            else:
                matched = bool(
                    getattr(msg, "media", None) or (msg.raw_text or "").strip()
                )

            if matched:
                selected_items.append(msg)
                if len(selected_items) >= count:
                    break

            # Do not edit Telegram for every scanned message.
            # A small live heartbeat prevents the UI from looking frozen.
            now = time.monotonic()
            if progress_cb and (now - last_scan_ui >= 0.8):
                last_scan_ui = now
                await progress_cb(
                    0, 0,
                    successful=0,
                    failed=0,
                    force=True,
                    label=(
                        f"🔎 درحال بررسی چنل… "
                        f"{len(selected_items)}/{count} مورد پیدا شد"
                    ),
                )

        total = len(selected_items)

        if not selected_items:
            if kind == "photos":
                error = "❌ <b>مدیای قابل ذخیره پیدا نشد.</b>"
            else:
                error = (
                    f"❌ <b>مدیای قابل ذخیره پیدا نشد.</b>\n"
                    f"<i>نوع انتخاب‌شده: {html.escape(labels.get(kind, kind))}</i>"
                )
            return {
                "saved": 0,
                "failed": 0,
                "processed": 0,
                "available": 0,
                "error": error,
            }

        # --------------------------------------------------------
        # PHASE 2: real progress starts HERE.
        # This call is forced, so the first 0% state is always visible.
        # --------------------------------------------------------
        if progress_cb:
            await progress_cb(
                0,
                total,
                successful=0,
                failed=0,
                force=True,
                label=f"💾 درحال ذخیره {labels.get(kind, 'مدیا')}…",
            )

        saved = 0
        failed = 0
        processed = 0

        # Oldest -> newest. Copying instead of forwarding removes the
        # Telegram forward header.
        for msg in reversed(selected_items):
            media_id = getattr(msg, "id", "?")

            # Give the user an immediate visual transition before a
            # potentially slow upload/download starts.
            if progress_cb:
                await progress_cb(
                    processed,
                    total,
                    successful=saved,
                    failed=failed,
                    force=True,
                    label=f"💾 درحال ذخیره مورد {processed + 1} از {total}…",
                )

            try:
                media = getattr(msg, "media", None)
                caption = msg.raw_text or None

                if media:
                    direct_error = None
                    try:
                        await _tg_call_with_flood_retry(
                            lambda m=media, c=caption: client.send_file(
                                "me", m, caption=c
                            ),
                            label=f"copy media {media_id}",
                        )
                    except Exception as direct_exc:
                        direct_error = direct_exc
                        print(
                            f"[CHANNEL_SAVE {uid}] direct media copy failed "
                            f"{media_id}: {direct_exc}; using download fallback"
                        )

                        path = await _tg_call_with_flood_retry(
                            lambda m=msg: m.download_media(),
                            label=f"download media {media_id}",
                        )
                        if not path:
                            raise RuntimeError(
                                f"media download failed: {direct_error}"
                            )

                        try:
                            await _tg_call_with_flood_retry(
                                lambda p=path, c=caption: client.send_file(
                                    "me", p, caption=c
                                ),
                                label=f"upload media {media_id}",
                            )
                        finally:
                            with contextlib.suppress(Exception):
                                os.remove(path)

                elif caption:
                    await _tg_call_with_flood_retry(
                        lambda c=caption: client.send_message("me", c),
                        label=f"save text {media_id}",
                    )
                else:
                    raise RuntimeError(
                        "matched message contained no savable content"
                    )

                saved += 1

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                failed += 1
                print(
                    f"[CHANNEL_SAVE {uid}] item failed "
                    f"channel={channel_id} message={media_id}: {exc}"
                )

            finally:
                processed += 1

                if progress_cb:
                    await progress_cb(
                        processed,
                        total,
                        successful=saved,
                        failed=failed,
                        force=True,
                        label=(
                            "💾 درحال ذخیره…"
                            if processed < total
                            else "✅ همه موارد بررسی شدند"
                        ),
                    )

        return {
            "saved": saved,
            "failed": failed,
            "processed": processed,
            "available": total,
            "error": None,
        }

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        print(f"[CHANNEL_SAVE {uid}] channel save failed: {exc}")
        import traceback
        traceback.print_exc()
        return {
            "saved": 0,
            "failed": 0,
            "processed": 0,
            "available": 0,
            "error": (
                f"❌ ذخیره از چنل انجام نشد.\n"
                f"<code>{html.escape(str(exc))}</code>"
            ),
        }


async def _channel_save_worker(client, uid, state, count, operation_id):
    """
    Owns the complete save lifecycle.

    The callback only changes the state and starts this task.
    All heavy Telegram work happens here, so the callback never freezes
    while channel history/media is being processed.
    """
    controller = _ChannelProgressController(state)
    current_task = asyncio.current_task()

    async def progress(processed, total, *, successful=0, failed=0,
                        force=False, label="درحال ذخیره…"):
        await controller.update(
            processed,
            total,
            successful=successful,
            failed=failed,
            force=force,
            label=label,
        )

    try:
        # Smooth transition, matching the game's "resolving" phase.
        await controller.phase("⏳ <b>درحال آماده‌سازی ذخیره…</b>")
        await asyncio.sleep(0.15)

        if not client:
            raise RuntimeError("سلف فعال نیست")

        await controller.phase(
            f"🔎 <b>درحال پیدا کردن {count} "
            f"{html.escape(str(state.get('media') or 'مدیا'))} آخر…</b>"
        )

        # Yield to the event loop before the potentially long channel scan.
        await asyncio.sleep(0)

        result = await _self_save_channel_media(
            client,
            uid,
            state,
            count,
            progress_cb=progress,
        )

        if result.get("error") and result.get("processed", 0) == 0:
            if result.get("available", 0) == 0:
                await controller.finish(
                    successful=result.get("saved", 0),
                    failed=result.get("failed", 0),
                    requested=count,
                    available=0,
                )
            else:
                await controller.edit(
                    result["error"],
                    buttons=[[
                        btn(
                            "🔙 بازگشت به پنل",
                            _self_cb(uid, "panel"),
                            "primary",
                        )
                    ]],
                )
            return

        # Let 100% remain visible briefly before replacing it with
        # the final summary. This makes the transition feel deliberate.
        await controller.finish(
            successful=result.get("saved", 0),
            failed=result.get("failed", 0),
            requested=count,
            available=result.get("available", 0),
        )

        await asyncio.sleep(0.35)

    except asyncio.CancelledError:
        print(f"[CHANNEL_SAVE {uid}] worker cancelled")
        with contextlib.suppress(Exception):
            await controller.edit(
                "⚠️ <b>ذخیره مدیا متوقف شد.</b>",
                buttons=[[
                    btn(
                        "🔙 بازگشت به پنل",
                        _self_cb(uid, "panel"),
                        "primary",
                    )
                ]],
            )
        raise

    except Exception as exc:
        print(f"[CHANNEL_SAVE {uid}] fatal worker error")
        import traceback
        traceback.print_exc()
        with contextlib.suppress(Exception):
            await controller.edit(
                "❌ <b>ذخیره مدیا با خطا متوقف شد.</b>\n"
                f"خطا: <code>{html.escape(str(exc))}</code>",
                buttons=[[
                    btn(
                        "🔙 بازگشت به پنل",
                        _self_cb(uid, "panel"),
                        "primary",
                    )
                ]],
            )

    finally:
        # Never let an old worker clear a newer operation.
        saved_state = self_channel_save_state(uid)
        if saved_state.get("operation_id") == operation_id:
            self_clear_channel_save_state(uid)

        if _channel_save_tasks.get(uid) is current_task:
            _channel_save_tasks.pop(uid, None)


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
    slots = 10
    filled = round(slots * percent / 100)
    bar = "▰" * filled + "▱" * (slots - filled)
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


def _archive_progress_text(percent: int, phase: str = "در حال استخراج…", current: int = 0, total: int = 0):
    percent = max(0, min(100, int(percent)))
    slots = 24
    filled = round(slots * percent / 100)
    bar = "▰" * filled + "▱" * (slots - filled)
    counter = f"  <code>{int(current)}/{int(total)}</code>" if total else ""
    return (
        f"📦 <b>استخراج آرشیو</b>\n\n"
        f"<code>{bar}</code> <b>{percent}%</b>{counter}\n"
        f"<i>{html.escape(phase)}</i>"
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
            await event.edit(_archive_progress_text(0, "در حال دانلود آرشیو…"))

        downloaded = await replied.download_media(file=str(archive_path))
        if not downloaded:
            return "❌ دانلود آرشیو ناموفق بود."

        # The five-second bar starts after download and covers the extraction
        # phase. Extraction itself runs in a worker thread so Telethon stays
        # responsive and the progress message can keep updating.
        progress_started = time.monotonic()
        progress_task = asyncio.create_task(
            _archive_progress_5s(event, progress_started, "در حال استخراج…")
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
        await event.edit(_archive_progress_text(0, "در حال ارسال فایل‌ها…", 0, total))

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
                    _archive_progress_text(percent, "در حال ارسال فایل‌ها…", index, total)
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

    # Interactive private-channel saver.  The bot panel stores only the
    # current user's short state; the self account performs the actual fetch.
    channel_state = self_channel_save_state(uid)
    if channel_state:
        step = channel_state.get("step")
        if step == "channel":
            # Kept only as a compatibility fallback for old pending states.
            try:
                raw_channel = text.strip()
                if raw_channel.startswith("https://t.me/"):
                    raw_channel = "@" + raw_channel.rstrip("/").split("/")[-1].split("?")[0]
                entity = await event.client.get_entity(raw_channel)
                if getattr(entity, "megagroup", False) or getattr(entity, "username", None):
                    raise ValueError("فقط چنل خصوصیِ عضو اکانت قابل انتخاب است")
                channel_state.update({"step": "media", "channel_id": int(entity.id), "channel_title": getattr(entity, "title", "بدون نام")})
                self_set_channel_save_state(uid, channel_state)
                await event.edit("📢 چنل انتخاب شد. نوع مدیا را از دکمه‌های همین پنل انتخاب کن.", parse_mode="html", buttons=self_channel_media_buttons(uid))
            except Exception as exc:
                await event.edit(f"❌ چنل پیدا نشد یا دسترسی وجود ندارد.\n<code>{html.escape(str(exc))}</code>", parse_mode="html")
                self_clear_channel_save_state(uid)
            return

        if step == "processing":
            # A processing state consumes this flow until the real save finishes.
            # In particular, a new numeric outgoing message must never restart it.
            return

        if step == "count":
            # Count is selected only from the inline numeric keypad.
            # Typed messages must never start the channel-save operation.
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
        # Accept: متن / متن + ریپلی / متن + ریپلای and reply to the original media.
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
                timeout=float(os.getenv("STT_TIMEOUT_SECONDS", "600")),
            )
        except asyncio.TimeoutError:
            result = "❌ تبدیل ویس به متن زمان‌بر شد؛ دوباره تلاش کن."
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

    switches = {
        "بولد روشن": ("bold", "on"), "بولد خاموش": ("bold", "off"),
        "فونت فارسی روشن": ("persian_font", "on"), "فونت فارسی خاموش": ("persian_font", "off"),
        "ترنسلیت روشن": ("translate", "on"), "ترنسلیت خاموش": ("translate", "off"),
        "تبچی روشن": ("auto_reply", "on"), "تبچی خاموش": ("auto_reply", "off"),
        "سین روشن": ("auto_read", "on"), "سین خاموش": ("auto_read", "off"),
        "تایپینگ روشن": ("typing", "on"), "تایپینگ خاموش": ("typing", "off"),
        "حالت بازی روشن": ("game_mode", "on"), "حالت بازی خاموش": ("game_mode", "off"),
        "ساعت روشن": ("time_name", "on"), "ساعت خاموش": ("time_name", "off"),
    }
    if low in switches:
        key, val = switches[low]
        self_set(uid, key, val)
        with contextlib.suppress(Exception):
            await event.edit(f"✅ {text}\nوضعیت: {'روشن' if val == 'on' else 'خاموش'}")
        return

    if low.startswith("تبچی متن"):
        value = text[len("تبچی متن"):].strip()
        if not value:
            await event.edit("❌ متن تبچی نمی‌تواند خالی باشد.")
            return
        self_set(uid, "auto_reply_text", value)
        self_set(uid, "auto_reply", "on")
        await event.edit("✅ متن تبچی ذخیره و فعال شد.")
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
        allowed = {"❤️","❤","🧡","💛","💚","💙","💜","🖤","🤍","🤎","🔥","✨","⭐","👍","👎","😂","🤣","😍","🥰","😎","😢","😡","👏","🙏","🎉","💯","⚡","🌹","😈","🕊"}
        if emoji not in allowed:
            await event.edit("❌ این ریاکشن پشتیبانی نمی‌شود. مثال: ریاکشن 🔥")
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
        self_set_reaction(uid, target, "❤️" if emoji == "❤" else emoji)
        await event.edit(f"✅ ریاکشن {emoji} برای کاربر `{target}` فعال شد.")
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
        emoji = self_reaction_map(uid).get(int(event.sender_id), "❤️")
        with contextlib.suppress(Exception):
            await client(SendReactionRequest(
                peer=event.peer_id,
                msg_id=event.id,
                reaction=[ReactionEmoji(emoticon=emoji)],
            ))

    if event.is_private and self_get(uid, "auto_reply", "off") == "on" and event.sender_id and int(event.sender_id) != int(uid):
        cache_key = (int(uid), int(event.sender_id))
        if cache_key not in _self_reply_cache:
            _self_reply_cache.add(cache_key)
            with contextlib.suppress(Exception):
                await event.respond(self_get(uid, "auto_reply_text", "سلام، فعلاً در دسترس نیستم."))
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
        except Exception as exc:
            print(f"[SELF {user_id}] outgoing handler error: {exc}")

    @client.on(events.NewMessage(incoming=True))
    async def _self_incoming(event):
        try:
            await self_handle_incoming(event, user_id)
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
    """Insert the self panel into any chat through Telegram inline mode."""
    query = (event.text or "").strip().casefold()
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
            title="📚 راهنمای سلف",
            description="راهنمای ۹ صفحه‌ای سلف با قابلیت‌های جدید نرخ ارز و لوگوساز.",
            text=self_guide_text(1),
            parse_mode="html",
            buttons=self_guide_buttons(uid, 1),
        )
    else:
        result = event.builder.article(
            title="⚙️ پنل سلف",
            description="پنل تنظیمات سلف را در همین چت ارسال کن.",
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
        await group_message(event)


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

    # --------------------------------------------------------
    # DIRECT PANEL IN BOT PRIVATE CHAT
    # --------------------------------------------------------
    if text.strip().casefold() == "پنل":
        if is_banned(user_id):
            await event.reply("🚫 شما توسط ادمین مسدود شده‌اید.")
            return
        if not has_registered_phone(user_id):
            await send_phone_request(user_id)
            return
        await send_self_panel(event.chat_id, user_id)
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
        await bot.send_message(user_id, "✅ شماره شما ثبت شد. حالا می‌توانی مستقیم از گزینه‌های ربات استفاده کنی.", buttons=Button.clear())
        await send_main(user_id, user_id)
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

@bot.on(events.NewMessage)
async def group_commands(event):
    if not (event.is_group or event.is_channel):
        return

    text = (event.raw_text or "").strip()
    user_id = event.sender_id

    if not user_id:
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
        await edit_or_send(
            event,
            "به سلف‌ساز خوش آمدید",
            main_buttons(user_id)
        )
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
            [btn("➕ اضافه کردن الماس", b"add_balance", "success")],
            [btn("🚫 مسدود کردن کاربر", b"ban_user", "danger")],
            [btn("🔓 رفع مسدودی", b"unban_user", "success")],
            [btn("🔙 برگشت", b"back", "primary")]
        ]
        await edit_or_send(
            event,
            "🛠 **پنل مدیریت**\n\nیک گزینه را انتخاب کنید:",
            buttons
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

    await restore_workers()

    print("🚀 Bot is running.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")


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
import contextlib
import html
import json
import os
import random
import secrets
import tempfile
import shutil
import re
import sqlite3
import time
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


def get_balance(user_id: int) -> int:
    init_user_db(user_id)
    with connect_db(user_id) as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0


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


def change_balance(user_id: int, amount: int):
    init_user_db(user_id)
    with connect_db(user_id) as db:
        db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id)
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
_self_reply_cache = set()
_inline_bot_cache = {}
_cleanup_tasks = {}
_cleanup_panel_messages = {}
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
            btn("📚 راهنما", _self_cb(uid, "guide"), "primary"),
        ],
        [
            btn("🧹 پاکسازی", _self_cb(uid, "cleanup"), "danger"),
            btn("💾 ذخیره چنل", _self_cb(uid, "channel_save"), "primary"),
        ],
        [btn("❌ بستن", _self_cb(uid, "close"), "danger")],
    ]


def self_panel_text(uid):
    def st(k):
        return "🟢 روشن" if self_get(uid, k, "off") == "on" else "🔴 خاموش"

    return (
        "╭━━━━━━━ ✦ پنل سلف ✦ ━━━━━━━╮\n\n"
        f"🕐 ساعت ایران: {st('time_name')}  •  <code>{self_clock(uid)}</code>\n"
        f"🔤 فونت ساعت: <b>{_font_label('clock', self_get(uid,'clock_font','normal'))}</b>\n"
        f"🅱 بولد: {st('bold')}\n"
        f"🅵 فونت فارسی: {st('persian_font')}\n"
        f"🔤 فونت انگلیسی: <b>{_font_label('english', self_get(uid,'english_font','normal'))}</b>\n"
        f"🌐 ترجمه: {st('translate')}\n"
        f"❤️ ریاکشن: <b>{len(self_reaction_targets(uid))}</b> کاربر\n"
        f"👁 سین: {st('auto_read')}\n"
        f"⌨️ تایپینگ: {st('typing')}\n"
        f"🎮 بازی: {st('game_mode')}\n"
        f"🤖 تبچی: {st('auto_reply')}\n"
        f"🔒 قفل چت: <b>{len(self_chat_lock_targets(uid))}</b> کاربر\n"
        f"🧹 پاکسازی: {self_get(uid, 'cleanup_progress', 'آماده') or 'آماده'}\n\n"
        "با دکمه‌های پایین تنظیمات را مستقیم تغییر بده.\n"
        "╰━━━━━━━━HusteRIX━━━━━━━━╯"
    )


def self_guide_text(page=1):
    pages = [
        (
            "📚 <b>راهنمای سلف — صفحه ۱ از ۳</b>\n\n"
            "⚙️ <b>پنل و تنظیمات</b>\n"
            "• «پنل» پنل سلف را در همان چتی که دستور را فرستادی باز می‌کند.\n"
            "• ساعت، بولد، فونت فارسی/انگلیسی، ترجمه، سین، تایپینگ و حالت بازی از پنل قابل کنترل‌اند.\n\n"
            "❤️ <b>ریاکشن</b>\n"
            "• روی پیام کاربر ریپلای کن و «ریاکشن ❤️» را بفرست.\n"
            "• برای حذف: «حذف ریاکشن».\n\n"
            "🤖 <b>تبچی</b>\n"
            "• «تبچی روشن / خاموش»\n"
            "• «تبچی متن متن دلخواه» برای تعیین پاسخ خودکار.\n\n"
            "👁 <b>سین</b> و ⌨️ <b>تایپینگ</b>\n"
            "• هرکدام را از پنل روشن یا خاموش کن."
        ),
        (
            "📚 <b>راهنمای سلف — صفحه ۲ از ۳</b>\n\n"
            "💾 <b>ذخیره از چنل خصوصی</b>\n"
            "• از پنل، «ذخیره از چنل خصوصی» را بزن.\n"
            "• به‌جای وارد کردن آیدی، همه چنل‌های خصوصیِ عضو اکانت نمایش داده می‌شوند.\n"
            "• فرمت هر مورد: <code>-ID</code> | <b>Channel Name</b>\n"
            "• بعد از انتخاب چنل، نوع مدیا و تعداد را انتخاب کن.\n"
            "• ذخیره‌سازی با کپی انجام می‌شود و پیام فوروارد نمی‌شود.\n"
            "• برای عکس، فقط پیام‌هایی که واقعاً <b>Photo</b> دارند شمرده می‌شوند؛ اگر عکسی نباشد لاگ ثبت می‌شود و نتیجه هم صریح اعلام می‌شود.\n\n"
            "💎 <b>انتقال الماس</b>\n"
            "• روی پیام کاربر ریپلای کن و «انتقال 500» بفرست.\n\n"
            "📝 <b>ویس / OCR</b>\n"
            "• روی ویس ریپلای + «متن».\n"
            "• روی تصویر ریپلای + «OCR».\n\n"
            "🖼️ <b>تبدیل رسانه</b>\n"
            "• عکس → «استیکر»\n"
            "• استیکر → «عکس»"
        ),
        (
            "📚 <b>راهنمای سلف — صفحه ۳ از ۳</b>\n\n"
            "🧹 <b>پاکسازی اکانت</b>\n"
            "• چت‌ها، گپ‌ها، کانال‌ها، مخاطبین، ربات‌ها یا همه را جداگانه پاکسازی کن.\n"
            "• Saved Messages دست‌نخورده می‌ماند.\n\n"
            "🔒 <b>قفل چت</b>\n"
            "• روی پیام کاربر در پیوی ریپلای کن و «قفل چت» بفرست.\n"
            "• برای خاموش کردن: «بازکردن قفل چت».\n\n"
            "🚫 <b>بلاک + ریپلای</b>\n"
            "• داخل گروه روی پیام کاربر ریپلای کن و «بلاک + ریپلای» بفرست.\n\n"
            "🎲 <b>تاس</b>\n"
            "• «تاس 1» تا «تاس 6»؛ فقط نتیجه موفق باقی می‌ماند.\n\n"
            "🕐 <b>فونت ساعت</b>\n"
            "• از دکمه فونت ساعت، فونت‌ها را یکی‌یکی تغییر بده."
        )
    ]
    page = max(1, min(int(page), len(pages)))
    return pages[page - 1]


def self_guide_buttons(uid, page=1):
    page = max(1, min(int(page), 3))
    nav = []
    if page > 1:
        nav.append(btn("◀️ قبلی", _self_cb(uid, f"guide_page:{page-1}"), "danger"))
    if page < 3:
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
                # Snapshot/archive the latest five messages BEFORE the two-sided
                # DeleteHistoryRequest.  After revoke=True Telegram may remove
                # the messages from history, so doing this after deletion is too late.
                archived = await _archive_last_five_before_delete(
                    client, uid, getattr(entity, "id", 0), []
                )
                if archived:
                    print(f"[CLEANUP {uid}] archived {archived} messages before deleting private chat {getattr(entity,'id','?')}")
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
    if action.startswith("guide_page:"):
        try:
            page = int(action.split(":", 1)[1])
        except ValueError:
            page = 1
        page = max(1, min(page, 3))
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
            state = {"step": "media", "channel_id": int(entity.id), "channel_title": title}
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
        state.update({"step": "count", "media": media_kind})
        self_set_channel_save_state(uid, state)
        await event.edit(
            f"💾 <b>{labels[media_kind]}</b>\n\nچند مورد آخر را ذخیره کنم؟\nمثال: <code>10</code>",
            parse_mode="html",
            buttons=[[btn("❌ لغو", _self_cb(uid, "channel_cancel"), "danger")],
                     [btn("↩️ برگشت", _self_cb(uid, "channel_save"), "primary")]],
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


async def _self_transcribe_reply(event, uid):
    """Voice/audio -> text with faster-whisper."""
    if not event.is_reply:
        return "❌ روی ویس ریپلای کن و «متن» را بفرست."
    replied = await event.get_reply_message()
    if not replied or not replied.media:
        return "❌ ویس یا فایل صوتی پیدا نشد."

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"husterix_stt_{uid}_"))
    try:
        path = await replied.download_media(file=str(tmp_dir))
        if not path:
            return "❌ دانلود ویس ناموفق بود."

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return "❌ قابلیت تبدیل ویس به متن نیاز به نصب `faster-whisper` دارد."

        model = getattr(_self_transcribe_reply, "_model", None)
        if model is None:
            model = WhisperModel(
                os.getenv("WHISPER_MODEL", "small"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            )
            _self_transcribe_reply._model = model

        def run_transcription():
            segments, _ = model.transcribe(
                str(path),
                language=os.getenv("WHISPER_LANGUAGE", "fa"),
                vad_filter=True,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

        result = await asyncio.to_thread(run_transcription)
        return f"📝 **متن ویس:**\n\n{result}" if result else "❌ صدایی برای تبدیل به متن پیدا نشد."
    except Exception as exc:
        print(f"[SELF {uid}] transcription failed: {exc}")
        return "❌ تبدیل ویس به متن انجام نشد."
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
            image = Image.open(path)
            try:
                return pytesseract.image_to_string(image, lang="fas+eng").strip()
            except Exception:
                return pytesseract.image_to_string(image, lang="eng").strip()

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


async def _self_roll_guaranteed_value(event, uid, target):
    """Send a real Telegram dice and reroll until Telegram returns the requested value."""
    try:
        from telethon.tl import types

        target = int(target)
        if target < 1 or target > 6:
            return False

        # A requested face has a 1/6 chance per roll. 60 attempts make
        # failure extremely unlikely while preventing an accidental endless loop.
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

            # Keep only the successful roll visible in the chat.
            with contextlib.suppress(Exception):
                await _tg_call_with_flood_retry(
                    lambda: event.client.delete_messages(
                        event.chat_id,
                        msg.id,
                        revoke=True,
                    ),
                    label="delete failed dice",
                )

            await asyncio.sleep(0.2)

        return False
    except Exception as exc:
        print(f"[SELF {uid}] forced dice {target} failed: {exc}")
        return False


# Backward-compatible alias for any existing internal references.
async def _self_roll_guaranteed_six(event, uid):
    return await _self_roll_guaranteed_value(event, uid, 6)


def self_channel_media_buttons(uid):
    return [
        [btn("🖼 تصاویر", _self_cb(uid, "channel_media:photos")), btn("🎬 ویدیوها", _self_cb(uid, "channel_media:videos"))],
        [btn("🎵 موسیقی", _self_cb(uid, "channel_media:music")), btn("🎤 ویس ها", _self_cb(uid, "channel_media:voice"))],
        [btn("📝 متن ها", _self_cb(uid, "channel_media:text")), btn("📦 کل مدیا ها", _self_cb(uid, "channel_media:all"))],
        [btn("❌ لغو", _self_cb(uid, "channel_cancel"), "danger")],
    ]


async def _self_save_channel_media(client, uid, state, count):
    """Copy requested items from a private channel to Saved Messages; never forward."""
    kind = state.get("media", "all")
    channel_id = state.get("channel_id")
    labels = {"photos":"تصویر", "videos":"ویدیو", "music":"موسیقی", "voice":"ویس", "text":"متن", "all":"مدیا"}
    if not channel_id:
        return "❌ چنل انتخاب نشده است."
    try:
        entity = await client.get_entity(int(channel_id))
        selected = []
        async for msg in client.iter_messages(entity, limit=max(count * 8, count + 30)):
            if not msg:
                continue
            if kind == "photos":
                matched = bool(getattr(msg, "photo", None))
            elif kind == "videos":
                matched = bool(getattr(msg, "video", None))
            elif kind == "music":
                matched = bool(getattr(msg, "audio", None)) and not bool(getattr(msg, "voice", None))
            elif kind == "voice":
                matched = bool(getattr(msg, "voice", None))
            elif kind == "text":
                matched = bool((msg.raw_text or "").strip()) and not getattr(msg, "media", None)
            else:
                matched = bool(getattr(msg, "media", None) or (msg.raw_text or "").strip())
            if matched:
                selected.append(msg)
                if len(selected) >= count:
                    break

        if not selected:
            if kind == "photos":
                print(f"[CHANNEL_SAVE {uid}] NO_PHOTO: no photo was published in channel {channel_id}")
                return "❌ هیچ عکسی در این چنل منتشر نشده است."
            return f"❌ هیچ موردی از نوع «{labels.get(kind, kind)}» در چنل پیدا نشد."

        saved = 0
        # Oldest -> newest, but copied rather than forwarded so there is no Telegram forward header.
        for msg in reversed(selected):
            try:
                media = getattr(msg, "media", None)
                caption = msg.raw_text or None
                if media:
                    path = await msg.download_media()
                    if not path:
                        print(f"[CHANNEL_SAVE {uid}] media download failed for message {msg.id}")
                        continue
                    try:
                        await client.send_file("me", path, caption=caption)
                    finally:
                        with contextlib.suppress(Exception):
                            os.remove(path)
                elif caption:
                    await client.send_message("me", caption)
                else:
                    continue
                saved += 1
            except Exception as exc:
                print(f"[CHANNEL_SAVE {uid}] copy failed channel={channel_id} message={getattr(msg,'id','?')}: {exc}")

        if kind == "photos" and saved == 0:
            print(f"[CHANNEL_SAVE {uid}] NO_PHOTO: channel {channel_id} had no successfully copyable photo")
            return "❌ هیچ عکسی در این چنل قابل ذخیره‌سازی نبود."
        return f"✅ {saved} {labels.get(kind, 'مورد')} آخر چنل بدون فوروارد در پیام‌های ذخیره‌شده کپی شد."
    except Exception as exc:
        print(f"[CHANNEL_SAVE {uid}] channel save failed: {exc}")
        return f"❌ ذخیره از چنل انجام نشد.\n<code>{html.escape(str(exc))}</code>"


async def self_handle_outgoing(event, uid):
    text = (event.raw_text or "").strip()
    low = text.casefold()
    if not text:
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

        if step == "count":
            try:
                count = int(text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
                if count < 1 or count > 1000:
                    raise ValueError("تعداد باید بین 1 تا 1000 باشد")
            except Exception as exc:
                await event.edit(f"❌ تعداد نامعتبر است: {exc}")
                return
            try:
                result_text = await _self_save_channel_media(event.client, uid, channel_state, count)
                await event.edit(result_text, parse_mode="html")
            finally:
                self_clear_channel_save_state(uid)
            return

    if low in {"دانلود", "download"}:
        await event.edit(await _self_save_replied_message(event, uid))
        return

    if low in {"متن", "text"} and event.is_reply:
        await event.edit(await _self_transcribe_reply(event, uid), parse_mode="md")
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

    if low in {"پنل", "panel"}:
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

    if low in {"راهنما", "guide"}:
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

    transfer = re.fullmatch(r"انتقال\s+(\d+)", text)
    if transfer:
        amount = int(transfer.group(1))
        if not (event.is_group or event.is_channel):
            await event.edit("❌ این دستور فقط داخل گپ قابل استفاده است.")
            return
        if amount <= 0:
            await event.edit("❌ مقدار انتقال باید بیشتر از صفر باشد.")
            return
        if not event.is_reply:
            await event.edit("❌ روی پیام کاربر ریپلای کن و بنویس: انتقال 500")
            return
        replied = await event.get_reply_message()
        target = int(replied.sender_id) if replied and replied.sender_id else 0
        if not target:
            await event.edit("❌ گیرنده پیدا نشد.")
            return
        if target == uid:
            await event.edit("❌ نمی‌توانید به خودتان الماس انتقال دهید.")
            return
        balance = get_balance(uid)
        if balance < amount:
            await event.edit(f"❌ موجودی کافی نیست.\n💎 موجودی شما: {balance:,}")
            return
        init_user_db(target)
        change_balance(uid, -amount)
        change_balance(target, amount)

        # Notification must be sent by the bot, not by the logged-in self account.
        # The self account performs the transfer, while the bot delivers the receipt.
        sender_label = await _transfer_sender_label(event.client, uid)
        sender_balance = get_balance(uid)
        recipient_balance = get_balance(target)

        await event.edit(
            f"✅ انتقال انجام شد.\n"
            f"💎 {amount:,} الماس به کاربر `{target}` منتقل شد.\n"
            f"💰 موجودی باقی‌مانده: {sender_balance:,}"
        )

        # The notification is deliberately sent by the bot account.
        with contextlib.suppress(Exception):
            await bot.send_message(
                target,
                "🎁 **الماس دریافت کردید!**\n\n"
                f"👤 از طرف {sender_label} برای شما واریز شد.\n"
                f"💎 مقدار: {amount:,} الماس\n"
                f"💎 موجودی فعلی: {recipient_balance:,} الماس"
            )
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
    """Keep a rolling five-message snapshot and an id -> chat lookup."""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    if not chat_id or not message_id:
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
    """Forward the supplied message objects to Saved Messages, with copy fallback."""
    saved = 0
    seen = set()
    for msg in sorted(messages, key=lambda m: getattr(m, "id", 0)):
        msg_id = getattr(msg, "id", None)
        if msg_id in seen:
            continue
        seen.add(msg_id)
        try:
            await client.forward_messages("me", msg)
            saved += 1
            continue
        except Exception:
            pass
        try:
            if getattr(msg, "media", None):
                path = await msg.download_media()
                if path:
                    await client.send_file("me", path, caption=msg.raw_text or None)
                    with contextlib.suppress(Exception):
                        os.remove(path)
                    saved += 1
            elif (msg.raw_text or "").strip():
                await client.send_message("me", msg.raw_text)
                saved += 1
        except Exception as exc:
            print(f"[SELF {uid}] deleted-chat archive {msg_id or '?'}: {exc}")
    return saved


async def _archive_last_five_before_delete(client, uid, chat_id, deleted_ids=None):
    """Archive the deleted message(s) plus the five newest surviving messages."""
    try:
        deleted_ids = {int(x) for x in (deleted_ids or [])}
        cached = list(_deleted_message_cache.get((int(uid), int(chat_id)), []))

        # The deleted message object is valuable: Telegram may remove it from
        # history before iter_messages() runs.  Therefore archive cached deleted
        # objects first instead of filtering them out.
        deleted_messages = [m for m in cached if getattr(m, "id", 0) in deleted_ids]

        # Then take the newest five messages that are still available.
        survivors = [m for m in cached if getattr(m, "id", 0) not in deleted_ids]
        if len(survivors) < 5:
            async for m in client.iter_messages(chat_id, limit=5):
                if getattr(m, "id", 0) not in deleted_ids and all(getattr(m, "id", 0) != getattr(x, "id", 0) for x in survivors):
                    survivors.append(m)
                if len(survivors) >= 5:
                    break
        survivors = sorted(survivors, key=lambda m: getattr(m, "id", 0))[-5:]

        # If one message was deleted, this archives that exact message.  For a
        # bulk deletion it archives all cached deleted messages plus the latest
        # five remaining messages, without scanning the whole chat.
        return await _archive_messages_to_saved(client, uid, deleted_messages + survivors)
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
            if event.is_private:
                _cache_private_message(user_id, event.message)
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
            due_total = int(elapsed_hours * SELF_HOURLY_COST)
            charged_total = int(float(get_setting(user_id, "charged_diamonds", "0") or 0))
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
            f"💎 موجودی: {balance:,}\n"
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

    save_active_session(user_id, session_string, state.get("sub_type", 0))
    set_setting(user_id, "charged_diamonds", "0")

    await start_self_worker(
        user_id,
        session_string,
        state.get("sub_type", 0)
    )

    await bot.send_message(
        user_id,
        "✅ سلف با موفقیت فعال شد!\n\n"
        f"💎 هزینه فعال‌سازی کسر نمی‌شود؛ از این پس {SELF_HOURLY_COST:g} الماس در ساعت محاسبه می‌شود."
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
    if query not in {"پنل", "panel", "راهنما", "guide"}:
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

    if query in {"راهنما", "guide"}:
        result = event.builder.article(
            title="📚 راهنمای سلف",
            description="راهنمای سه‌صفحه‌ای سلف با دکمه‌های قبلی و بعدی.",
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
    if text.strip().casefold() in {"پنل", "panel"}:
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
        target = user_id

        if event.is_reply:
            reply = await event.get_reply_message()
            if reply and reply.sender_id:
                target = reply.sender_id

        balance = get_balance(target)
        try:
            target_entity = await bot.get_entity(int(target))
            username = getattr(target_entity, "username", None)
        except Exception:
            username = None
        identity = f"@{username}" if username else str(int(target))
        buttons = [[btn(f"💎 {balance:,}", f"balance_{target}".encode(), "primary")]]
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
                f"💎 موجودی: {balance:,}"
            )
            return

        change_balance(user_id, -amount)

        total = amount * 2
        tax = max(1, int(total * GAME_TAX))
        prize = total - tax

        text_game = (
            "💎 **بازی**\n"
            f" ‌**{amount:,}**\n\n"
            "🎉 **جایزه برنده:**\n"
            f"{prize:,} 💎\n"
            "💰 **مالیات:**\n"
            f"{tax:,} 💎\n\n"
            "💎 💎 💎\n"
            "برای شروع بازی، نفر دوم روی پیوستن بزند."
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

        msg = await event.reply(text_game, buttons=buttons)

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
                f"💎 موجودی: {balance:,}\n"
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
            f"💎 موجودی: `{balance:,}` الماس\n"
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
        await safe_answer(event, f"💎 موجودی: {balance:,} الماس")
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

        if key not in active_games:
            await safe_answer(event, "❌ این بازی منقضی شده است.", True)
            return

        if get_balance(joiner) < amount:
            await safe_answer(event, "❌ موجودی کافی ندارید.", True)
            return

        change_balance(joiner, -amount)

        total = amount * 2
        tax = max(1, int(total * GAME_TAX))
        prize = total - tax

        winner = secrets.choice((organizer, joiner))
        loser = joiner if winner == organizer else organizer

        change_balance(winner, prize)

        winner_balance = get_balance(winner)
        loser_balance = get_balance(loser)
        winner_name = await user_name(winner)
        loser_name = await user_name(loser)

        game = active_games.pop(key)
        game["task"].cancel()

        with contextlib.suppress(Exception):
            await bot.delete_messages(event.chat_id, event.message_id)

        # Result layout matches the reference UI: label + value in two columns.
        game_result_buttons = [
            [
                btn("🎉 جایزه برنده", b"game_noop_prize", "success"),
                btn(f"💎 {prize:,}", b"game_noop_prize_value", "success"),
            ],
            [
                btn("💎 موجودی برنده", b"game_noop_winner", "primary"),
                btn(f"💎 {winner_balance:,}", b"game_noop_winner_value", "primary"),
            ],
            [
                btn("❌ موجودی بازنده", b"game_noop_loser", "danger"),
                btn(f"💎 {loser_balance:,}", b"game_noop_loser_value", "danger"),
            ],
        ]
        await bot.send_message(
            event.chat_id,
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
            f"💎 موجودی: {balance:,}\n\n"
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
            f"💎 موجودی: {balance:,}\n"
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

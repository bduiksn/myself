
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
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import SetTypingRequest, SendReactionRequest, TranslateTextRequest
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
        buttons=[[Button.request_phone("📱 اشتراک‌گذاری شماره")]],
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
    now=datetime.now(ZoneInfo("Asia/Tehran")).strftime("%H:%M")
    chars=SELF_CLOCK_FONTS.get(self_get(uid,"clock_font","normal"), SELF_CLOCK_FONTS["normal"])
    return now.translate(str.maketrans("0123456789", chars))

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
        [btn("❌ بستن پنل", _self_cb(uid, "close"), "danger")],
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
        f"🤖 تبچی: {st('auto_reply')}\n\n"
        "با دکمه‌های پایین تنظیمات را مستقیم تغییر بده.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def self_guide_text():
    return (
        "╭━━━━━━━ ✦ راهنمای سلف ✦ ━━━━━━━╮\n\n"
        "⚙️ <b>پنل</b>\n"
        "• پنل ← باز کردن پنل شیشه‌ای\n"
        "• راهنما ← نمایش همین راهنما\n\n"
        "✍️ <b>متن</b>\n"
        "• بولد روشن / بولد خاموش\n"
        "• فونت فارسی روشن / فونت فارسی خاموش\n"
        "• فونت انگلیسی از پنل\n"
        "• ترنسلیت روشن / ترنسلیت خاموش\n\n"
        "🕐 <b>ساعت ایران</b>\n"
        "• ساعت روشن / ساعت خاموش\n"
        "• دکمه فونت ساعت، فونت فعلی و پیش‌نمایش را نشان می‌دهد\n\n"
        "❤️ <b>ریاکشن + ریپلای</b>\n"
        "• روی پیام کاربر ریپلای کن و «ریاکشن ❤️» بفرست\n"
        "• حذف: «حذف ریاکشن» یا «حذف ریاکشن + ریپلای»\n\n"
        "👁 سین روشن / سین خاموش\n"
        "⌨️ تایپینگ روشن / تایپینگ خاموش\n"
        "🎮 حالت بازی روشن / حالت بازی خاموش\n"
        "🤖 تبچی روشن / تبچی خاموش\n"
        "• تبچی متن متن دلخواه\n\n"
        "💎 <b>انتقال</b>\n"
        "• روی پیام کاربر ریپلای کن و بنویس: انتقال 500\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯"
    )


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


async def send_self_panel(chat_id: int, uid: int, reply_to=None):
    """Inline panels are bot messages; Telegram does not deliver callback queries to user accounts."""
    return await bot.send_message(
        chat_id,
        self_panel_text(uid),
        parse_mode="html",
        buttons=self_panel_buttons(uid),
        reply_to=reply_to,
    )


async def send_self_guide(chat_id: int, uid: int, reply_to=None):
    return await bot.send_message(
        chat_id,
        self_guide_text(),
        parse_mode="html",
        buttons=[[btn("🔙 برگشت به پنل", _self_cb(uid, "panel"), "primary")]],
        reply_to=reply_to,
    )


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
        with contextlib.suppress(Exception):
            await event.delete()
        return True
    if action == "guide":
        await event.edit(
            self_guide_text(),
            parse_mode="html",
            buttons=[[btn("🔙 برگشت به پنل", _self_cb(uid, "panel"), "primary")]],
        )
        return True
    if action == "panel":
        await event.edit(self_panel_text(uid), parse_mode="html", buttons=self_panel_buttons(uid))
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


async def self_handle_outgoing(event, uid):
    text = (event.raw_text or "").strip()
    low = text.casefold()
    if not text:
        return

    if low == "پنل":
        with contextlib.suppress(Exception):
            await event.delete()
        try:
            await send_self_panel(event.chat_id, uid)
        except Exception as exc:
            print(f"[SELF {uid}] panel send failed: {exc}")
            with contextlib.suppress(Exception):
                await event.client.send_message(
                    event.chat_id,
                    "❌ پنل شیشه‌ای ارسال نشد. مطمئن شوید ربات در این چت عضو است و اجازه ارسال پیام دارد."
                )
        return

    if low == "راهنما":
        with contextlib.suppress(Exception):
            await event.delete()
        try:
            await send_self_guide(event.chat_id, uid)
        except Exception as exc:
            print(f"[SELF {uid}] guide send failed: {exc}")
            with contextlib.suppress(Exception):
                await event.client.send_message(event.chat_id, "❌ راهنما ارسال نشد؛ ربات باید در این چت قابل دسترس باشد.")
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
        await event.edit(f"✅ انتقال انجام شد.\n💎 {amount:,} الماس به کاربر `{target}` منتقل شد.\n💰 موجودی باقی‌مانده: {get_balance(uid):,}")
        with contextlib.suppress(Exception):
            await event.client.send_message(target, f"🎁 الماس دریافت کردید!\n\n💎 مقدار: {amount:,} الماس")
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

async def self_handle_incoming(event, uid):
    client = event.client

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

        now = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%H:%M")
        first = me.first_name or "کاربر"

        # Remove a previous [HH:MM] suffix generated by this bot.
        clean = re.sub(r"\s*(?:\[\d{1,2}:\d{2}\]|\d{1,2}:\d{2})\s*$", "", first).strip()
        new_first = f"{clean[:55]} {now}"

        if new_first != first:
            from telethon.tl.functions.account import UpdateProfileRequest
            await client(UpdateProfileRequest(first_name=new_first))
    except Exception as exc:
        # Function import is intentionally local to keep startup simple.
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
            await self_handle_outgoing(event, user_id)
        except Exception as exc:
            print(f"[SELF {user_id}] outgoing handler error: {exc}")

    @client.on(events.NewMessage(incoming=True))
    async def _self_incoming(event):
        try:
            await self_handle_incoming(event, user_id)
        except Exception as exc:
            print(f"[SELF {user_id}] incoming handler error: {exc}")

    try:
        await client.connect()

        if not await client.is_user_authorized():
            deactivate_session(user_id)
            print(f"[SELF {user_id}] session is no longer authorized")
            return

        self_workers[user_id] = asyncio.current_task()
        presence_task = asyncio.create_task(_presence_loop(client, user_id))
        print(f"[SELF {user_id}] started")

        last_name_update = 0

        while True:
            if not get_active_session(user_id):
                break

            balance = get_balance(user_id)
            if balance < 2:
                print(f"[SELF {user_id}] balance ended; stopping")
                deactivate_session(user_id)
                break

            now = time.time()
            if now - last_name_update >= 60:
                if time_name_enabled(user_id):
                    try:
                        from telethon.tl.functions.account import UpdateProfileRequest
                        me = await client.get_me()
                        if me:
                            current = me.first_name or "کاربر"
                            clean = re.sub(
                                r"\s*(?:\[\d{1,2}:\d{2}\]|\d{1,2}:\d{2})\s*$",
                                "",
                                current
                            ).strip()
                            clock = self_clock(user_id)
                            new_name = f"{clean[:55]} {clock}"
                            if current != new_name:
                                await client(
                                    UpdateProfileRequest(
                                        first_name=new_name
                                    )
                                )
                    except Exception as exc:
                        print(f"[SELF {user_id}] profile update: {exc}")
                last_name_update = now

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

            await asyncio.sleep(15)

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
            "کد را به صورت اعداد ارسال کن. اگر ورود دو مرحله‌ای فعال باشد، بعد از کد رمز دو مرحله‌ای را می‌پرسم."
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
            code = re.sub(r"\D", "", text)
            client = state.get("client")

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
        buttons = [[
            btn(
                f"💎 {balance:,}",
                f"balance_{target}".encode(),
                "primary"
            )
        ]]

        await event.reply(
            f"🎖️ **موجودی الماس**\n\n"
            f"👤 آیدی: `{target}`\n"
            f"💎 موجودی: `{balance:,}`",
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

        organizer = await user_name(user_id)
        text_game = (
            "⚔️ **نبرد الماس**\n\n"
            f"👤 برگزارکننده: {organizer}\n"
            f"💰 مبلغ: {amount:,} الماس\n"
            f"🏆 جایزه قبل از مالیات: {amount * 2:,} الماس\n\n"
            "برای ورود روی دکمه زیر بزنید."
        )

        buttons = [
            [btn(
                "⚔️ پیوستن به نبرد",
                f"game_join_{amount}_{user_id}".encode(), "success"
            )],
            [btn(
                "❌ لغو نبرد",
                f"game_cancel_{amount}_{user_id}".encode(), "danger"
            )]
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

        text = (
            "👤 **حساب کاربری**\n\n"
            f"🆔 آیدی: `{user_id}`\n"
            f"💎 موجودی: `{balance:,}` الماس\n"
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

        winner = random.choice([organizer, joiner])
        loser = joiner if winner == organizer else organizer

        change_balance(winner, prize)

        winner_name = await user_name(winner)
        loser_name = await user_name(loser)

        game = active_games.pop(key)
        game["task"].cancel()

        with contextlib.suppress(Exception):
            await bot.delete_messages(event.chat_id, event.message_id)

        await bot.send_message(
            event.chat_id,
            "◈ ━━━ ⚔️ نتیجه نبرد ━━━ ◈\n\n"
            f"🏆 برنده: {winner_name}\n"
            f"💀 بازنده: {loser_name}\n"
            f"💎 جایزه: {prize:,} الماس\n"
            f"🧾 مالیات: {tax:,} الماس\n\n"
            "◈ ━━━━━━━━━━━━━ ◈"
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

        with contextlib.suppress(Exception):
            await bot.send_message(
                target,
                f"✅ پرداخت شما تأیید شد.\n"
                f"💎 {diamonds:,} الماس به حساب شما اضافه شد."
            )

        await safe_answer(event, "✅ پرداخت تأیید شد.")
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

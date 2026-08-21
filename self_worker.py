# -*- coding: utf-8 -*-
"""Self-account worker for Diamond Bot.

This module intentionally contains no bot token and no separate database.
It uses bot.py's shared SQLite/settings/session helpers and runs one Telethon
user session per activated account inside the same Python process.
"""
import asyncio
import contextlib
import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest, SendReactionRequest, TranslateTextRequest
from telethon.tl.types import SendMessageTypingAction, ReactionEmoji, TextWithEntities


TZ = ZoneInfo("Asia/Tehran")
_workers = {}
_clients = {}
_reply_cache = set()


def _app():
    # Lazy import avoids circular import during bot.py startup.
    import bot
    return bot


def _get(uid, key, default=None):
    return _app().self_get(uid, key, default)


def _set(uid, key, value):
    return _app().self_set(uid, key, value)


def _clock(uid):
    return _app().self_clock(uid)


def _targets(uid):
    return _app().self_reaction_targets(uid)


def _save_targets(uid, targets):
    return _app().self_save_reaction_targets(uid, targets)


def _stretch(text):
    return _app().self_stretch(text)


def _transform_english(text, uid):
    return _app().self_transform_english(text, uid)


async def _translate(client, text):
    if not text:
        return None
    try:
        result = await client(TranslateTextRequest(
            to_lang="en",
            text=[TextWithEntities(text=text, entities=[])],
        ))
        if getattr(result, "result", None):
            translated = result.result[0].text
            return translated.strip() if translated else None
    except Exception as exc:
        print(f"[SELF] Telegram translation failed: {exc}")
    return None


async def _typing(client, event):
    with contextlib.suppress(Exception):
        await client(SetTypingRequest(
            peer=event.peer_id,
            action=SendMessageTypingAction(),
        ))


async def _react(client, event, emoji="❤️"):
    with contextlib.suppress(Exception):
        await client(SendReactionRequest(
            peer=event.peer_id,
            msg_id=event.id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))

def _clean_clock_name(first_name: str) -> str:
    # The self bot writes only a bare HH:MM suffix, never square brackets.
    return re.sub(r"\s*(?:\d{1,2}:\d{2})\s*$", "", first_name or "").strip()


async def _update_profile_clock(client, uid):
    if _get(uid, "time_name", "on") != "on":
        return
    try:
        me = await client.get_me()
        if not me:
            return
        base = _clean_clock_name(me.first_name or "کاربر")[:55]
        desired = f"{base} {_clock(uid)}"
        if me.first_name != desired:
            await client(functions.account.UpdateProfileRequest(first_name=desired))
    except Exception as exc:
        print(f"[SELF {uid}] clock update: {exc}")


async def handle_outgoing(event, uid):
    app = _app()
    text = (event.raw_text or "").strip()
    low = text.casefold()
    if not text:
        return

    # The user account sends these commands; the bot account sends the actual
    # inline panel because user accounts cannot create bot-style callback UI.
    if low == "پنل":
        with contextlib.suppress(Exception):
            await event.delete()
        try:
            await app.send_self_panel(event.chat_id, uid)
        except Exception as exc:
            print(f"[SELF {uid}] panel: {exc}")
        return

    if low == "راهنما":
        with contextlib.suppress(Exception):
            await event.delete()
        try:
            await app.send_self_guide(event.chat_id, uid)
        except Exception as exc:
            print(f"[SELF {uid}] guide: {exc}")
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
        key, value = switches[low]
        _set(uid, key, value)
        with contextlib.suppress(Exception):
            await event.edit(f"✅ {text}\nوضعیت: {'روشن' if value == 'on' else 'خاموش'}")
        return

    if low.startswith("تبچی متن"):
        value = text[len("تبچی متن"):].strip()
        if not value:
            await event.edit("❌ متن تبچی نمی‌تواند خالی باشد.")
            return
        _set(uid, "auto_reply_text", value)
        _set(uid, "auto_reply", "on")
        await event.edit("✅ متن تبچی ذخیره و فعال شد.")
        return

    if low.startswith("فونت ساعت"):
        raw = text[len("فونت ساعت"):].strip().casefold()
        value = app.SELF_FONT_ALIASES.get(raw, raw)
        if value not in app.SELF_CLOCK_FONTS:
            await event.edit("❌ فونت نامعتبر است.\n" + " / ".join(app.SELF_FONT_ALIASES))
            return
        _set(uid, "clock_font", value)
        await event.edit(app.self_font_preview(uid, "clock"), parse_mode="html")
        return

    if low.startswith("فونت انگلیسی"):
        raw = text[len("فونت انگلیسی"):].strip().casefold()
        value = app.SELF_ENGLISH_FONT_ALIASES.get(raw, raw)
        if value not in app.SELF_ENGLISH_FONTS:
            await event.edit("❌ فونت نامعتبر است.\n" + " / ".join(app.SELF_ENGLISH_FONT_ALIASES))
            return
        _set(uid, "english_font", value)
        await event.edit(app.self_font_preview(uid, "english"), parse_mode="html")
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
        targets = _targets(uid)
        targets.add(target)
        _save_targets(uid, targets)
        with contextlib.suppress(Exception):
            _app().self_set_reaction(uid, target, "❤️" if emoji == "❤" else emoji)
        await event.edit(f"✅ ریاکشن {emoji} برای کاربر `{target}` فعال شد.")
        return

    if low in {"حذف ریاکشن", "ریاکشن خاموش", "حذف ریاکشن ❤️", "حذف ریاکشن + ریپلای"}:
        if not event.is_reply:
            await event.edit("❌ روی پیام همان کاربر ریپلای کن.")
            return
        replied = await event.get_reply_message()
        if replied and replied.sender_id:
            target = int(replied.sender_id)
            targets = _targets(uid)
            targets.discard(target)
            _save_targets(uid, targets)
            with contextlib.suppress(Exception):
                _app().self_remove_reaction(uid, target)
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
        balance = app.get_balance(uid)
        if balance < amount:
            await event.edit(f"❌ موجودی کافی نیست.\n💎 موجودی شما: {balance:,}")
            return
        app.change_balance(uid, -amount)
        app.init_user_db(target)
        app.change_balance(target, amount)
        await event.edit(
            f"✅ انتقال انجام شد.\n💎 {amount:,} الماس به کاربر `{target}` منتقل شد.\n"
            f"💰 موجودی باقی‌مانده: {app.get_balance(uid):,}"
        )
        with contextlib.suppress(Exception):
            await event.client.send_message(target, f"🎁 الماس دریافت کردید!\n\n💎 مقدار: {amount:,} الماس")
        return

    # Commands starting with / are left untouched.
    if text.startswith(("/", ".")):
        return

    transformed = text
    changed = False

    if _get(uid, "translate", "off") == "on":
        translated = await _translate(event.client, transformed)
        if translated:
            transformed = translated
            changed = True

    if _get(uid, "persian_font", "off") == "on":
        transformed = _stretch(transformed)
        changed = True

    if _get(uid, "english_font", "normal") != "normal":
        transformed = _transform_english(transformed, uid)
        changed = True

    if _get(uid, "bold", "off") == "on":
        try:
            await event.edit(f"<strong>{html.escape(transformed)}</strong>", parse_mode="html")
        except Exception:
            pass
        return

    if changed:
        with contextlib.suppress(Exception):
            await event.edit(transformed)


async def handle_incoming(event, uid):
    if _get(uid, "auto_read", "off") == "on":
        with contextlib.suppress(Exception):
            await event.client.send_read_acknowledge(event.chat_id, max_id=event.id)

    sender_id = event.sender_id
    if sender_id and int(sender_id) in _targets(uid):
        emoji = "❤️"
        with contextlib.suppress(Exception):
            emoji = _app().self_reaction_map(uid).get(int(sender_id), "❤️")
        await _react(event.client, event, emoji)

    if (
        event.is_private
        and _get(uid, "auto_reply", "off") == "on"
        and sender_id
        and sender_id != uid
        and (int(uid), int(sender_id)) not in _reply_cache
    ):
        key = (int(uid), int(sender_id))
        _reply_cache.add(key)
        with contextlib.suppress(Exception):
            await event.respond(_get(uid, "auto_reply_text", "سلام، فعلاً در دسترس نیستم."))
        asyncio.create_task(_clear_reply_cache(key))

async def _clear_reply_cache(key):
    await asyncio.sleep(60)
    _reply_cache.discard(key)


async def _charge(uid):
    app = _app()
    session = app.get_active_session(uid)
    if not session:
        return False
    start_time = int(session[2])
    elapsed_hours = int((time.time() - start_time) // 3600)
    due_total = int(elapsed_hours * app.SELF_HOURLY_COST)
    charged_total = int(float(app.get_setting(uid, "charged_diamonds", "0") or 0))
    if due_total > charged_total:
        charge = due_total - charged_total
        with app.connect_db(uid) as db:
            db.execute(
                "UPDATE users SET balance=MAX(balance-?,0) WHERE user_id=?",
                (charge, uid),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('charged_diamonds',?)",
                (str(due_total),),
            )
        if app.get_balance(uid) <= 0:
            app.deactivate_session(uid)
            return False
    return True


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
            typing_on = _get(uid, "typing", "off") == "on"
            game_on = _get(uid, "game_mode", "off") == "on"
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

async def worker(uid: int, session_string: str, sub_type: int = 0):
    app = _app()
    client = TelegramClient(
        StringSession(session_string),
        app.API_ID,
        app.API_HASH,
        device_model="Diamond Self",
        system_version="Python",
        app_version="1.0",
        sequential_updates=True,
    )
    _clients[uid] = client
    app.self_clients[uid] = client
    presence_task = None

    @client.on(events.NewMessage(outgoing=True))
    async def outgoing_event(event):
        try:
            await handle_outgoing(event, uid)
        except Exception as exc:
            print(f"[SELF {uid}] outgoing handler: {exc}")

    @client.on(events.NewMessage(incoming=True))
    async def incoming_event(event):
        try:
            await handle_incoming(event, uid)
        except Exception as exc:
            print(f"[SELF {uid}] incoming handler: {exc}")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            app.deactivate_session(uid)
            print(f"[SELF {uid}] session unauthorized")
            return

        _workers[uid] = asyncio.current_task()
        app.self_workers[uid] = asyncio.current_task()
        presence_task = asyncio.create_task(_presence_loop(client, uid))
        print(f"[SELF {uid}] started")

        last_clock = 0.0
        while True:
            if not app.get_active_session(uid):
                break
            if app.get_balance(uid) < 1:
                app.deactivate_session(uid)
                break

            now = time.time()
            if now - last_clock >= 60:
                await _update_profile_clock(client, uid)
                last_clock = now

            if not await _charge(uid):
                break

            await asyncio.sleep(15)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[SELF {uid}] worker error: {exc}")
    finally:
        if presence_task:
            presence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await presence_task
        _workers.pop(uid, None)
        _clients.pop(uid, None)
        app.self_workers.pop(uid, None)
        app.self_clients.pop(uid, None)
        with contextlib.suppress(Exception):
            await client.disconnect()
        print(f"[SELF {uid}] stopped")


async def start_self_worker(user_id: int, session_string: str, sub_type: int = 0):
    old = _workers.get(user_id)
    if old and not old.done():
        old.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await old
    task = asyncio.create_task(worker(user_id, session_string, sub_type))
    _workers[user_id] = task
    # Keep bot.py's registry in sync for its management buttons.
    _app().self_workers[user_id] = task


async def stop_self_worker(user_id: int):
    app = _app()
    task = _workers.get(user_id) or app.self_workers.get(user_id)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _workers.pop(user_id, None)
    _clients.pop(user_id, None)
    app.self_workers.pop(user_id, None)
    app.self_clients.pop(user_id, None)
    app.deactivate_session(user_id)

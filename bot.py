#!/usr/bin/env python3
"""
IG Tracker Bot — Telegram control with Reply Keyboard
Login → track accounts → auto-comment on new posts.
"""

import os
import json
import asyncio
import logging
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    FeedbackRequired,
    PleaseWaitFewMinutes,
    UserNotFound,
)
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DEFAULT_POLL = int(os.getenv("POLL_SECONDS", "300"))
DEFAULT_COMMENT = os.getenv("COMMENT_TEXT", "🔥 first")

SESSION_DIR = BASE / "sessions"
DATA_DIR = BASE / "data"
SESSION_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

TARGETS_FILE = DATA_DIR / "targets.json"
SEEN_FILE = DATA_DIR / "seen.json"
CONFIG_FILE = DATA_DIR / "config.json"
SESSION_FILE = SESSION_DIR / "main.json"

WAIT_USERNAME, WAIT_PASSWORD = range(2)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("igbot")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso() -> str:
    return utcnow().isoformat()

def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

def safe_uname(u: str) -> str:
    return u.strip().lstrip("@").lower()

def short_caption(text: Optional[str], limit: int = 180) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")

# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Login"), KeyboardButton("Logout")],
        [KeyboardButton("Add Target"), KeyboardButton("Remove Target")],
        [KeyboardButton("List"), KeyboardButton("Status")],
        [KeyboardButton("Set Comment"), KeyboardButton("Auto Comment")],
        [KeyboardButton("How to use"), KeyboardButton("Help")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        raw = load_json(CONFIG_FILE, {})
        self.poll_seconds: int = int(raw.get("poll_seconds", DEFAULT_POLL))
        self.comment_text: str = raw.get("comment_text", DEFAULT_COMMENT)
        self.auto_comment: bool = bool(raw.get("auto_comment", True))
        self.owner_chat_id: int = int(raw.get("owner_chat_id", CHAT_ID))

    def save(self) -> None:
        save_json(CONFIG_FILE, {
            "poll_seconds": self.poll_seconds,
            "comment_text": self.comment_text,
            "auto_comment": self.auto_comment,
            "owner_chat_id": self.owner_chat_id,
        })

cfg = Config()

# ---------------------------------------------------------------------------
# Instagram session
# ---------------------------------------------------------------------------
class IGSession:
    def __init__(self):
        self.cl = Client()
        self.cl.delay_range = [2, 5]
        self.logged_in = False
        self.username: Optional[str] = None

    def load(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            self.cl.load_settings(SESSION_FILE)
            self.cl.get_timeline_feed()
            self.logged_in = True
            self.username = self.cl.username
            log.info("session restored: %s", self.username)
            return True
        except LoginRequired:
            return False
        except Exception as e:
            log.warning("session load failed: %s", e)
            return False

    def login(self, username: str, password: str) -> Dict[str, Any]:
        try:
            self.cl.login(username, password)
            self.cl.dump_settings(SESSION_FILE)
            self.logged_in = True
            self.username = username
            return {"ok": True, "username": username}
        except ChallengeRequired:
            return {"ok": False, "error": "challenge_required"}
        except PleaseWaitFewMinutes:
            return {"ok": False, "error": "rate_limited"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def logout(self) -> None:
        try:
            self.cl.logout()
        except Exception:
            pass
        if SESSION_FILE.exists():
            SESSION_FILE.unlink(missing_ok=True)
        self.logged_in = False
        self.username = None

    def user_id(self, uname: str) -> Optional[int]:
        try:
            return self.cl.user_id_from_username(safe_uname(uname))
        except UserNotFound:
            return None
        except Exception:
            return None

    def medias(self, uid: int, amount: int = 6) -> List[Any]:
        try:
            return self.cl.user_medias(uid, amount)
        except Exception as e:
            log.warning("medias error: %s", e)
            return []

    def comment(self, media_pk: int, text: str) -> Dict[str, Any]:
        try:
            mid = self.cl.media_id(media_pk)
            self.cl.media_comment(mid, text)
            return {"ok": True}
        except FeedbackRequired as e:
            return {"ok": False, "error": "feedback_required", "detail": str(e)}
        except PleaseWaitFewMinutes:
            return {"ok": False, "error": "rate_limited"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

ig = IGSession()

# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------
class Tracker:
    def __init__(self):
        self.targets: List[str] = load_json(TARGETS_FILE, [])
        self.seen: Dict[str, str] = load_json(SEEN_FILE, {})

    def save_targets(self) -> None:
        save_json(TARGETS_FILE, self.targets)

    def save_seen(self) -> None:
        save_json(SEEN_FILE, self.seen)

    def add_target(self, uname: str) -> Dict[str, Any]:
        uname = safe_uname(uname)
        if uname in self.targets:
            return {"ok": False, "error": "already_tracked", "username": uname}
        if not ig.logged_in:
            return {"ok": False, "error": "not_logged_in"}
        uid = ig.user_id(uname)
        if uid is None:
            return {"ok": False, "error": "user_not_found", "username": uname}
        self.targets.append(uname)
        self.save_targets()
        for m in ig.medias(uid, 6):
            self.seen[str(m.pk)] = iso()
        self.save_seen()
        return {"ok": True, "username": uname}

    def remove_target(self, uname: str) -> Dict[str, Any]:
        uname = safe_uname(uname)
        if uname not in self.targets:
            return {"ok": False, "error": "not_tracked", "username": uname}
        self.targets.remove(uname)
        self.save_targets()
        return {"ok": True, "username": uname}

    def list_targets(self) -> List[str]:
        return list(self.targets)

    async def check_new(self) -> List[Dict[str, Any]]:
        if not ig.logged_in:
            return []
        news = []
        for uname in list(self.targets):
            try:
                uid = ig.user_id(uname)
                if uid is None:
                    continue
                for m in ig.medias(uid, 6):
                    pk = str(m.pk)
                    if pk in self.seen:
                        continue
                    self.seen[pk] = iso()
                    news.append({
                        "username": uname,
                        "pk": m.pk,
                        "code": m.code,
                        "likes": getattr(m, "like_count", 0) or 0,
                        "comments": getattr(m, "comment_count", 0) or 0,
                        "caption": short_caption(getattr(m, "caption_text", None)),
                        "url": f"https://www.instagram.com/p/{m.code}/",
                        "product_type": getattr(m, "product_type", None),
                        "media_type": getattr(m, "media_type", None),
                    })
            except Exception as e:
                news.append({"error": f"{uname}: {e}"})
        if news:
            self.save_seen()
        return news

tracker = Tracker()
login_state: Dict[int, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Owner check
# ---------------------------------------------------------------------------
def is_owner(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return uid == cfg.owner_chat_id or (CHAT_ID and uid == CHAT_ID)

async def deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("Owner only.")

# ---------------------------------------------------------------------------
# How to use text
# ---------------------------------------------------------------------------
ABOUT_TEXT = """\
<b>How to use this bot</b>

<b>1. Login your Instagram account</b>
Press <b>Login</b>
Bot will ask for your username, then your password.
The password message is automatically deleted for safety.
Your session is saved, so you do not need to login every time.
To remove the session, press <b>Logout</b>.

<b>2. Add accounts to track</b>
Press <b>Add Target</b> and send the username.
Example: cool.moco
You can track multiple accounts at the same time.
The bot remembers recent posts, so it only reacts to new ones.

<b>3. View tracked accounts</b>
Press <b>List</b> to see all accounts you are currently tracking.

<b>4. Remove a tracked account</b>
Press <b>Remove Target</b> and send the username you want to stop tracking.

<b>5. Set your comment text</b>
Press <b>Set Comment</b> and send the text you want the bot to post.
Example: then i walk in 💀

<b>6. Turn auto-comment on or off</b>
Press <b>Auto Comment</b> and send <code>on</code> or <code>off</code>.

When any tracked account uploads a new post or reel, the bot will:
• notify you with the post link and caption
• automatically comment with your set text (if auto-comment is ON)

That is the complete flow.

For any help contact @pvnuo
"""

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    text = (
        "<b>IG Tracker Bot</b>\n\n"
        "Use the buttons below.\n"
        "Or type commands if you prefer.\n\n"
        "Press 👉 /help"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    text = (
        "<b>Commands</b>\n\n"
        "/login — login Instagram account\n"
        "/logout — remove session\n"
        "/add username — track an account\n"
        "/remove username — stop tracking\n"
        "/list — show tracked accounts\n"
        "/setcomment text — set comment text\n"
        "/autocomment on|off — turn auto-comment on/off\n"
        "/status — current status\n"
        "/about — how to use the bot\n"
        "/help — this menu"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    await update.message.reply_text(ABOUT_TEXT, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        await deny(update)
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    login_state[chat_id] = {}
    await update.message.reply_text(
        "Send your Instagram <b>username</b> (without @).\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
    )
    return WAIT_USERNAME

async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    uname = safe_uname(update.message.text or "")
    if not uname:
        await update.message.reply_text("Invalid username. Try again or /cancel")
        return WAIT_USERNAME
    login_state[chat_id] = {"username": uname}
    await update.message.reply_text(
        f"Username: <code>{uname}</code>\n"
        "Now send the <b>password</b>.\n"
        "(Message will be deleted after reading.)",
        parse_mode="HTML",
    )
    return WAIT_PASSWORD

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    state = login_state.get(chat_id, {})
    uname = state.get("username")
    password = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not uname or not password:
        await context.bot.send_message(chat_id, "Missing data. Use Login again.")
        login_state.pop(chat_id, None)
        return ConversationHandler.END

    await context.bot.send_message(chat_id, "Logging in…")
    result = ig.login(uname, password)
    login_state.pop(chat_id, None)
    if result.get("ok"):
        await context.bot.send_message(
            chat_id,
            f"✅ Logged in as <b>@{result['username']}</b>\n"
            "Session saved. You can add targets now.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        err = result.get("error", "unknown")
        if err == "challenge_required":
            await context.bot.send_message(chat_id, "⚠️ Challenge required. Check Instagram app/email and try Login later.")
        elif err == "rate_limited":
            await context.bot.send_message(chat_id, "⏳ Rate limited. Wait a few minutes then try Login again.")
        else:
            await context.bot.send_message(chat_id, f"❌ Login failed: {err}")
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    login_state.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    ig.logout()
    await update.message.reply_text("Session removed.", reply_markup=MAIN_KEYBOARD)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /add username\nOr press Add Target then send the username.")
        return
    res = tracker.add_target(context.args[0])
    if res.get("ok"):
        await update.message.reply_text(f"✅ Now tracking @{res['username']}", reply_markup=MAIN_KEYBOARD)
    elif res.get("error") == "already_tracked":
        await update.message.reply_text(f"Already tracking @{res['username']}")
    elif res.get("error") == "not_logged_in":
        await update.message.reply_text("Not logged in. Press Login first.")
    elif res.get("error") == "user_not_found":
        await update.message.reply_text(f"User @{res['username']} not found.")
    else:
        await update.message.reply_text(f"Error: {res.get('error')}")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove username\nOr press Remove Target then send the username.")
        return
    res = tracker.remove_target(context.args[0])
    if res.get("ok"):
        await update.message.reply_text(f"Removed @{res['username']}", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(f"Not tracking @{res.get('username')}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    targets = tracker.list_targets()
    if not targets:
        await update.message.reply_text("No accounts being tracked.", reply_markup=MAIN_KEYBOARD)
        return
    lines = [f"• @{t}" for t in targets]
    await update.message.reply_text(
        f"<b>Tracking ({len(targets)})</b>\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_setcomment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            f"Current: <code>{cfg.comment_text}</code>\n"
            "Send: /setcomment your text here",
            parse_mode="HTML",
        )
        return
    cfg.comment_text = " ".join(context.args)
    cfg.save()
    await update.message.reply_text(
        f"Set → <code>{cfg.comment_text}</code>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_autocomment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        state = "ON" if cfg.auto_comment else "OFF"
        await update.message.reply_text(
            f"Auto-comment: <b>{state}</b>\n"
            "Send: /autocomment on   or   /autocomment off",
            parse_mode="HTML",
        )
        return
    cfg.auto_comment = context.args[0].lower() == "on"
    cfg.save()
    await update.message.reply_text(
        f"Auto-comment → {'ON' if cfg.auto_comment else 'OFF'}",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return
    session = f"✅ @{ig.username}" if ig.logged_in else "❌ not logged in"
    targets = tracker.list_targets()
    text = (
        f"<b>Status</b>\n"
        f"Session: {session}\n"
        f"Tracked accounts: {len(targets)}\n"
        f"Comment: <code>{cfg.comment_text}</code>\n"
        f"Auto-comment: {'ON' if cfg.auto_comment else 'OFF'}\n"
        f"Poll every: {cfg.poll_seconds}s"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)

# ---------------------------------------------------------------------------
# Button handler
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await deny(update)
        return

    if update.effective_chat.id in login_state:
        return

    text = (update.message.text or "").strip()

    if text == "Login":
        await cmd_login(update, context)
    elif text == "Logout":
        await cmd_logout(update, context)
    elif text == "List":
        await cmd_list(update, context)
    elif text == "Status":
        await cmd_status(update, context)
    elif text == "How to use":
        await cmd_about(update, context)
    elif text == "Help":
        await cmd_help(update, context)
    elif text == "Add Target":
        await update.message.reply_text("Send the username you want to track.\nExample: cool.moco")
        context.user_data["expect"] = "add"
    elif text == "Remove Target":
        await update.message.reply_text("Send the username you want to remove.\nExample: cool.moco")
        context.user_data["expect"] = "remove"
    elif text == "Set Comment":
        await update.message.reply_text(
            f"Current: <code>{cfg.comment_text}</code>\n"
            "Send the new comment text now.",
            parse_mode="HTML",
        )
        context.user_data["expect"] = "setcomment"
    elif text == "Auto Comment":
        state = "ON" if cfg.auto_comment else "OFF"
        await update.message.reply_text(
            f"Auto-comment is currently <b>{state}</b>\n"
            "Send <code>on</code> or <code>off</code>",
            parse_mode="HTML",
        )
        context.user_data["expect"] = "autocomment"
    else:
        expect = context.user_data.get("expect")
        if expect == "add":
            context.user_data.pop("expect", None)
            context.args = [text]
            await cmd_add(update, context)
        elif expect == "remove":
            context.user_data.pop("expect", None)
            context.args = [text]
            await cmd_remove(update, context)
        elif expect == "setcomment":
            context.user_data.pop("expect", None)
            context.args = text.split()
            await cmd_setcomment(update, context)
        elif expect == "autocomment":
            context.user_data.pop("expect", None)
            context.args = [text.lower()]
            await cmd_autocomment(update, context)

# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------
async def poll_loop(app: Application) -> None:
    log.info("poll loop started")
    while True:
        try:
            if not ig.logged_in:
                await asyncio.sleep(15)
                continue
            items = await tracker.check_new()
            for item in items:
                if "error" in item:
                    await app.bot.send_message(cfg.owner_chat_id, f"⚠ {item['error']}")
                    continue
                tag = "🎬 REEL" if item.get("product_type") == "clips" else "🖼 PHOTO" if item.get("media_type") == 1 else "📌 POST"
                msg = (
                    f"🆕 <b>@{item['username']}</b> {tag}\n"
                    f"❤ {item['likes']}  💬 {item['comments']}\n"
                    f"{item['caption']}\n"
                    f"{item['url']}"
                )
                await app.bot.send_message(cfg.owner_chat_id, msg, parse_mode="HTML")
                if cfg.auto_comment:
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                    res = ig.comment(item["pk"], cfg.comment_text)
                    if res.get("ok"):
                        await app.bot.send_message(
                            cfg.owner_chat_id,
                            f"✅ commented → <code>{cfg.comment_text}</code>",
                            parse_mode="HTML",
                        )
                    else:
                        await app.bot.send_message(
                            cfg.owner_chat_id,
                            f"❌ comment fail: {res.get('error')}",
                        )
        except Exception as e:
            log.exception("poll")
            try:
                await app.bot.send_message(cfg.owner_chat_id, f"poll err: {e}")
            except Exception:
                pass
        await asyncio.sleep(cfg.poll_seconds)

async def post_init(app: Application) -> None:
    if ig.load():
        log.info("restored session for %s", ig.username)
    app.create_task(poll_loop(app))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_TOKEN missing in .env")

    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_read_timeout(42.0)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            WAIT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("setcomment", cmd_setcomment))
    app.add_handler(CommandHandler("autocomment", cmd_autocomment))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

    log.info("bot starting")
    print("bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
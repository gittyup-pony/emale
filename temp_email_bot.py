import asyncio
import logging
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Configuration ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"   # From @BotFather
EMAIL_DOMAIN   = "mail.tm"                   # mail.tm default domain
POLL_INTERVAL  = 15                          # seconds between inbox checks
EXPIRY_SECONDS = 600                         # 10 minutes
WARNING_SECONDS = 480                        # warn at 8 minutes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAILTM_BASE = "https://api.mail.tm"

# ── Per-user session storage ──────────────────────────────────────────────────
# sessions[chat_id] = {
#   "email": str, "password": str, "token": str,
#   "account_id": str, "seen_ids": set,
#   "created_at": datetime, "tasks": [asyncio.Task, ...]
# }
sessions: dict[int, dict] = {}


# ── mail.tm helpers ───────────────────────────────────────────────────────────

async def get_domain(client: httpx.AsyncClient) -> str:
    """Return the first available mail.tm domain."""
    r = await client.get(f"{MAILTM_BASE}/domains")
    r.raise_for_status()
    return r.json()["hydra:member"][0]["domain"]


async def create_account(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Register a mailbox and return its account id."""
    r = await client.post(f"{MAILTM_BASE}/accounts", json={"address": email, "password": password})
    r.raise_for_status()
    return r.json()["id"]


async def get_token(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Authenticate and return a JWT token."""
    r = await client.post(f"{MAILTM_BASE}/token", json={"address": email, "password": password})
    r.raise_for_status()
    return r.json()["token"]


async def fetch_messages(client: httpx.AsyncClient, token: str) -> list[dict]:
    """Fetch the inbox message list."""
    r = await client.get(
        f"{MAILTM_BASE}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json().get("hydra:member", [])


async def fetch_message_body(client: httpx.AsyncClient, token: str, msg_id: str) -> str:
    """Fetch the full text body of a single message."""
    r = await client.get(
        f"{MAILTM_BASE}/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    data = r.json()
    return data.get("text") or data.get("intro") or "(no text content)"


async def delete_account(client: httpx.AsyncClient, token: str, account_id: str) -> None:
    """Permanently delete a mailbox."""
    await client.delete(
        f"{MAILTM_BASE}/accounts/{account_id}",
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Session lifecycle ─────────────────────────────────────────────────────────

def cancel_session_tasks(chat_id: int) -> None:
    session = sessions.get(chat_id)
    if session:
        for t in session.get("tasks", []):
            t.cancel()


async def destroy_session(chat_id: int, app: Application, reason: str) -> None:
    """Delete the mailbox, cancel tasks, and notify the user."""
    session = sessions.pop(chat_id, None)
    if not session:
        return

    cancel_session_tasks(chat_id)

    async with httpx.AsyncClient() as client:
        try:
            await delete_account(client, session["token"], session["account_id"])
        except Exception as e:
            logger.warning("Could not delete account: %s", e)

    await app.bot.send_message(
        chat_id,
        f"🗑 *{session['email']}* has been deleted.\n_{reason}_\n\nUse /new to create a fresh address.",
        parse_mode="Markdown",
    )


# ── Background tasks ──────────────────────────────────────────────────────────

async def poll_inbox(chat_id: int, app: Application) -> None:
    """Continuously poll inbox and forward new emails to the user."""
    session = sessions.get(chat_id)
    if not session:
        return

    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            session = sessions.get(chat_id)
            if not session:
                break
            try:
                messages = await fetch_messages(client, session["token"])
                for msg in messages:
                    mid = msg["id"]
                    if mid in session["seen_ids"]:
                        continue
                    session["seen_ids"].add(mid)
                    body = await fetch_message_body(client, session["token"], mid)
                    text = (
                        f"📬 *New Email!*\n"
                        f"*From:* {msg['from']['address']}\n"
                        f"*Subject:* {msg.get('subject', '(no subject)')}\n\n"
                        f"{body[:3000]}"  # Telegram message limit guard
                    )
                    await app.bot.send_message(chat_id, text, parse_mode="Markdown")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Polling error for %d: %s", chat_id, e)


async def expiry_warning(chat_id: int, app: Application) -> None:
    """Send a warning 2 minutes before expiry."""
    await asyncio.sleep(WARNING_SECONDS)
    if chat_id not in sessions:
        return
    await app.bot.send_message(
        chat_id,
        "⚠️ Your temp email expires in *2 minutes*. Use /extend to add 10 more minutes.",
        parse_mode="Markdown",
    )


async def expiry_delete(chat_id: int, app: Application) -> None:
    """Auto-delete the mailbox after EXPIRY_SECONDS."""
    await asyncio.sleep(EXPIRY_SECONDS)
    if chat_id not in sessions:
        return
    await destroy_session(chat_id, app, "Address expired after 10 minutes.")


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # Clean up any existing session first
    if chat_id in sessions:
        cancel_session_tasks(chat_id)
        async with httpx.AsyncClient() as client:
            try:
                s = sessions.pop(chat_id)
                await delete_account(client, s["token"], s["account_id"])
            except Exception:
                pass

    await update.message.reply_text("⏳ Creating your temp email address…")

    import random, string
    password = "".join(random.choices(string.ascii_letters + string.digits, k=16))

    try:
        async with httpx.AsyncClient() as client:
            domain     = await get_domain(client)
            username   = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            email      = f"{username}@{domain}"
            account_id = await create_account(client, email, password)
            token      = await get_token(client, email, password)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to create address: {e}")
        return

    sessions[chat_id] = {
        "email": email,
        "password": password,
        "token": token,
        "account_id": account_id,
        "seen_ids": set(),
        "created_at": datetime.utcnow(),
        "tasks": [],
    }

    # Schedule background tasks
    loop = asyncio.get_event_loop()
    tasks = [
        loop.create_task(poll_inbox(chat_id, ctx.application)),
        loop.create_task(expiry_warning(chat_id, ctx.application)),
        loop.create_task(expiry_delete(chat_id, ctx.application)),
    ]
    sessions[chat_id]["tasks"] = tasks

    await update.message.reply_text(
        f"✅ *Your temp email is ready!*\n\n"
        f"`{email}`\n\n"
        f"⏱ Auto-deletes in *10 minutes*.\n"
        f"📬 I'll forward any incoming emails to you automatically.\n\n"
        f"Commands: /inbox /extend /delete",
        parse_mode="Markdown",
    )


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active address. Use /new to create one.")
        return

    async with httpx.AsyncClient() as client:
        try:
            messages = await fetch_messages(client, session["token"])
        except Exception as e:
            await update.message.reply_text(f"❌ Could not fetch inbox: {e}")
            return

    if not messages:
        await update.message.reply_text("📭 Inbox is empty.")
        return

    for msg in messages:
        mid = msg["id"]
        session["seen_ids"].add(mid)
        body = await fetch_message_body(client, session["token"], mid)
        text = (
            f"📬 *Email*\n"
            f"*From:* {msg['from']['address']}\n"
            f"*Subject:* {msg.get('subject', '(no subject)')}\n\n"
            f"{body[:3000]}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active address. Use /new to create one.")
        return

    # Cancel existing timers and restart them
    for t in session.get("tasks", []):
        t.cancel()

    loop = asyncio.get_event_loop()
    tasks = [
        loop.create_task(poll_inbox(chat_id, ctx.application)),
        loop.create_task(expiry_warning(chat_id, ctx.application)),
        loop.create_task(expiry_delete(chat_id, ctx.application)),
    ]
    session["tasks"] = tasks
    session["created_at"] = datetime.utcnow()

    await update.message.reply_text(
        f"⏱ Timer reset! `{session['email']}` will now expire in another *10 minutes*.",
        parse_mode="Markdown",
    )


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in sessions:
        await update.message.reply_text("No active address to delete.")
        return
    await destroy_session(chat_id, ctx.application, "Manually deleted by you.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await update.message.reply_text("No active address. Use /new to create one.")
        return

    elapsed = (datetime.utcnow() - session["created_at"]).seconds
    remaining = max(0, EXPIRY_SECONDS - elapsed)
    mins, secs = divmod(remaining, 60)

    await update.message.reply_text(
        f"📧 *Active address:* `{session['email']}`\n"
        f"⏱ *Expires in:* {mins}m {secs}s\n"
        f"📬 *Emails received:* {len(session['seen_ids'])}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Temp Email Bot*\n\n"
        "/new — Generate a new temp email address\n"
        "/inbox — Manually check your inbox\n"
        "/extend — Reset the 10-minute expiry timer\n"
        "/status — Show address info and time remaining\n"
        "/delete — Immediately delete your address\n"
        "/help — Show this message\n\n"
        "Emails are forwarded to you automatically as they arrive.",
        parse_mode="Markdown",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("new",    cmd_new))
    app.add_handler(CommandHandler("inbox",  cmd_inbox))
    app.add_handler(CommandHandler("extend", cmd_extend))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("start",  cmd_help))

    logger.info("Bot is running…")
    app.run_polling()


if __name__ == "__main__":
    main()

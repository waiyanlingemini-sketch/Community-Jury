import logging
import os
import sqlite3
from pathlib import Path
from contextlib import closing
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, Poll
from telegram.ext import (
    Application, ContextTypes, MessageHandler, PollAnswerHandler,
    ChatMemberHandler, filters,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("community-jury")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
DISCUSSION_GROUP_ID = int(os.environ["DISCUSSION_GROUP_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DB_PATH = os.getenv("DB_PATH", "/tmp/jury.sqlite3")
WEBHOOK_PATH = "/telegram/webhook"

# Threshold is based on the channel member count at the time each post appears.
THRESHOLDS = [
    (300, 20),
    (500, 30),
    (1000, 50),
    (3000, 100),
    (5000, 150),
    (10**18, 200),
]

app = FastAPI()
tg_app: Optional[Application] = None


def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as con:
        con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS posts (
            channel_id INTEGER NOT NULL,
            channel_message_id INTEGER NOT NULL,
            discussion_message_id INTEGER,
            poll_id TEXT,
            threshold INTEGER NOT NULL,
            member_count INTEGER NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(channel_id, channel_message_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_poll ON posts(poll_id);
        CREATE TABLE IF NOT EXISTS votes (
            poll_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(poll_id, user_id)
        );
        """)
        con.commit()


def threshold_for(member_count: int) -> int:
    # Optional temporary test override. Set DELETE_VOTE_THRESHOLD=3 in Render
    # to test with three distinct accounts; remove it to restore scaling rules.
    test_override = os.getenv("DELETE_VOTE_THRESHOLD", "").strip()
    if test_override:
        try:
            value = int(test_override)
            if value >= 1:
                return value
        except ValueError:
            log.warning("Ignoring invalid DELETE_VOTE_THRESHOLD=%r", test_override)

    for ceiling, required in THRESHOLDS:
        if member_count <= ceiling:
            return required
    return 200


async def maybe_delete_post(context: ContextTypes.DEFAULT_TYPE, poll_id: str):
    with closing(db()) as con:
        post = con.execute(
            "SELECT * FROM posts WHERE poll_id=?", (poll_id,)
        ).fetchone()
        if not post or post["deleted"]:
            return
        count = con.execute(
            "SELECT COUNT(*) FROM votes WHERE poll_id=? AND selected=1",
            (poll_id,),
        ).fetchone()[0]
        if count < post["threshold"]:
            return

        # Mark first to prevent duplicate delete attempts from concurrent updates.
        con.execute(
            "UPDATE posts SET deleted=1 WHERE poll_id=? AND deleted=0",
            (poll_id,),
        )
        con.commit()

    try:
        await context.bot.delete_message(
            chat_id=post["channel_id"],
            message_id=post["channel_message_id"],
        )
        log.info(
            "Deleted channel post %s after %s removal votes (threshold %s)",
            post["channel_message_id"], count, post["threshold"],
        )
    except Exception:
        # Allow a retry if Telegram rejected the deletion.
        with closing(db()) as con:
            con.execute("UPDATE posts SET deleted=0 WHERE poll_id=?", (poll_id,))
            con.commit()
        log.exception("Could not delete channel post; check bot admin permissions")


async def on_discussion_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != DISCUSSION_GROUP_ID:
        return
    if not msg.is_automatic_forward:
        return

    origin = msg.forward_origin
    if not origin or origin.type != "channel":
        return
    if origin.chat.id != CHANNEL_ID:
        return

    channel_message_id = origin.message_id
    member_count = await context.bot.get_chat_member_count(CHANNEL_ID)
    threshold = threshold_for(member_count)

    # Avoid creating duplicate polls if Telegram retries an update.
    with closing(db()) as con:
        existing = con.execute(
            "SELECT poll_id FROM posts WHERE channel_id=? AND channel_message_id=?",
            (CHANNEL_ID, channel_message_id),
        ).fetchone()
        if existing:
            return
        con.execute(
            """INSERT OR REPLACE INTO posts
               (channel_id, channel_message_id, discussion_message_id, threshold, member_count)
               VALUES (?, ?, ?, ?, ?)""",
            (CHANNEL_ID, channel_message_id, msg.message_id, threshold, member_count),
        )
        con.commit()

    poll_message = await context.bot.send_poll(
        chat_id=DISCUSSION_GROUP_ID,
        question="ဒီ Channel Post ကို ဖျက်သင့်ပါသလား။",
        options=["👎 ဖျက်သင့်တယ်"],
        is_anonymous=False,
        allows_multiple_answers=False,
        reply_to_message_id=msg.message_id,
    )
    poll = poll_message.poll
    with closing(db()) as con:
        con.execute(
            """UPDATE posts SET poll_id=?
               WHERE channel_id=? AND channel_message_id=?""",
            (poll.id, CHANNEL_ID, channel_message_id),
        )
        con.commit()

    log.info(
        "Created poll %s for channel post %s; members=%s threshold=%s",
        poll.id, channel_message_id, member_count, threshold,
    )


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer or not answer.user:
        return

    # One-option poll: option_ids contains [0] when selected, [] if the vote is withdrawn.
    selected = 1 if 0 in answer.option_ids else 0
    with closing(db()) as con:
        known = con.execute(
            "SELECT 1 FROM posts WHERE poll_id=?", (answer.poll_id,)
        ).fetchone()
        if not known:
            return
        con.execute(
            """INSERT INTO votes(poll_id, user_id, selected) VALUES (?, ?, ?)
               ON CONFLICT(poll_id, user_id)
               DO UPDATE SET selected=excluded.selected""",
            (answer.poll_id, answer.user.id, selected),
        )
        con.commit()

    await maybe_delete_post(context, answer.poll_id)


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This handler is intentionally only a health/logging hook.
    # The poll is created from the Discussion Group's automatic-forward message,
    # which provides the exact comment-thread message to reply to.
    post = update.channel_post
    if post and post.chat_id == CHANNEL_ID:
        log.info("Observed channel post %s", post.message_id)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Membership changes are logged. Threshold is snapshotted per post.
    log.info("Chat membership update received")


async def startup():
    global tg_app
    init_db()
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, on_discussion_message))
    tg_app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    tg_app.add_handler(PollAnswerHandler(on_poll_answer))
    tg_app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.bot.set_webhook(
        url=WEBHOOK_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=["message", "channel_post", "poll_answer", "chat_member"],
        drop_pending_updates=False,
    )
    log.info("Bot started; webhook set to %s", WEBHOOK_URL + WEBHOOK_PATH)


async def shutdown():
    global tg_app
    if tg_app:
        await tg_app.bot.delete_webhook()
        await tg_app.stop()
        await tg_app.shutdown()
        tg_app = None


@app.on_event("startup")
async def _startup():
    await startup()


@app.on_event("shutdown")
async def _shutdown():
    await shutdown()


@app.get("/")
async def health():
    return {"status": "ok", "service": "community-jury-bot"}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    update = Update.de_json(payload, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

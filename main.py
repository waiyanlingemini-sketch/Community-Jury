import logging
import os
import sqlite3
from pathlib import Path
from contextlib import closing
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    ChatMemberHandler,
    filters,
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
        con.executescript(
            """
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

            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_poll
            ON posts(poll_id);

            CREATE TABLE IF NOT EXISTS votes (
                poll_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                selected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(poll_id, user_id)
            );
            """
        )
        con.commit()


def threshold_for(member_count: int) -> int:
    for ceiling, required in THRESHOLDS:
        if member_count <= ceiling:
            return required
    return 200


async def maybe_delete_post(context: ContextTypes.DEFAULT_TYPE, poll_id: str):
    with closing(db()) as con:
        post = con.execute(
            "SELECT * FROM posts WHERE poll_id=?",
            (poll_id,),
        ).fetchone()

        if not post or post["deleted"]:
            return

        count = con.execute(
            "SELECT COUNT(*) FROM votes WHERE poll_id=? AND selected=1",
            (poll_id,),
        ).fetchone()[0]

        if count < post["threshold"]:
            return

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
            post["channel_message_id"],
            count,
            post["threshold"],
        )
    except Exception:
        with closing(db()) as con:
            con.execute(
                "UPDATE posts SET deleted=0 WHERE poll_id=?",
                (poll_id,),
            )
            con.commit()
        log.exception("Could not delete channel post; check bot admin permissions")


async def on_discussion_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    msg = update.effective_message

    if not msg:
        log.warning("Received message update without effective_message")
        return

    log.info(
        "Message received: chat_id=%s message_id=%s automatic_forward=%s "
        "forward_origin_type=%s",
        msg.chat_id,
        msg.message_id,
        msg.is_automatic_forward,
        getattr(msg.forward_origin, "type", None),
    )

    if msg.chat_id != DISCUSSION_GROUP_ID:
        return

    # Only Telegram's automatic channel-to-discussion forward should trigger a poll.
    if not msg.is_automatic_forward:
        log.info(
            "Ignored group message %s: it is not an automatic channel forward",
            msg.message_id,
        )
        return

    origin = msg.forward_origin
    if not origin or origin.type != "channel":
        log.warning(
            "Automatic forward %s has no channel forward_origin; no poll created",
            msg.message_id,
        )
        return

    if origin.chat.id != CHANNEL_ID:
        log.warning(
            "Forward source mismatch: expected channel_id=%s, got=%s",
            CHANNEL_ID,
            origin.chat.id,
        )
        return

    channel_message_id = origin.message_id

    # If a poll already exists for this channel post, do not create a duplicate.
    with closing(db()) as con:
        existing = con.execute(
            """
            SELECT poll_id FROM posts
            WHERE channel_id=? AND channel_message_id=?
            """,
            (CHANNEL_ID, channel_message_id),
        ).fetchone()

    if existing and existing["poll_id"]:
        log.info(
            "Poll already exists for channel post %s; skipping duplicate forward",
            channel_message_id,
        )
        return

    try:
        member_count = await context.bot.get_chat_member_count(CHANNEL_ID)
        threshold = threshold_for(member_count)

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
                """
                INSERT INTO posts (
                    channel_id,
                    channel_message_id,
                    discussion_message_id,
                    poll_id,
                    threshold,
                    member_count
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, channel_message_id)
                DO UPDATE SET
                    discussion_message_id=excluded.discussion_message_id,
                    poll_id=excluded.poll_id,
                    threshold=excluded.threshold,
                    member_count=excluded.member_count,
                    deleted=0
                """,
                (
                    CHANNEL_ID,
                    channel_message_id,
                    msg.message_id,
                    poll.id,
                    threshold,
                    member_count,
                ),
            )
            con.commit()

        log.info(
            "POLL CREATED successfully: poll_id=%s channel_post=%s "
            "discussion_message=%s members=%s threshold=%s",
            poll.id,
            channel_message_id,
            msg.message_id,
            member_count,
            threshold,
        )

    except Exception:
        log.exception(
            "FAILED to create poll for channel post %s in discussion group %s. "
            "Check bot group permissions, linked discussion group, and channel access.",
            channel_message_id,
            DISCUSSION_GROUP_ID,
        )


async def on_poll_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    answer = update.poll_answer
    if not answer or not answer.user:
        return

    selected = 1 if 0 in answer.option_ids else 0

    with closing(db()) as con:
        known = con.execute(
            "SELECT 1 FROM posts WHERE poll_id=?",
            (answer.poll_id,),
        ).fetchone()

        if not known:
            log.warning("Received vote for unknown poll_id=%s", answer.poll_id)
            return

        con.execute(
            """
            INSERT INTO votes(poll_id, user_id, selected)
            VALUES (?, ?, ?)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET selected=excluded.selected
            """,
            (answer.poll_id, answer.user.id, selected),
        )
        con.commit()

    log.info(
        "Vote recorded: poll_id=%s user_id=%s selected=%s",
        answer.poll_id,
        answer.user.id,
        selected,
    )
    await maybe_delete_post(context, answer.poll_id)


async def on_channel_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    post = update.channel_post
    if post and post.chat_id == CHANNEL_ID:
        log.info(
            "Channel post observed: channel_id=%s message_id=%s. "
            "Waiting for Telegram automatic forward into discussion group.",
            post.chat_id,
            post.message_id,
        )


async def on_chat_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.info("Chat membership update received")


async def startup():
    global tg_app

    init_db()

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE, on_discussion_message)
    )
    tg_app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post)
    )
    tg_app.add_handler(PollAnswerHandler(on_poll_answer))
    tg_app.add_handler(
        ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER)
    )

    await tg_app.initialize()

    # Validate the configured bot token and log the configured chat identities.
    me = await tg_app.bot.get_me()
    log.info("Telegram bot authenticated: username=@%s id=%s", me.username, me.id)

    try:
        channel = await tg_app.bot.get_chat(CHANNEL_ID)
        group = await tg_app.bot.get_chat(DISCUSSION_GROUP_ID)
        log.info(
            "Configured channel: id=%s title=%s; discussion group: id=%s title=%s",
            channel.id,
            channel.title,
            group.id,
            group.title,
        )
    except Exception:
        log.exception(
            "Could not access configured channel/group. "
            "Check CHANNEL_ID, DISCUSSION_GROUP_ID, and bot membership/admin rights."
        )
        raise

    await tg_app.start()

    await tg_app.bot.set_webhook(
        url=WEBHOOK_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=[
            "message",
            "channel_post",
            "poll_answer",
            "chat_member",
        ],
        drop_pending_updates=False,
    )

    webhook_info = await tg_app.bot.get_webhook_info()
    log.info(
        "Bot started. Webhook URL=%s pending_updates=%s last_error=%s",
        webhook_info.url,
        webhook_info.pending_update_count,
        webhook_info.last_error_message,
    )


async def shutdown():
    global tg_app

    if tg_app:
        # Do not delete the webhook during shutdown. On Render restarts/deploys,
        # an old instance could otherwise remove the webhook set by a new instance.
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
    if not tg_app:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )
        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        payload = await request.json()
        update = Update.de_json(payload, tg_app.bot)
        log.info("Webhook update received: update_id=%s", update.update_id)
        await tg_app.process_update(update)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        log.exception("Failed to process Telegram webhook update")
        raise HTTPException(status_code=500, detail="Webhook processing failed")

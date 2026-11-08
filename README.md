# Community Jury Bot

Standalone Telegram moderation bot. It does not modify or depend on the existing posting bot.

## What it does

- Watches a Telegram channel's linked Discussion Group for automatic-forwarded channel posts.
- Replies to each forwarded post with a Telegram native, non-anonymous poll containing one option: `👎 ဖျက်သင့်တယ်`.
- Tracks unique voters and deletes the original channel post as soon as the removal-vote threshold is reached.
- Stores poll/post/vote mappings in SQLite on Render Persistent Disk.

## Threshold table

The bot snapshots the channel member count when a post appears:

| Channel members | Removal votes required |
|---:|---:|
| 1–300 | 20 |
| 301–500 | 30 |
| 501–1,000 | 50 |
| 1,001–3,000 | 100 |
| 3,001–5,000 | 150 |
| 5,001+ | 200 |

There is no vote time limit.

## Telegram setup

1. Create a new bot with @BotFather.
2. Add it as an administrator to the channel with **Delete Messages** permission.
3. Add it as an administrator to the linked Discussion Group with permission to **Post Messages**.
4. Make sure the channel is linked to that Discussion Group.
5. Do not share the bot token publicly.

## Render deployment

1. Push these files to a GitHub repository.
2. In Render, create a new Blueprint or Web Service from the repository.
3. Ensure a persistent disk is attached at `/var/data`.
4. Set these environment variables:
   - `BOT_TOKEN`: token from BotFather
   - `CHANNEL_ID`: numeric channel ID, usually starts with `-100`
   - `DISCUSSION_GROUP_ID`: numeric linked group ID, usually starts with `-100`
   - `WEBHOOK_URL`: your deployed Render service URL, e.g. `https://your-service.onrender.com`
5. Deploy. The service health endpoint is `/`.

## Important behavior and limitations

- Telegram's view count is not used. A post is deleted when the configured number of distinct users vote for the one removal option.
- Polls are non-anonymous and revoting is disabled, so Telegram sends `poll_answer` updates with voter identity. The bot uses user IDs to prevent duplicate counting.
- Telegram clients may allow a voter to withdraw/change a poll vote. This bot tracks the latest state reported by Telegram; a withdrawn vote is removed from the count.
- The threshold is determined once per post, based on member count at posting time.
- Telegram bots cannot reliably know every human who viewed a post. This design counts actual poll votes only.
- Telegram must deliver the automatic-forwarded channel post into the linked Discussion Group. The bot replies to that message to place the poll in the correct comment thread.
- Test first in a private test channel and linked group before using in a live channel.

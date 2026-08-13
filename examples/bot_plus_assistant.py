"""A complete music bot. Every command is one line, because AyFac handles the rest.

A bot **cannot** join a voice chat. The bot here only parses commands and dispatches to
the assistant (user) client, which is the one actually in the call.

    export API_ID=... API_HASH=... STRING_SESSION=... BOT_TOKEN=...
    python examples/bot_plus_assistant.py

There is no /join and no /add: `/play` joins if needed and queues if busy, and the call
leaves on its own when the queue runs out. Reply to a voice note or an audio file with
`/play` and it is downloaded and played.

    /play <file|url>   or reply to a voice/audio message
    /pause  /resume  /skip  /previous  /replay  /stop
    /seek <secs>  /forward [secs]  /rewind [secs]
    /volume <0-200>  /mute  /unmute
    /loop <n|track|queue|shuffle|off>
    /now   /queue
"""

from __future__ import annotations

import asyncio

from pyrogram import Client, filters  # provided by kurigram
from pyrogram.types import Message

from aytgcalls import AyCreds, AyFac
from aytgcalls.exceptions import AytgcallsError
from aytgcalls.telegram import build_user_client

credentials = AyCreds.from_env().require()
if not credentials.bot_token:
    raise SystemExit("BOT_TOKEN is required for the command interface")

# The user ("assistant") session — this is the account that joins the voice chat.
assistant: Client = build_user_client(credentials, name="assistant")

# The bot session — commands only. It never touches the call.
bot = Client(
    name="commander",
    api_id=credentials.api_id,
    api_hash=credentials.api_hash,
    bot_token=credentials.bot_token,
    in_memory=True,
)

ay = AyFac(assistant)


def argument(message: Message, default: str | None = None) -> str | None:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else default


def number(message: Message, default: float) -> float:
    try:
        return float(argument(message) or default)
    except ValueError:
        return default


@bot.on_message(filters.command(["play", "add"]) & filters.group)
async def play(_: Client, message: Message) -> None:
    # A replied-to voice note / audio file is a valid source all by itself.
    source: object | None = argument(message)
    if message.reply_to_message is not None and not source:
        source = message.reply_to_message
    if not source:
        await message.reply("Usage: `/play <file|url>` — or reply to a voice/audio message.")
        return
    try:
        track, started = await ay.play(message.chat.id, source)
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")
        return
    await message.reply(
        f"▶️ Playing **{track.display_name}**" if started
        else f"➕ Queued **{track.display_name}** (#{len(ay[message.chat.id].queue)})"
    )


@bot.on_message(
    filters.command(["pause", "resume", "skip", "previous", "replay", "stop", "end"])
    & filters.group
)
async def control(_: Client, message: Message) -> None:
    chat, action = message.chat.id, message.command[0]
    try:
        if action == "pause":
            await ay.pause(chat)
            await message.reply("⏸ Paused")
        elif action == "resume":
            await ay.resume(chat)
            await message.reply("▶️ Resumed")
        elif action == "skip":
            following = await ay.skip(chat)
            await message.reply(
                f"⏭ **{following.display_name}**" if following else "⏭ Queue finished"
            )
        elif action == "previous":
            earlier = await ay.previous(chat)
            await message.reply(f"⏮ **{earlier.display_name}**")
        elif action == "replay":
            await ay.replay(chat)
            await message.reply("🔁 From the top")
        else:  # stop / end
            await ay.stop(chat)
            await message.reply("⏹ Stopped and left the voice chat")
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")


@bot.on_message(filters.command(["seek", "forward", "rewind"]) & filters.group)
async def seeking(_: Client, message: Message) -> None:
    chat, action = message.chat.id, message.command[0]
    try:
        if action == "seek":
            if argument(message) is None:
                await message.reply("Usage: `/seek <seconds>`")
                return
            landed = await ay.seek(chat, number(message, 0))
        elif action == "forward":
            landed = await ay.forward(chat, number(message, 10))
        else:
            landed = await ay.rewind(chat, number(message, 10))
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")
        return
    info = ay.now_playing(chat)
    await message.reply(
        f"⏩ `{info.format_time(landed)} / {info.format_time(info.duration)}`\n"
        f"{info.progress_bar()}"
    )


@bot.on_message(filters.command(["volume", "mute", "unmute"]) & filters.group)
async def loudness(_: Client, message: Message) -> None:
    chat, action = message.chat.id, message.command[0]
    try:
        if action == "mute":
            await ay.mute(chat)
            await message.reply("🔇 Muted")
        elif action == "unmute":
            await ay.unmute(chat)
            await message.reply("🔊 Unmuted")
        else:
            percent = int(number(message, -1))
            if not 0 <= percent <= 200:
                await message.reply("Usage: `/volume <0-200>`")
                return
            await ay.volume(chat, percent)
            await message.reply(f"🔊 Volume {percent}%")
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")


@bot.on_message(filters.command("loop") & filters.group)
async def loop(_: Client, message: Message) -> None:
    try:
        # A count, a name, or "shuffle" — loop() sorts it out.
        mode = await ay.loop(message.chat.id, argument(message, "off"))
    except (AytgcallsError, ValueError) as exc:
        await message.reply(f"❌ {exc}")
        return
    times = ay[message.chat.id].queue.loop_times
    await message.reply(f"🔁 Loop: **{mode.value}**" + (f" ×{times}" if times else ""))


@bot.on_message(filters.command(["now", "np", "queue"]) & filters.group)
async def status(_: Client, message: Message) -> None:
    info = ay.now_playing(message.chat.id)
    if info is None or info.source is None:
        await message.reply("Nothing is playing here.")
        return
    if message.command[0] == "queue":
        items = ay[message.chat.id].queue.items
        if not items:
            await message.reply(f"Now: **{info.title}**\nQueue is empty.")
            return
        lines = "\n".join(f"{i + 1}. {t.display_name}" for i, t in enumerate(items[:15]))
        await message.reply(f"Now: **{info.title}**\n\n**Up next**\n{lines}")
        return
    await message.reply(
        f"**{info.title}**\n"
        f"`{info.format_time(info.position)} / {info.format_time(info.duration)}`\n"
        f"{info.progress_bar()}\n"
        f"state: {info.state.value} · volume: {info.volume:.0f}% · "
        f"loop: {info.loop.value} · queued: {info.queued}"
    )


async def main() -> None:
    await assistant.start()
    await bot.start()
    print("Assistant and bot are running. Ctrl-C to quit.")
    try:
        await asyncio.Event().wait()
    finally:
        await ay.leave_all()
        await bot.stop()
        await assistant.stop()


if __name__ == "__main__":
    asyncio.run(main())

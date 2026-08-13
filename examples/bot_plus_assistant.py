"""A complete music-bot command surface in one file.

A bot **cannot** join a voice chat. The bot here only parses commands and dispatches to
the assistant (user) client, which is the one actually in the call.

    export API_ID=... API_HASH=... STRING_SESSION=... BOT_TOKEN=...
    python examples/bot_plus_assistant.py

Commands
    /play <file|url>   play now (or queue if something is already playing)
    /add <file|url>    always queue
    /pause  /resume  /skip  /previous  /replay  /stop  /end
    /seek <seconds>    jump to an absolute position
    /forward [secs]    default 10          /rewind [secs]   default 10
    /volume <0-200>    local gain          /mute  /unmute    server side
    /loop <off|track|queue>
    /queue             show what is lined up
    /now               show what is playing, with a progress bar
"""

from __future__ import annotations

import asyncio

from pyrogram import Client, filters  # provided by kurigram
from pyrogram.types import Message

from aytgcalls import GroupCallFactory, TelegramCredentials
from aytgcalls.exceptions import AytgcallsError
from aytgcalls.telegram import build_user_client

credentials = TelegramCredentials.from_env().require()
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

factory = GroupCallFactory(assistant)


def argument(message: Message, default: str | None = None) -> str | None:
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else default


def number(message: Message, default: float) -> float:
    raw = argument(message)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


@bot.on_message(filters.command(["play", "add"]) & filters.group)
async def play(_: Client, message: Message) -> None:
    source = argument(message)
    if not source:
        await message.reply("Usage: `/play <file path or url>`")
        return
    try:
        call = await factory.get_or_create(message.chat.id)
        if message.command[0] == "add":
            track = await call.queue.add(source)
            await message.reply(f"➕ Queued **{track.display_name}** (#{len(call.queue)})")
            return
        # add() plays immediately when idle and queues when busy — the "auto" path.
        track, started = await call.add(source)
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")
        return
    await message.reply(
        f"▶️ Playing **{track.display_name}**" if started
        else f"➕ Queued **{track.display_name}** (#{len(call.queue)})"
    )


@bot.on_message(
    filters.command(["pause", "resume", "skip", "previous", "replay", "stop", "end"])
    & filters.group
)
async def control(_: Client, message: Message) -> None:
    call = factory.get(message.chat.id)
    if call is None or not call.is_connected:
        await message.reply("Not in a voice chat here.")
        return
    action = message.command[0]
    try:
        if action == "pause":
            await call.pause()
            await message.reply(f"⏸ Paused at `{call.now_playing.format_time(call.position)}`")
        elif action == "resume":
            await call.resume()
            await message.reply("▶️ Resumed")
        elif action == "skip":
            following = await call.skip()
            await message.reply(
                f"⏭ **{following.display_name}**" if following else "⏭ Queue finished"
            )
        elif action == "previous":
            earlier = await call.previous()
            await message.reply(f"⏮ **{earlier.display_name}**")
        elif action == "replay":
            await call.replay()
            await message.reply("🔁 Restarted from the beginning")
        elif action == "stop":
            await call.stop()
            await message.reply("⏹ Stopped and cleared the queue")
        else:  # end
            await call.end()
            await message.reply("👋 Left the voice chat")
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")


@bot.on_message(filters.command(["seek", "forward", "rewind"]) & filters.group)
async def seeking(_: Client, message: Message) -> None:
    call = factory.get(message.chat.id)
    if call is None or not call.is_connected:
        await message.reply("Not in a voice chat here.")
        return
    action = message.command[0]
    try:
        if action == "seek":
            target = argument(message)
            if target is None:
                await message.reply("Usage: `/seek <seconds>`")
                return
            landed = await call.seek(float(target))
        elif action == "forward":
            landed = await call.forward(number(message, 10))
        else:
            landed = await call.rewind(number(message, 10))
    except ValueError:
        await message.reply("Give me a number of seconds.")
        return
    except AytgcallsError as exc:
        await message.reply(f"❌ {exc}")
        return
    info = call.now_playing
    await message.reply(
        f"⏩ `{info.format_time(landed)} / {info.format_time(info.duration)}`\n"
        f"{info.progress_bar()}"
    )


@bot.on_message(filters.command(["volume", "mute", "unmute"]) & filters.group)
async def loudness(_: Client, message: Message) -> None:
    call = factory.get(message.chat.id)
    if call is None or not call.is_connected:
        await message.reply("Not in a voice chat here.")
        return
    try:
        if message.command[0] == "mute":
            await call.mute()
            await message.reply("🔇 Muted")
        elif message.command[0] == "unmute":
            await call.unmute()
            await message.reply("🔊 Unmuted")
        else:
            percent = int(number(message, -1))
            if not 0 <= percent <= 200:
                await message.reply("Usage: `/volume <0-200>`")
                return
            await call.set_volume(percent)
            await message.reply(f"🔊 Volume {percent}%")
    except (AytgcallsError, ValueError) as exc:
        await message.reply(f"❌ {exc}")


@bot.on_message(filters.command("loop") & filters.group)
async def loop(_: Client, message: Message) -> None:
    call = factory.get(message.chat.id)
    if call is None or not call.is_connected:
        await message.reply("Not in a voice chat here.")
        return
    try:
        mode = call.set_loop(argument(message, "off"))
    except ValueError as exc:
        await message.reply(f"❌ {exc}")
        return
    await message.reply(f"🔁 Loop: **{mode.value}**")


@bot.on_message(filters.command(["now", "np", "queue"]) & filters.group)
async def status(_: Client, message: Message) -> None:
    call = factory.get(message.chat.id)
    if call is None or not call.is_connected:
        await message.reply("Not in a voice chat here.")
        return
    info = call.now_playing
    if message.command[0] == "queue":
        if not call.queue.items:
            await message.reply(f"Now: **{info.title}**\nQueue is empty.")
            return
        lines = "\n".join(
            f"{index + 1}. {track.display_name}"
            for index, track in enumerate(call.queue.items[:15])
        )
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
        await factory.leave_all()
        await bot.stop()
        await assistant.stop()


if __name__ == "__main__":
    asyncio.run(main())

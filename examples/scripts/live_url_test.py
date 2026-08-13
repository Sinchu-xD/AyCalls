#!/usr/bin/env python3
"""Live test: stream an audio URL into a real Telegram group voice chat.

Beyond scripts/live_check.py this one:
  * validates the URL with FFmpeg *before* joining, so a bad link is not confused
    with a transport failure;
  * reports our own participant state (``muted`` / ``can_self_unmute``), which is the
    single most common reason "packets are flowing but nobody hears anything";
  * samples RTP counters on a timer and prints a packets-per-second rate, so you can see
    the 20 ms cadence holding (or not) over time.

Environment:
    API_ID, API_HASH, STRING_SESSION   user (assistant) session — required
    TEST_CHAT_ID                       chat with a RUNNING voice chat — required
    TEST_AUDIO_URL                     http(s) audio URL — required
    TEST_STREAM_SECONDS                default 25
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from aytgcalls import CallConfig, GroupCall, TelegramCredentials, enable_debug  # noqa: E402
from aytgcalls.exceptions import AytgcallsError  # noqa: E402
from aytgcalls.media.opus import opus_available  # noqa: E402
from aytgcalls.media.source import probe_source, validate_source  # noqa: E402
from aytgcalls.telegram import build_user_client  # noqa: E402

SECONDS = float(os.environ.get("TEST_STREAM_SECONDS", "25"))


def fail(message: str) -> None:
    print(f"\n[FAIL] {message}")
    raise SystemExit(1)


async def participant_state(call: GroupCall) -> dict[str, object] | None:
    """Read our own GroupCallParticipant record straight from the server."""
    signaling = call._signaling  # noqa: SLF001
    discovered = call._discovered  # noqa: SLF001
    if discovered is None:
        return None
    state = await signaling.get_group_call(discovered.input_call, limit=50)
    me_id = call.client.me.id
    for participant in getattr(state, "participants", []) or []:
        if getattr(getattr(participant, "peer", None), "user_id", None) == me_id:
            return {
                "muted": getattr(participant, "muted", None),
                "can_self_unmute": getattr(participant, "can_self_unmute", None),
                "volume": getattr(participant, "volume", None),
                "source": getattr(participant, "source", None),
            }
    return None


async def main() -> int:
    missing = [
        name
        for name in ("API_ID", "API_HASH", "STRING_SESSION", "TEST_CHAT_ID", "TEST_AUDIO_URL")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"[SKIP] missing: {', '.join(missing)}")
        return 0

    if shutil.which(os.environ.get("AYTGCALLS_FFMPEG", "ffmpeg")) is None:
        fail("ffmpeg not on PATH")
    if not opus_available():
        fail("libopus unavailable via PyAV")
    print("[OK] preflight: ffmpeg + libopus present, no py-tgcalls")

    url = os.environ["TEST_AUDIO_URL"]
    chat_raw = os.environ["TEST_CHAT_ID"]
    chat_id: int | str = int(chat_raw) if chat_raw.lstrip("-").isdigit() else chat_raw

    # 1. Validate the URL first — a dead link must not look like a transport bug.
    print(f"\n-> probing audio URL with FFmpeg: {url}")
    try:
        await probe_source(validate_source(url))
    except AytgcallsError as exc:
        fail(f"URL is not playable: {exc}")
    print("[OK] FFmpeg decoded audio from the URL")

    enable_debug()
    client = build_user_client(TelegramCredentials.from_env().require(), name="aytgcalls_live_url")
    try:
        await client.start()
    except Exception as exc:
        fail(f"could not start the user session ({type(exc).__name__}: {exc})")
    me = await client.get_me()
    if me.is_bot:
        fail("STRING_SESSION belongs to a BOT. Bots cannot join voice chats.")
    print(f"[OK] user session: id={me.id} @{me.username or '-'}")

    call = GroupCall(client, config=CallConfig.from_env(buffer_ms=800, prefetch_ms=400))
    status = 0
    try:
        print(f"\n-> joining voice chat in {chat_id} ...")
        await call.join(chat_id)
        discovered = call._discovered  # noqa: SLF001
        print(f"[OK] joined '{discovered.title or '-'}' "
              f"({discovered.participants_count} participants), ssrc={call.ssrc}")

        stats = await call.get_stats()
        print(f"[OK] ICE={stats.ice_state}   DTLS={stats.dtls_state}")
        if stats.dtls_state != "connected":
            fail("DTLS-SRTP did not establish")

        # 2. Are we allowed to be heard at all?
        state = await participant_state(call)
        print(f"\n[info] our participant record: {state}")
        if state and state.get("muted"):
            if state.get("can_self_unmute"):
                print("-> we are muted but may self-unmute; unmuting ...")
                await call.mute(False)
                print(f"   after unmute: {await participant_state(call)}")
            else:
                print("[WARN] SERVER-SIDE MUTED and can_self_unmute=False - an ADMIN must "
                      "unmute this account or nobody will hear the audio, even though "
                      "RTP will keep flowing.")

        # 3. Stream.
        print(f"\n-> streaming the URL for {SECONDS:g}s ...")
        await call.play(url)

        previous = 0
        started = time.monotonic()
        while time.monotonic() - started < SECONDS:
            await asyncio.sleep(2.5)
            stats = await call.get_stats()
            delta = stats.packets_sent - previous
            previous = stats.packets_sent
            print(
                f"   t={time.monotonic() - started:5.1f}s  "
                f"packets={stats.packets_sent:6d} (+{delta:3d} ~ {delta / 2.5:5.1f}/s)  "
                f"bytes={stats.bytes_sent:8d}  audio_frames={stats.frames_encoded:5d}  "
                f"silence={stats.frames_silence:4d}  underruns={stats.underruns:4d}  "
                f"buffered={call.player.buffered_ms:4d}ms"
            )

        stats = await call.get_stats()
        expected = int(SECONDS * 50 * 0.7)  # 50 packets/s at 20 ms, 30% slack
        print()
        if stats.packets_sent < expected:
            fail(f"only {stats.packets_sent} RTP packets in {SECONDS:g}s "
                 f"(expected >= {expected}) - media is not flowing")
        print(f"[OK] {stats.packets_sent} RTP packets sent "
              f"(~{stats.packets_sent / SECONDS:.1f}/s, target 50/s)")
        if stats.frames_encoded == 0:
            fail("the encoder only saw silence - the URL never reached the pipeline")
        print(f"[OK] {stats.frames_encoded} real audio frames encoded "
              f"({stats.frames_silence} silence, {stats.underruns} underruns)")
        print("\n" + call.debug_snapshot())
        print("\n[ASK] Final check: ask someone in that voice chat whether they actually "
              "heard the audio. Packets leaving this host is necessary but not sufficient.")
    except AytgcallsError as exc:
        print(f"\n[FAIL] {type(exc).__name__}: {exc}")
        status = 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] unexpected {type(exc).__name__}: {exc}")
        status = 1
    finally:
        print("\n-> leaving ...")
        try:
            await call.leave()
        finally:
            await client.stop()
    print("[OK] left cleanly" if status == 0 else "[FAIL] live test failed")
    return status


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

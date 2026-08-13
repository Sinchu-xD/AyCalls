#!/usr/bin/env python3
"""End-to-end smoke test against a real Telegram group voice chat.

This is the only check that can prove the two things a build environment cannot:

1. Telegram accepts our ``phone.joinGroupCall`` payload.
2. The DTLS-SRTP handshake with Telegram's SFU completes and RTP actually flows.

It is skipped unless every environment variable is present::

    export API_ID=...            # my.telegram.org
    export API_HASH=...
    export STRING_SESSION=...    # a USER session, not a bot (examples/scripts/gen_session.py)
    export TEST_CHAT_ID=-1001234567890
    export TEST_AUDIO=/path/to/sample.mp3   # optional; a tone is generated otherwise

    python examples/scripts/live_check.py

Exit code 0 means: joined, streamed, RTP packets counted, left cleanly.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from aytgcalls import CallConfig, GroupCall, TelegramCredentials, enable_debug  # noqa: E402
from aytgcalls.exceptions import AytgcallsError  # noqa: E402
from aytgcalls.media.opus import opus_available  # noqa: E402
from aytgcalls.telegram import build_user_client  # noqa: E402

REQUIRED = ("API_ID", "API_HASH", "STRING_SESSION", "TEST_CHAT_ID")
STREAM_SECONDS = float(os.environ.get("TEST_STREAM_SECONDS", "12"))


def _fail(message: str) -> None:
    print(f"❌ {message}")
    raise SystemExit(1)


def _preflight() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(f"⏭  SKIPPED — missing environment variables: {', '.join(missing)}")
        raise SystemExit(0)
    if shutil.which(os.environ.get("AYTGCALLS_FFMPEG", "ffmpeg")) is None:
        _fail("ffmpeg not found on PATH (apt install ffmpeg)")
    if not opus_available():
        _fail("libopus is not available through PyAV (apt install libopus0)")
    for conflict in ("tgcalls", "pytgcalls"):
        if conflict in sys.modules or _installed(conflict):
            _fail(f"{conflict} is installed; aytgcalls must run without it")
    print("✅ preflight: ffmpeg, libopus, no py-tgcalls")


def _installed(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _sample_audio() -> str:
    path = os.environ.get("TEST_AUDIO")
    if path:
        if not os.path.exists(path):
            _fail(f"TEST_AUDIO does not exist: {path}")
        return path
    target = os.path.join(tempfile.gettempdir(), "aytgcalls_live_check.wav")
    subprocess.run(
        [
            os.environ.get("AYTGCALLS_FFMPEG", "ffmpeg"),
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={STREAM_SECONDS + 5:.0f}:sample_rate=48000",
            "-ac", "2", target,
        ],
        check=True,
    )
    print(f"ℹ️  generated a 440 Hz test tone at {target}")
    return target


async def main() -> int:
    _preflight()
    enable_debug()

    credentials = TelegramCredentials.from_env().require()
    chat_id_raw = os.environ["TEST_CHAT_ID"]
    chat_id: int | str = int(chat_id_raw) if chat_id_raw.lstrip("-").isdigit() else chat_id_raw
    audio = _sample_audio()

    client = build_user_client(credentials, name="aytgcalls_live_check")
    try:
        await client.start()
    except Exception as exc:  # bad session string, wrong api_id, network down…
        _fail(
            f"Could not start the user session ({type(exc).__name__}: {exc}).\n"
            "   Regenerate STRING_SESSION with examples/scripts/gen_session.py and check API_ID/API_HASH."
        )
    me = await client.get_me()
    if me.is_bot:
        _fail("STRING_SESSION belongs to a bot. A bot cannot join a voice chat.")
    print(f"✅ user session: id={me.id} @{me.username or '-'}")

    call = GroupCall(client, config=CallConfig.from_env())
    ended = asyncio.Event()

    @call.on_disconnect
    async def _(_call: GroupCall, reason) -> None:  # noqa: ANN001
        print(f"ℹ️  disconnect event: {reason.value}")
        ended.set()

    status = 0
    try:
        print(f"→ discovering + joining the voice chat in {chat_id} …")
        await call.join(chat_id)
        print(f"✅ joined. ssrc={call.ssrc}")

        stats = await call.get_stats()
        print(f"✅ ICE={stats.ice_state}  DTLS={stats.dtls_state}")
        if stats.dtls_state != "connected":
            _fail("DTLS did not reach the connected state")

        print(f"→ streaming {audio} for {STREAM_SECONDS:g}s …")
        await call.play(audio)

        baseline = (await call.get_stats()).packets_sent
        await asyncio.sleep(STREAM_SECONDS)
        stats = await call.get_stats()
        sent = stats.packets_sent - baseline
        expected = int(STREAM_SECONDS * 1000 / 20 * 0.7)  # 50 packets/s, 30% slack

        print(
            f"   packets_sent={stats.packets_sent} (+{sent})  bytes={stats.bytes_sent}  "
            f"frames={stats.frames_encoded} silence={stats.frames_silence} "
            f"underruns={stats.underruns}"
        )
        if sent < expected:
            _fail(f"only {sent} RTP packets in {STREAM_SECONDS:g}s, expected >= {expected}")
        print("✅ RTP is flowing at the expected 20 ms cadence")
        if stats.frames_encoded == 0:
            _fail("the encoder only ever saw silence — the media pipeline is not feeding it")
        print("✅ real audio frames (not silence) reached the encoder")
        print("\n" + call.debug_snapshot())
        print(
            "\n👂 Ask a participant whether they heard the tone. Packets leaving the host is "
            "necessary but not sufficient — audibility also depends on not being "
            "server-side muted."
        )
    except AytgcallsError as exc:
        print(f"❌ {type(exc).__name__}: {exc}")
        status = 1
    finally:
        print("→ leaving …")
        await call.leave()
        await client.stop()
    print("✅ left cleanly" if status == 0 else "❌ live check failed")
    return status


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

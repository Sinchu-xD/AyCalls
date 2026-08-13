"""The shortest possible player: no join, no leave, no bookkeeping.

    export API_ID=... API_HASH=... STRING_SESSION=...
    python examples/basic_play.py -1001234567890 song.mp3
"""

from __future__ import annotations

import asyncio
import sys

from aytgcalls import AyCall, AyCreds, enable_debug
from aytgcalls.telegram import build_user_client


async def main(chat_id: int, source: str) -> None:
    enable_debug()  # remove for quieter logs

    # Credentials come from API_ID / API_HASH / STRING_SESSION — never hardcode them.
    client = build_user_client(AyCreds.from_env())
    await client.start()

    call = AyCall(client, chat_id)
    finished = asyncio.Event()

    @call.on_disconnect
    async def _(_call: AyCall, reason) -> None:  # noqa: ANN001
        print(f"call ended: {reason.value}")
        finished.set()

    try:
        # play() joins the voice chat, starts the audio, and the call leaves by itself
        # once the queue runs out. That is the whole program.
        await call.play(source)
        print("playing… (the call will leave automatically when the track ends)")
        await finished.wait()
    finally:
        await call.end()
        await client.stop()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))

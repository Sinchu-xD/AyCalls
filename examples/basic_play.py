"""Play a local file into a group voice chat.

    export API_ID=... API_HASH=... STRING_SESSION=...
    python examples/basic_play.py -1001234567890 song.mp3
"""

from __future__ import annotations

import asyncio
import sys

from aytgcalls import GroupCall, TelegramCredentials, enable_debug
from aytgcalls.telegram import build_user_client


async def main(chat_id: int, path: str) -> None:
    enable_debug()  # remove for quieter logs

    # Credentials come from API_ID / API_HASH / STRING_SESSION — never hardcode them.
    client = build_user_client(TelegramCredentials.from_env())
    await client.start()

    call = GroupCall(client)
    finished = asyncio.Event()

    @call.on_stream_end
    async def _(_call: GroupCall, source, reason) -> None:  # noqa: ANN001
        print(f"finished {source.display_name} ({reason.value})")
        finished.set()

    @call.on_disconnect
    async def _(_call: GroupCall, reason) -> None:  # noqa: ANN001
        print(f"disconnected: {reason.value}")
        finished.set()

    try:
        await call.join(chat_id)
        await call.play(path)
        print("playing… (Ctrl-C to stop)")
        await finished.wait()
    finally:
        await call.leave()
        await client.stop()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))

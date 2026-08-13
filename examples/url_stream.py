"""Stream an http(s) URL and queue several tracks.

    export API_ID=... API_HASH=... STRING_SESSION=...
    python examples/url_stream.py -1001234567890 https://example.com/radio.mp3
"""

from __future__ import annotations

import asyncio
import sys

from aytgcalls import GroupCall, LoopMode, TelegramCredentials
from aytgcalls.telegram import build_user_client


async def main(chat_id: int, url: str) -> None:
    client = build_user_client(TelegramCredentials.from_env())
    await client.start()

    call = GroupCall(client)
    try:
        await call.join(chat_id)

        # Nothing is downloaded up-front: FFmpeg streams it and the ring buffer holds
        # only a few hundred milliseconds at a time.
        await call.play(url)
        await call.queue.add("https://example.com/second.mp3")
        await call.queue.add("local_outro.flac")
        call.queue.loop = LoopMode.QUEUE

        await asyncio.sleep(15)
        await call.set_volume(60)          # local gain
        await asyncio.sleep(5)
        await call.pause()
        await asyncio.sleep(2)
        await call.resume()
        await call.skip()

        stats = await call.get_stats()
        print("RTP packets sent:", stats.packets_sent)
        print(call.debug_snapshot())

        await asyncio.sleep(20)
    finally:
        await call.leave()
        await client.stop()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))

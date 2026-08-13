"""Stream URLs, let the queue build itself, and drive every control.

    export API_ID=... API_HASH=... STRING_SESSION=...
    python examples/url_stream.py -1001234567890 https://example.com/radio.mp3
"""

from __future__ import annotations

import asyncio
import sys

from aytgcalls import AyCall, AyCreds
from aytgcalls.telegram import build_user_client


async def main(chat_id: int, url: str) -> None:
    client = build_user_client(AyCreds.from_env())
    await client.start()

    call = AyCall(client, chat_id)
    try:
        # Nothing is downloaded up front: FFmpeg streams it and the ring buffer holds
        # only a few hundred milliseconds at a time. play() also joins for us.
        track, started = await call.play(url)
        print(f"{'playing' if started else 'queued'}: {track.display_name}")

        # Call play() again for every request — it queues automatically when busy.
        await call.play("https://example.com/second.mp3")
        await call.play("local_outro.flac")
        print(f"queue length: {len(call.queue)}")

        await asyncio.sleep(10)
        print(call.now_playing)                 # title, position/duration, progress bar

        await call.seek(30)                     # jump to 0:30
        await call.forward(15)                  # 0:45
        await call.rewind(5)                    # 0:40
        await call.set_volume(60)

        await call.loop(2)                      # repeat this track twice more
        await call.loop("shuffle")              # shuffle, then keep looping the queue
        await call.loop("off")

        await call.pause()
        await asyncio.sleep(2)
        await call.resume()
        await call.skip()

        stats = await call.get_stats()
        print(f"RTP packets sent: {stats.packets_sent}")
        await asyncio.sleep(20)
    finally:
        # end() stops playback and leaves; the call would also leave on its own once the
        # queue emptied.
        await call.end()
        await client.stop()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))

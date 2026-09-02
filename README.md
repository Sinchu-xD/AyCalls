# AyCalls

> Play audio and video into **Telegram group voice chats** from Python.
> No `py-tgcalls`. No `tgcalls`. No Telethon. Just Kurigram + aiortc + FFmpeg.

[![PyPI](https://img.shields.io/pypi/v/aytgcalls.svg)](https://pypi.org/project/aytgcalls/)
[![Python](https://img.shields.io/pypi/pyversions/aytgcalls.svg)](https://pypi.org/project/aytgcalls/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-274%20passed-brightgreen.svg)](examples/tests/)

AyCalls is a small, opinionated library that wires three battle-tested tools together:

* **Kurigram** — MTProto signaling (`phone.joinGroupCall`, `phone.LeaveGroupCall`, …)
* **aiortc** — ICE / DTLS-SRTP / RTP, controlled at the ORTC level so SSRCs can be pinned
* **FFmpeg** — decodes anything you point it at into 48 kHz stereo PCM

The result is a `pytgcalls`-style API that just works: one `play()` call handles join,
queue, playback, and auto-leave.

```python
from aytgcalls import AyClient, AyCreds

client = AyClient(AyCreds.from_env())
await client.start()

await client.play(-1001234567890, "song.mp3")    # auto-join + play
await client.skip(-1001234567890)
await client.end(-1001234567890)
await client.stop()
```

Every action takes `chat_id` first — one client, many voice chats.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Why you need two accounts](#why-you-need-two-accounts)
- [Installation](#installation)
- [Generating a STRING_SESSION](#generating-a-string_session)
- [Quick start](#quick-start)
- [Three usage styles](#three-usage-styles)
  - [AyClient — one class, everything](#ayclient--one-class-everything)
  - [AyFac — factory for many chats](#ayfac--factory-for-many-chats)
  - [AyCall — one call, full control](#aycall--one-call-full-control)
- [API reference](#api-reference)
- [The queue](#the-queue)
- [Events](#events)
- [Video / screen sharing](#video--screen-sharing)
- [Bot command surface](#bot-command-surface)
- [Configuration](#configuration)
- [Error handling](#error-handling)
- [Verifying it works](#verifying-it-works-on-a-vps)
- [Troubleshooting](#troubleshooting)
- [What's implemented, what's not](#whats-implemented-whats-not)
- [Development](#development)
- [License](#license)

---

## Features

- 🎵 **Stream any audio format** FFmpeg understands — MP3, FLAC, OGG/Opus, WAV, AAC, M4A, MOV, MKV, remote HTTP(S) streams, Telegram voice notes, YouTube / SoundCloud / Vimeo / Twitch (via the optional `yt-dlp` integration)
- 🎬 **Video / screen sharing** via H.264 on a presentation SSRC
- ⏯ **Full playback control** — play, pause, resume, skip, previous, replay, stop, seek, forward, rewind, loop (track / queue / shuffle / count)
- 📜 **Near-gapless transitions** — the next track starts the moment the previous one hits EOF, so the 20 ms RTP cadence never breaks
- 🧠 **Smart seeking** — byte-offset seeks for self-framing formats (MP3, AAC, ADTS, AC3), FFmpeg `-ss` for header-dependent ones (WAV, FLAC, OGG, MP4), live streams are refused with a clear error
- 🪄 **One-call automation** — `play()` auto-joins, auto-queues, the call auto-leaves when the queue empties (with a configurable grace period)
- 🔁 **Auto-reconnect** with exponential backoff when the SFU drops you
- 🔇 **Server-side mute / unmute / volume** through `phone.editGroupCallParticipant`
- 👥 **List participants**, rename the voice chat, query live RTP / ICE / DTLS stats
- 🧹 **JSON ⇄ SDP bridge** (both directions, round-trip tested) for anyone who wants the SDP view
- 🪶 **Lazy imports** — aiortc and Kurigram only load when you actually need them, so unit tests don't drag them in

## How it works

```
chat_id
   │  channels.getFullChannel / messages.getFullChat
   ▼
InputGroupCall ──► phone.joinGroupCall(params = {ssrc, ufrag, pwd, fingerprints})
                              │
                              ▼
                   UpdateGroupCallConnection
                   {transport:{…}, audio:{payload-types, rtp-hdrexts}}
                              │
   ┌──────────────────────────┴───────────────────────────────────────┐
   │  ICE (controlling)  →  DTLS (client)  →  SRTP keys              │
   └──────────────────────────┬───────────────────────────────────────┘
                              ▼
 file/URL ─ FFmpeg ─► PCM s16le 48k/2 ─► ring buffer ─► gain ─► Opus 20 ms ─► RTP ─► SFU
```

The full protocol write-up — including how the SSRC and Opus payload type are pinned, and
why we can't use `RTCPeerConnection` directly — lives in [`PROTOCOL.md`](PROTOCOL.md).

## Why you need two accounts

This is a Telegram limitation, not a library one: `phone.joinGroupCall` is **user-only**.

The standard pattern is a two-account setup:

| Account | Role |
|---|---|
| **User** ("assistant"), via `API_ID` + `API_HASH` + `STRING_SESSION` | Joins the call and streams media. **Required.** |
| **Bot**, via `BOT_TOKEN` | Optional. Parses `/play`, `/skip`, etc. and dispatches to the assistant. Never touches the call. |

`GroupCall` checks this at join time and raises `BotClientNotAllowed` if you hand it a
bot session.

## Installation

```bash
pip install aytgcalls
```

### Remove conflicting Pyrogram forks

Kurigram is a maintained Pyrogram fork and **still imports as `pyrogram`**. If the real
`pyrogram` or `pyrofork` is installed alongside it, the two packages overwrite each
other's files and you get bizarre import errors. Clean them up first:

```bash
pip uninstall -y pyrogram pyrofork tgcalls py-tgcalls pytgcalls
pip install -U kurigram aytgcalls
```

Recommended speedup:

```bash
pip install "aytgcalls[fast]"     # adds tgcrypto
```

Sanity-check the install:

```bash
python -c "import pyrogram; print(pyrogram.__version__)"   # should be a kurigram version
python -c "import importlib.util; assert importlib.util.find_spec('tgcalls') is None; print('no py-tgcalls')"
```

### FFmpeg and libopus

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg libopus0

# Fedora / RHEL
sudo dnf install -y ffmpeg opus

# macOS
brew install ffmpeg opus
```

Verify:

```bash
ffmpeg -version
python -c "from aytgcalls.media.opus import opus_available; print('opus ✓' if opus_available() else 'opus ✗')"
```

If `ffmpeg` isn't on `PATH`, point at it explicitly:

```bash
export AYTGCALLS_FFMPEG=/usr/local/bin/ffmpeg
```

### Optional: YouTube / streaming sites

For YouTube, SoundCloud, Vimeo, Twitch and similar, install `yt-dlp` and the helper
package picks it up automatically:

```bash
pip install -U yt-dlp
```

AyCalls uses [`YouTubeMusic`](https://pypi.org/project/YouTubeMusic/) internally — it
wraps `yt-dlp` with format selectors that always return direct progressive URLs, never
HLS / DASH manifests.

## Generating a STRING_SESSION

Get `API_ID` / `API_HASH` from <https://my.telegram.org> → *API development tools*, then:

```bash
export API_ID=1234567
export API_HASH=0123456789abcdef0123456789abcdef
python examples/scripts/gen_session.py
```

Log in with the **phone number of a normal account** (not a bot). The script prints the
session string once.

> **Treat `STRING_SESSION` like a password.** Anyone with it has full access to that
> account. Keep it in an environment variable or a secret manager. AyCalls never
> hardcodes credentials and never logs them.

## Quick start

```python
import asyncio
from aytgcalls import AyClient, AyCreds

async def main():
    client = AyClient(AyCreds.from_env())
    await client.start()

    await client.play(-1001234567890, "song.mp3")    # auto-join + play
    await asyncio.sleep(30)
    await client.stop()

asyncio.run(main())
```

That's it — one `play()` call handles join, queue, play, and auto-leave.

## Three usage styles

### AyClient — one class, everything

The recommended entry point. One instance per bot, multiple voice chats:

```python
from aytgcalls import AyClient, AyCreds

client = AyClient(AyCreds.from_env())
await client.start()

# every method takes chat_id first
await client.play(chat_id, "song.mp3")          # auto-join + play or queue
await client.pause(chat_id)
await client.resume(chat_id)
await client.skip(chat_id)
await client.seek(chat_id, 45)
await client.forward(chat_id, 10)
await client.rewind(chat_id, 10)
await client.volume(chat_id, 80)
await client.mute(chat_id)
await client.unmute(chat_id)
await client.loop(chat_id, "queue")
await client.shuffle(chat_id)
await client.play_video(chat_id, "clip.mp4")

# introspection
print(client.position(chat_id))
print(client.now_playing(chat_id))
print(client.is_connected(chat_id))
print(await client.get_participants(chat_id))   # who's in the call

# lifecycle
await client.end(chat_id)        # stop + leave this chat
await client.stop()              # leave all + shutdown
```

`AyClient` wraps `AyFac` internally and mirrors every per-chat control. You can also pass
in a pre-built Kurigram `Client` if you need more control over startup.

### AyFac — factory for many chats

For when you want the factory directly — useful if you already have a Kurigram client
lying around:

```python
from aytgcalls import AyFac

fac = AyFac(user_client)
await fac.play(chat_id, "song.mp3")         # creates, joins, plays or queues
await fac.pause(chat_id)
await fac.skip(chat_id)
await fac.volume(chat_id, 80)
await fac.stop(chat_id)                     # stop + leave
await fac.leave_all()                       # shutdown
```

### AyCall — one call, full control

For a single persistent voice chat where you want the call object as the API:

```python
from aytgcalls import AyCall

call = AyCall(user_client, chat_id)
await call.play("song.mp3")                 # joins + plays
await call.pause()
await call.resume()
await call.skip()
await call.end()                            # stop + leave
```

## API reference

All methods are available on `AyClient`, `AyFac` (with `chat_id` first), and `AyCall`
(without `chat_id`). The table below shows the `AyClient` signature.

### Playback

```python
await client.play(chat_id, source)              # join / play / queue, all automatic
await client.play(chat_id, source, force=True)  # jump the queue
await client.add(chat_id, source)               # alias for play()
await client.pause(chat_id)
await client.resume(chat_id)
await client.stop_playback(chat_id)             # stop audio, stay in the call
await client.stop(chat_id)                      # stop + leave (same as end)
await client.end(chat_id)
await client.previous(chat_id)
await client.replay(chat_id)
```

`source` can be a local path, an `http(s)` URL, a Kurigram `Message`, or an `AudioSource`.

### Seeking

```python
await client.seek(chat_id, 90)        # absolute seconds → returns where we landed
await client.forward(chat_id, 10)     # skip forward (default 10s)
await client.rewind(chat_id, 10)      # skip backward (default 10s, clamped at start)
```

### Queue

```python
await client.shuffle(chat_id)
await client.clear_queue(chat_id)
await client.remove(chat_id, 2)       # drop the track at index 2
await client.move(chat_id, 4, 0)      # move index 4 to the front
await client.loop(chat_id, "off")     # "off" | "track" | "queue" | "shuffle"
await client.loop(chat_id, 3)         # repeat current track 3 more times
await client.loop(chat_id)            # read current mode
```

### Volume

```python
await client.volume(chat_id, 80)      # local gain, 0..200 (%)
await client.mute(chat_id)            # server-side
await client.unmute(chat_id)          # server-side
```

### Video

```python
await client.play_video(chat_id, "clip.mp4")   # stream video
await client.stop_video(chat_id)               # stop video, audio continues
```

### Introspection

```python
client.now_playing(chat_id)           # TrackInfo: title, state, position, duration…
client.position(chat_id)              # current playback position (seconds)
client.duration(chat_id)              # track duration (None for live)
client.volume(chat_id)                # current volume setting
client.playback_state(chat_id)        # PlaybackState: PLAYING | PAUSED | IDLE…
client.is_connected(chat_id)          # whether we're in the voice chat
client.get_call(chat_id)              # underlying GroupCall, or None
await client.get_stats(chat_id)       # CallStats: packets, bytes, frames, ICE state…
await client.get_participants(chat_id)# list of participants
client.active_calls                   # dict of all joined calls
len(client)                           # number of active calls
```

### Join / leave

```python
await client.join(chat_id)            # join without playing
await client.leave(chat_id)           # leave this chat
await client.set_title(chat_id, "Late night 🎧")   # rename the voice chat
```

## The queue

```python
from aytgcalls import AyLoop

await call.queue.add("a.mp3")
await call.queue.add("b.mp3", position=0)         # insert at the front
await call.queue.extend(["c.mp3", "https://example.com/d.mp3"])
await call.queue.remove(1)
await call.queue.clear()
await call.queue.shuffle()
await call.queue.move(2, 0)

call.queue.current            # AudioSource | None
call.queue.items              # tuple of upcoming tracks
call.queue.history            # recently finished tracks
await call.queue.next()       # advance (respects loop mode)
await call.queue.previous()   # step back through history

await call.loop(3)                  # repeat the current track 3 more times
await call.loop("track")            # forever
await call.loop("queue")            # whole queue
await call.loop("shuffle")          # shuffle + keep looping
await call.loop("off")
```

`loop()` accepts a count, a `LoopMode`, or any friendly word (`one`, `song`, `all`,
`playlist`, `repeat`, `shuffle`, `off`), so a chat command can be forwarded straight to
it. After `loop(3)` plays the track three more times the mode falls back to `off` on
its own.

`play()` returns `(track, started_now)` so a bot can reply either "playing" or "queued
at #3" without tracking state itself. When a track ends the next one starts on its own.

Transitions are near-gapless: the next FFmpeg process starts the moment the previous one
hits EOF, feeding the same ring buffer, so the 20 ms RTP cadence never breaks.

## Events

```python
@call.on_stream_end
async def _(call, source, reason):
    # reason: FINISHED | FAILED | STOPPED | TIMEOUT
    print("finished", source.display_name, reason.value)

@call.on_disconnect
async def _(call, reason):
    # reason: REQUESTED | CALL_ENDED | KICKED | TRANSPORT_FAILED | SFU_TIMEOUT | QUEUE_FINISHED
    print("disconnected", reason.value)
```

## Video / screen sharing

```python
await call.play_video("clip.mp4")       # stream a video file
await call.stop_video()                 # stop video, audio continues
await call.play("song.mp3")             # audio keeps going
```

The video track is encoded to H.264 by FFmpeg and sent on the dedicated presentation
SSRC (`ssrc-groups`). Requires Telegram's presentation join path.

## Bot command surface

See [`examples/bot_plus_assistant.py`](examples/bot_plus_assistant.py) for a complete
command set using `AyFac`:

```
/play <file|url>   or reply to a voice/audio message
/pause  /resume  /skip  /previous  /replay  /stop  /end
/seek <secs>  /forward [secs]  /rewind [secs]
/volume <0-200>  /mute  /unmute
/loop <n|track|queue|shuffle|off>
/remove <idx>  /move <from> <to>
/now   /queue   /participants
```

There is no `/join` and no `/add` — `/play` covers both.

## Configuration

```python
from aytgcalls import AyConfig

config = AyConfig(
    ffmpeg_path="ffmpeg",
    opus_bitrate=96_000,      # 64–128 kbps is Telegram's comfortable range
    buffer_ms=400,            # jitter buffer depth
    prefetch_ms=200,          # buffered before the first frame is released
    volume=100,
    ice_servers=(),           # Telegram's SFU is ICE-lite on a public IP; STUN not needed
    connect_timeout=20.0,
    keepalive_interval=10.0,  # phone.checkGroupCall
    auto_reconnect=True,
    reconnect_max_attempts=8,
)
call = AyCall(client, config=config)
```

Everything is also readable from the environment:

| Variable | Meaning |
|---|---|
| `API_ID`, `API_HASH`, `STRING_SESSION` | user session (required) |
| `BOT_TOKEN` | optional command bot |
| `AYTGCALLS_FFMPEG` | path to the ffmpeg binary |
| `AYTGCALLS_OPUS_BITRATE` | Opus bitrate in bits/s |
| `AYTGCALLS_BUFFER_MS`, `AYTGCALLS_PREFETCH_MS` | buffering |
| `AYTGCALLS_VOLUME` | initial volume percent |
| `AYTGCALLS_ICE_SERVERS` | comma-separated STUN/TURN URLs |
| `AYTGCALLS_CONNECT_TIMEOUT`, `AYTGCALLS_KEEPALIVE_INTERVAL` | timeouts |
| `AYTGCALLS_DEBUG=1` | verbose logging with redacted signaling JSON |

```python
from aytgcalls import enable_debug
enable_debug()
```

### Automation you can turn off

```python
AyConfig(
    auto_join=True,          # play() joins by itself
    auto_leave=True,         # leave when the queue runs out
    auto_leave_delay=3.0,    # grace period, so a quick next request keeps the call
)
```

The grace period matters: if a user queues another song within `auto_leave_delay`, the
pending leave is cancelled and the call stays up.

## Error handling

```
AytgcallsError
├── BotClientNotAllowed          a bot session was used
├── GroupCallNotFound            no active voice chat in that chat
├── NotInGroup                   the account cannot access the chat
├── AlreadyJoined / NotJoined
├── AlreadyPlaying / NotPlaying
├── InvalidAudioSource / MediaSourceError
├── FFmpegError / FFmpegNotInstalled
├── OpusError
├── TransportError
│   ├── ICEFailed
│   └── DTLSHandshakeFailed
└── TelegramCallError            wraps DATA_JSON_INVALID, GROUPCALL_INVALID,
                                 GROUPCALL_FORBIDDEN, CHAT_ADMIN_REQUIRED,
                                 GROUPCALL_SSRC_DUPLICATE_MUCH, JOIN_AS_PEER_INVALID
```

`TelegramCallError` attaches a plain-English explanation to every known RPC error id:

```python
from aytgcalls.exceptions import AytgcallsError

try:
    await client.play(chat_id, "song.mp3")
except AytgcallsError as exc:
    await message.reply(f"❌ {exc}")
```

## Verifying it works on a VPS

```bash
export API_ID=... API_HASH=... STRING_SESSION=...
export TEST_CHAT_ID=-1001234567890         # a chat with a RUNNING voice chat
python examples/scripts/live_check.py
```

It joins, streams a tone, asserts that RTP packets actually left the host at the
expected 50 packets/second, prints the stats, and leaves cleanly. Exit code 0 = pass.

```
✅ preflight: ffmpeg, libopus, no py-tgcalls
✅ user session: id=… @…
✅ joined. ssrc=1735203981
✅ ICE=completed  DTLS=connected
   packets_sent=601 (+600)  bytes=147840  frames=600 silence=3 underruns=3
✅ RTP is flowing at the expected 20 ms cadence
✅ real audio frames (not silence) reached the encoder
✅ left cleanly
```

## Troubleshooting

**Nobody can hear anything, but `packets_sent` keeps rising.**
The media path is fine; you're almost certainly *server-side muted*. In a group where
members join muted, an admin must unmute the assistant, or the account needs speaking
rights. Watch the logs for `Server-side muted with can_self_unmute=False`. You can also
try `await call.mute(False)`.

**`ICEFailed: ICE did not connect`.**
Outbound UDP is blocked. Telegram's media servers need arbitrary outbound UDP (ports
vary, commonly 40000–65535) to `91.108.x.x` / `149.154.x.x`. Cloud firewalls that only
allow TCP will fail here. There's no TCP fallback in this package.

**`DTLSHandshakeFailed`.**
ICE succeeded but the handshake didn't. Usually a stale call — leave, wait a few
seconds, rediscover and rejoin (`auto_reconnect=True` does this for you). Also check
your clock is correct — certificate validity is time-sensitive.

**`CHAT_ADMIN_REQUIRED`.**
The account lacks rights for that action. Promote the assistant, or ask an admin to
allow members to speak.

**`GROUPCALL_SSRC_DUPLICATE_MUCH`.**
A previous session never left cleanly. AyCalls picks a fresh SSRC per join; wait a few
seconds and retry.

**`FFmpegNotInstalled`.**
`apt install ffmpeg`, or set `AYTGCALLS_FFMPEG=/path/to/ffmpeg`.

**`GroupCallNotFound`.**
The voice chat isn't running (or is scheduled for later). AyCalls joins existing calls;
it doesn't create them.

**Choppy audio / lots of `underruns` in the stats.**
Increase `buffer_ms` / `prefetch_ms`, especially for remote URLs on a slow link.

**`ImportError` mentioning `pyrogram`.**
Two forks are installed at once. See
[Remove conflicting Pyrogram forks](#remove-conflicting-pyrogram-forks).

**`TransportError: … RTMP/stream broadcast`.**
Those calls don't accept WebRTC publishers at all — you'd have to push to the RTMP URL
from `phone.getGroupCallStreamRtmpUrl`. See `PROTOCOL.md` §7.

## What's implemented, what's not

Implemented:

- Discovery, join, keepalive, leave, mute/unmute, volume, update handling, reconnect
- One-call automation: `play()` auto-joins, auto-queues, the call auto-leaves when the
  queue empties
- Full playback control: play / pause / resume / skip / previous / replay / stop / end,
  seek / forward / rewind, loop (count / track / queue / shuffle), live position + duration
- Telegram voice notes and audio messages as sources (downloaded and cleaned up for you)
- JSON ⇄ SDP bridge (both directions, round-trip tested)
- ICE (controlling) + DTLS-SRTP (client) + RTP with pinned SSRC and the SFU's payload type
- FFmpeg → PCM → ring buffer → gain → Opus 20 ms → paced RTP
- Queue with loop / shuffle / history, near-gapless transitions, deterministic teardown
- Video / screen sharing via presentation SSRC (H.264, FFmpeg-encoded)
- Participant listing and voice-chat renaming

Deliberately not implemented:

- **Receiving** other participants' audio (we publish only)
- **RTMP / stream-mode** calls (detected and rejected with a clear error)
- **E2E conference calls** (`public_key` / `block` arguments of `phone.joinGroupCall`)

## Development

```bash
git clone https://github.com/Sinchu-xD/AyCalls.git
cd AyCalls
pip install -e ".[dev]"
pytest -q          # 274 tests, no network required
ruff check .
mypy aytgcalls
```

Two of the test modules go further than unit tests:

- `examples/tests/test_loopback.py` stands up a second aiortc peer locally, serialises
  its parameters into exactly the JSON shape Telegram sends, and runs a real
  ICE + DTLS-SRTP + Opus RTP session against it.
- `examples/tests/test_integration.py` drives the **public API** — `play()` →
  `pause` / `resume` / `skip` / `volume` → `leave()` — against a fake Kurigram client
  that returns real TL objects and hands off to that local peer. It asserts the far end
  decodes 48 kHz audio, that `phone.checkGroupCall` keepalives run, that
  `phone.leaveGroupCall` gets our SSRC, and that no tasks or FFmpeg processes leak.

What neither can prove is that Telegram's *production* SFU accepts the join payload —
that's what `examples/scripts/live_check.py` is for.

## License

MIT — see [LICENSE](LICENSE).

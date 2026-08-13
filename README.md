# aytgcalls

Play audio into **Telegram group voice chats** from Python.

* **Kurigram** for MTProto signaling (`phone.*`)
* **aiortc** for real media transport — ICE → DTLS-SRTP → RTP/Opus
* **FFmpeg** for decoding anything into 48 kHz stereo PCM
* **No py-tgcalls, no tgcalls, no Telethon.** Not wrapped, not vendored, not a dependency.

```python
from aytgcalls import AyCall

call = AyCall(user_client, chat_id)   # a Kurigram USER session
await call.play("song.mp3")           # joins, plays, queues, and leaves — all on its own
```

No `join()`, no `add()`, no `leave()`. `play()` is the whole API:

| You call | It handles |
|---|---|
| `await call.play(src)` | **joins** the voice chat if needed |
| `await call.play(src)` again while busy | **queues** it (no separate `add`) |
| track finishes | **advances** the queue by itself |
| queue runs out | **leaves** the voice chat automatically |
| `await call.play(message)` | **downloads** a Telegram voice note / audio and plays it |

---

## Table of contents

- [How it works](#how-it-works)
- [A bot cannot join a voice chat](#a-bot-cannot-join-a-voice-chat)
- [Installation](#installation)
  - [Removing conflicting Pyrogram forks](#removing-conflicting-pyrogram-forks)
  - [FFmpeg and libopus](#ffmpeg-and-libopus)
- [Generating a STRING_SESSION](#generating-a-string_session)
- [Quick start](#quick-start)
- [API reference](#api-reference)
- [Queue](#queue)
- [Events](#events)
- [Multiple chats](#multiple-chats)
- [Bot command interface + assistant](#bot-command-interface--assistant)
- [Configuration](#configuration)
- [Error handling](#error-handling)
- [Verifying it works on a VPS](#verifying-it-works-on-a-vps)
- [Troubleshooting](#troubleshooting)
- [What is and is not implemented](#what-is-and-is-not-implemented)
- [Development](#development)

---

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
   ┌──────────────────────────┴───────────────────────────┐
   │  ICE (controlling)  →  DTLS (client)  →  SRTP keys   │
   └──────────────────────────┬───────────────────────────┘
                              ▼
 file/URL ─ FFmpeg ─► PCM s16le 48k/2 ─► ring buffer ─► gain ─► Opus 20 ms ─► RTP ─► SFU
```

The full protocol write-up, including which parts are certain and which need live
verification, is in [`PROTOCOL.md`](PROTOCOL.md).

## A bot cannot join a voice chat

This is a Telegram limitation, not a library one. `phone.joinGroupCall` is user-only.

So `aytgcalls` uses the standard two-account pattern:

| Account | Role |
|---|---|
| **User** ("assistant"), via `API_ID` + `API_HASH` + `STRING_SESSION` | Joins the call and streams audio. **Required.** |
| **Bot**, via `BOT_TOKEN` | Optional. Parses `/play`, `/skip`… and dispatches to the assistant. Never touches the call. |

`GroupCall` checks this at join time and raises `BotClientNotAllowed` with an explanation
if you hand it a bot session.

## Installation

```bash
pip install aytgcalls
```

### Removing conflicting Pyrogram forks

Kurigram is a maintained Pyrogram fork and **still imports as `pyrogram`**. If the real
`pyrogram` or `pyrofork` is installed alongside it, they overwrite each other's files and
you get bizarre import errors. Remove them first:

```bash
pip uninstall -y pyrogram pyrofork tgcalls py-tgcalls pytgcalls
pip install -U kurigram aytgcalls
```

Optional speedup (recommended):

```bash
pip install "aytgcalls[fast]"     # adds tgcrypto
```

Verify:

```bash
python -c "import pyrogram; print(pyrogram.__version__)"   # e.g. 2.2.24 (kurigram)
python -c "import importlib.util; assert importlib.util.find_spec('tgcalls') is None; print('no tgcalls ✓')"
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

Check:

```bash
ffmpeg -version
python -c "from aytgcalls.media.opus import opus_available; print('opus ✓' if opus_available() else 'opus ✗')"
```

If `ffmpeg` is not on `PATH`, point at it explicitly:

```bash
export AYTGCALLS_FFMPEG=/usr/local/bin/ffmpeg
```

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
> account. Keep it in an environment variable or a secret manager. `aytgcalls` never
> hardcodes credentials and never logs them — debug logging redacts ICE passwords, DTLS
> fingerprints, access hashes and session strings.

## Quick start

```python
import asyncio
from aytgcalls import AyCall, AyCreds
from aytgcalls.telegram import build_user_client

async def main():
    client = build_user_client(AyCreds.from_env())   # API_ID/API_HASH/STRING_SESSION
    await client.start()

    call = AyCall(client, -1001234567890)   # the voice chat must already be running
    await call.play("song.mp3")             # joins + plays; returns immediately

    await asyncio.sleep(30)
    await call.end()                        # or just let it auto-leave
    await client.stop()

asyncio.run(main())
```

For a bot serving many chats, `AyFac` collapses it to a single line per request:

```python
from aytgcalls import AyFac

ay = AyFac(client)
await ay.play(chat_id, "song.mp3")      # creates, joins, plays or queues
await ay.skip(chat_id)
await ay.loop(chat_id, 3)
await ay.stop(chat_id)                  # stop + leave
```

Local files, URLs, **Telegram voice notes / audio messages**, and anything FFmpeg can
decode all work:

```python
await call.play("song.mp3")
await call.play("/music/track.flac")
await call.play("https://example.com/audio.mp3")
await call.play("https://example.com/live-radio.aac")

await call.play(message)                    # a Kurigram Message: voice / audio / document
await call.play(message.reply_to_message)   # "reply to a voice note with /play"
await call.play(message.voice)              # or the media object directly
```

Telegram media is downloaded through the same MTProto session that is already in the call,
into a temp directory, and **deleted as soon as the track finishes** — nothing piles up on
disk. A looping track keeps its file until the loop ends.

## API reference

```python
call = AyCall(user_client, chat_id, config=AyConfig())

# --- the only thing you normally call ------------------------------------------
await call.play(source)                 # joins / plays / queues, all automatic
await call.play(source, force=True)     # jump the queue and start now
await call.play(source, chat_id=...)    # if the chat id was not given to AyCall()

# --- transport ----------------------------------------------------------------
await call.pause()
await call.resume()
await call.skip()                       # -> next AudioSource or None
await call.previous()                   # -> back through the history
await call.stop()                       # stop + leave   (same as end())
await call.stop_playback()              # stop but stay in the call
await call.end()

# --- seeking -----------------------------------------------------------------
await call.seek(90)                     # absolute, seconds -> where we landed
await call.forward(10)                  # relative
await call.rewind(10)                   # relative, clamped at 0
await call.replay()                     # restart the current track

# --- repeat / shuffle ---------------------------------------------------------
await call.loop(3)                      # repeat the current track 3 more times
await call.loop("track")                # repeat forever
await call.loop("queue")                # loop the whole queue
await call.loop("shuffle")              # shuffle now, then keep looping
await call.loop("off")
await call.loop()                       # read the current mode
call.loop = "queue"                     # assignment works too

# --- sound -------------------------------------------------------------------
await call.set_volume(80)               # local gain, 0..200 (%)
await call.set_volume(80, server_side=True)   # also phone.editGroupCallParticipant
await call.mute()
await call.unmute()

# --- queue -------------------------------------------------------------------
await call.shuffle()
await call.clear_queue()

# --- state -------------------------------------------------------------------
call.is_connected · call.is_playing · call.is_paused
call.position · call.duration · call.volume · call.loop_mode
call.now_playing        # AyTrack: title, state, position, duration, progress bar…
call.playback_state · call.ssrc · call.chat_id
await call.get_stats()  # AyStats(packets_sent, bytes_sent, frames_encoded, ice_state, …)
call.debug_snapshot()   # JSON string for bug reports (no secrets)

# manual control is still there if you want it
await call.join(chat_id) ; await call.leave()

async with AyCall(user_client, chat_id) as call:   # leaves on exit
    ...
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

Every control lives on `AyCall`, so a consumer only ever imports `aytgcalls`:

```python
from aytgcalls import AyCall

call = AyCall(user_client, chat_id)
await call.play("https://example.com/song.mp3")   # joins, plays; queues if busy
await call.forward(30)
await call.loop(2)
print(call.now_playing)     # song.mp3 [playing] 00:47 / 03:52 ▬▬▬▬🔘▬▬▬▬▬▬▬▬▬▬▬
# no leave() needed — the call exits when the queue is done
```

### Names

The public names are all `Ay*`; the longer descriptive names still work.

| Import | Also available as |
|---|---|
| `AyCall` | `GroupCall` |
| `AyFac` | `GroupCallFactory` |
| `AyConfig` | `CallConfig` |
| `AyCreds` | `TelegramCredentials` |
| `AyLoop` | `LoopMode` |
| `AyTrack` | `TrackInfo` |
| `AySource` | `AudioSource` |
| `AyState` | `PlaybackState` |
| `AyStats` | `CallStats` |
| `AyPlayer`, `AyQueue` | `Player`, `TrackQueue` |

### Now playing

`call.now_playing` returns a `TrackInfo` built for chat replies:

```python
info = call.now_playing
info.title                       # "song.mp3"
info.state                       # PlaybackState.PLAYING
info.position, info.duration     # 47.2, 232.0   (duration None => live)
info.progress                    # 0.203  (None for live)
info.queued, info.loop, info.volume
info.is_live
info.format_time(info.position)  # "00:47"
info.progress_bar()              # "▬▬▬▬🔘▬▬▬▬▬▬▬▬▬▬▬"  ("🔴 LIVE" when live)
str(info)                        # one-line summary
```

### How seeking works

`position` counts frames that have actually been **sent**, not frames decoded — the reader
runs up to `buffer_ms` ahead of the sender, so those two differ. Seeking picks a strategy
per source:

| Source | Strategy | Notes |
|---|---|---|
| Local file | FFmpeg `-ss` | Fast and frame-accurate. |
| URL, self-framing format (MP3, ADTS AAC, MPEG-TS) with `Accept-Ranges` | HTTP `Range` byte offset | Offset estimated from duration + length: exact for CBR, resyncs within a frame or two for VBR. No re-downloading. |
| URL, header-dependent format (WAV, FLAC, OGG/Opus, M4A, WebM) | FFmpeg `-ss` on the piped stream | A byte slice would cut away the header the decoder needs, so correctness wins over speed: the stream is re-read from the start and discarded up to the seek point. |
| Live stream (no discoverable duration) | not seekable | Raises `NotPlaying`. |

`duration` is discovered in the background via PyAV (which links its own FFmpeg libraries,
so it works even where the `ffmpeg` binary cannot resolve DNS) and is `None` until known.

## Queue

```python
from aytgcalls import LoopMode

await call.queue.add("a.mp3")
await call.queue.add("b.mp3", position=0)
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
`playlist`, `repeat`, `shuffle`, `off`), so a chat command can be forwarded straight to it.
After `loop(3)` plays the track three more times the mode falls back to `off` by itself.

`play()` returns `(track, started_now)` so a bot can reply either "playing" or "queued at
#3" without tracking state itself. When a track ends the next one starts on its own — no
polling and no `on_stream_end` bookkeeping required.

Transitions are near-gapless: the next FFmpeg process starts the moment the previous one
hits EOF, feeding the same ring buffer, so the 20 ms RTP cadence never breaks.

## Events

```python
@call.on_stream_end
async def _(call, source, reason):
    # reason: COMPLETED | SKIPPED | STOPPED | ERROR
    print("finished", source.display_name, reason.value)

@call.on_disconnect
async def _(call, reason):
    # reason: REQUESTED | CALL_ENDED | KICKED | TRANSPORT_FAILED | SFU_TIMEOUT
    print("disconnected", reason.value)
```

## Multiple chats

```python
from aytgcalls import GroupCallFactory

manager = GroupCallFactory(user_client)
call = await manager.get_or_create(-1001234567890)
await call.play("song.mp3")

await manager.leave(-1001234567890)
await manager.leave_all()
```

Each call gets its own transport and its own FFmpeg pipeline; the practical limit is CPU
and bandwidth, not the protocol.

## Bot command interface + assistant

See [`examples/bot_plus_assistant.py`](examples/bot_plus_assistant.py) for a complete
command surface — the bot parses the commands and the user session does the streaming:

```
/play <file|url>   or reply to a voice/audio message      /now   /queue
/pause  /resume  /skip  /previous  /replay  /stop
/seek <secs>  /forward [secs]  /rewind [secs]
/volume <0-200>  /mute  /unmute
/loop <n|track|queue|shuffle|off>
```

There is no `/join` and no `/add` — `/play` covers both.

## Configuration

```python
from aytgcalls import CallConfig

config = CallConfig(
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
call = GroupCall(client, config=config)
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

`TelegramCallError` attaches an explanation to every known RPC error id:

```python
from aytgcalls.exceptions import AytgcallsError

try:
    await call.join(chat_id)
    await call.play(source)
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

**Nobody can hear anything, but `packets_sent` keeps rising**
The media path is fine; you are almost certainly *server-side muted*. In a group where
members join muted, an admin must unmute the assistant, or the account needs speaking
rights. Watch the logs for `Server-side muted with can_self_unmute=False`. You can also
try `await call.mute(False)`.

**`ICEFailed: ICE did not connect`**
Outbound UDP is blocked. Telegram's media servers need arbitrary outbound UDP (ports vary,
commonly the 40000–65535 range) to `91.108.x.x` / `149.154.x.x`. Cloud firewalls that only
allow TCP will fail here. There is no TCP fallback in this package.

**`DTLSHandshakeFailed`**
ICE succeeded but the handshake did not. Usually a stale call: leave, wait a few seconds,
rediscover and rejoin (`auto_reconnect=True` does this for you). Check your clock is
correct — certificate validity is time-sensitive.

**`CHAT_ADMIN_REQUIRED`**
The account lacks rights for that action. Promote the assistant, or ask an admin to allow
members to speak.

**`GROUPCALL_SSRC_DUPLICATE_MUCH`**
A previous session never left cleanly. `aytgcalls` picks a fresh SSRC per join; wait a few
seconds and retry.

**`FFmpegNotInstalled`**
`apt install ffmpeg`, or set `AYTGCALLS_FFMPEG=/path/to/ffmpeg`.

**`GroupCallNotFound`**
The voice chat is not running (or is scheduled for later). This package joins existing
calls; it does not create them.

**Choppy audio / lots of `underruns` in the stats**
Increase `buffer_ms` / `prefetch_ms`, especially for remote URLs on a slow link.

**`ImportError` mentioning `pyrogram`**
Two forks are installed at once. See
[Removing conflicting Pyrogram forks](#removing-conflicting-pyrogram-forks).

**The call is an RTMP broadcast**
`TransportError: … RTMP/stream broadcast`. Those calls do not accept WebRTC publishers at
all; you would have to push to the RTMP URL from `phone.getGroupCallStreamRtmpUrl`.
See `PROTOCOL.md` §7.

## What is and is not implemented

Implemented:

* discovery, join, keepalive, leave, mute/unmute, volume, update handling, reconnect
* one-call automation: `play()` auto-joins, auto-queues and the call auto-leaves when the
  queue empties
* full playback control: play / pause / resume / skip / previous / replay / stop / end,
  seek / forward / rewind, loop (count·track·queue·shuffle), live position + duration
* Telegram voice notes and audio messages as sources, downloaded and cleaned up for you
* JSON ⇄ SDP bridge (both directions, round-trip tested)
* ICE (controlling) + DTLS-SRTP (client) + RTP with pinned SSRC and the SFU's payload type
* FFmpeg → PCM → ring buffer → gain → Opus 20 ms → paced RTP
* queue with loop/shuffle/history, near-gapless transitions, deterministic teardown

Not implemented, deliberately:

* **Receiving** other participants' audio (we publish only)
* **Video / screen sharing** (`phone.joinGroupCallPresentation`, `ssrc-groups`)
* **RTMP / stream-mode** calls (detected and rejected with a clear error)
* **E2E conference calls** (`public_key` / `block` arguments of `phone.joinGroupCall`)

## Development

```bash
git clone … && cd aytgcalls
pip install -e ".[dev]"
pytest -q          # 246 tests, no network required
ruff check .
mypy aytgcalls
```

Two of the test modules go further than unit tests:

* `examples/tests/test_loopback.py` stands up a second aiortc peer locally, serialises its
  parameters into exactly the JSON shape Telegram sends, and runs a real
  ICE + DTLS-SRTP + Opus RTP session against it.
* `examples/tests/test_integration.py` drives the **public API** — `play()` →
  `pause`/`resume`/`skip`/`volume` → `leave()` — against a fake Kurigram client that
  returns real TL objects and hands off to that local peer. It asserts the far end
  decodes 48 kHz audio, that `phone.checkGroupCall` keepalives run, that
  `phone.leaveGroupCall` gets our SSRC, and that no tasks or FFmpeg processes leak.

What neither can prove is that Telegram's *production* SFU accepts the join payload; that
is what `examples/scripts/live_check.py` is for.

## License

MIT — see [LICENSE](LICENSE).

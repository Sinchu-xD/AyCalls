# Telegram Group Voice Chat — Protocol Analysis (Phase 0)

This document is the written analysis that precedes the implementation. It describes how
`aytgcalls` talks to Telegram's group-call SFU using **Kurigram** (MTProto) for signaling and
**aiortc** (ICE / DTLS-SRTP / RTP) for media, with **no dependency on py-tgcalls / tgcalls**.

Every section ends with a **Confidence** marker:

* **CERTAIN** — documented in Telegram's public API docs or verifiable from the TL schema shipped
  with Kurigram (we introspected it: see §8).
* **NEEDS LIVE VERIFICATION** — derived from the public protocol shape, but the exact server
  behaviour can only be confirmed against a real group call with a real user session.
* **IMPOSSIBLE / LIMITATION** — cannot be done, with the reason stated plainly.

---

## 0. Account model (why a bot cannot do this)

`phone.joinGroupCall` requires a `join_as: InputPeer` and operates on the *call participant* set.
Bot accounts are not permitted to become group-call participants — the Telegram API surface for
voice chats is user-only. There is no bot method to publish audio into a group call, and no bot
token can be exchanged for a call participation right.

Consequence for this package:

* The client passed to `GroupCall(client)` **must** be a Kurigram *user* session
  (API_ID + API_HASH + STRING_SESSION).
* A bot token may only drive the *command* surface (`/play`, `/skip`), which then dispatches to the
  user ("assistant") session that is actually in the call.
* `GroupCall` validates `client.me.is_bot` at join time and raises `BotClientNotAllowed`.

**Confidence: CERTAIN** (this is the same constraint every user-bot music project works around,
and it falls directly out of the API surface).

---

## 1. Discovering the active group call

A group call is addressed by an `InputGroupCall(id, access_hash)`. It is *not* the chat id. To find
it:

| Chat type | Method | Field |
|---|---|---|
| Channel / supergroup | `channels.GetFullChannel(channel=InputChannel)` | `full_chat.call` |
| Basic group | `messages.GetFullChat(chat_id=int)` | `full_chat.call` |

`full_chat.call` is `InputGroupCall` when a voice chat is currently active, and `None` otherwise.
`None` ⇒ `GroupCallNotFound` (there is no way to "start" playback into a call that does not exist;
creating one requires `phone.CreateGroupCall` and admin rights).

State for that call is then fetched with:

```
phone.GetGroupCall(call=InputGroupCall, limit=<int>)
  -> phone.GroupCall{ call: GroupCall, participants: [GroupCallParticipant], ... }
```

`GroupCall` carries `participants_count`, `schedule_date` (set ⇒ the call is scheduled but not
started), `rtmp_stream` (⇒ the call is an RTMP broadcast, see §7), `join_muted`,
`can_change_join_muted`, `listeners_count`, `version`.

`phone.GetGroupCallJoinAs(peer)` returns the peers you are allowed to join as; `InputPeerSelf()` is
the default and is valid for a normal user account in a group it belongs to.

**Confidence: CERTAIN.** Implemented in `aytgcalls/telegram/discovery.py`.

---

## 2. Joining — `phone.JoinGroupCall`

TL signature as shipped in Kurigram 2.2.24 (introspected, §8):

```
phone.JoinGroupCall(
    call: InputGroupCall,
    join_as: InputPeer,
    params: DataJSON,
    muted: bool = None,
    video_stopped: bool = None,
    invite_hash: str = None,
    public_key: int = None,     # E2E conference calls only — unused here
    block: bytes = None,        # E2E conference calls only — unused here
)
```

`params` is a `DataJSON{data: "<json string>"}`. For an **audio-only publisher** the JSON is:

```json
{
  "ssrc": 1234567890,
  "ufrag": "Ku4t",
  "pwd": "0Nx0/4rHqQhoUlVfEPHNqRSU",
  "fingerprints": [
    {
      "hash": "sha-256",
      "fingerprint": "AB:CD:...:EF",
      "setup": "active"
    }
  ]
}
```

Rules that matter:

* `ssrc` is a **random non-zero int32** (we generate it in `[1, 2**31-1]` to stay unambiguously
  positive; the field is signed in `phone.LeaveGroupCall.source` and `phone.CheckGroupCall.sources`,
  so keeping it inside signed-positive range avoids sign-conversion bugs).
* `ufrag` / `pwd` are the **local ICE** credentials. They must be the credentials the local ICE
  agent actually uses, otherwise STUN binding requests will be rejected (`USERNAME` mismatch).
* `fingerprints[0].fingerprint` is the **local DTLS certificate fingerprint**, colon-separated
  uppercase hex, matching `hash: "sha-256"`.
* `setup: "active"` means *we* are the DTLS client and will send the `ClientHello`. The SFU answers
  `setup: "passive"`. `aytgcalls` sends `active` and additionally *tolerates* the server replying
  `active`/`actpass` by flipping its own role (see `transport/webrtc.py::_dtls_role_for`).
* **`"ssrc-groups"` must be omitted entirely** for audio-only publishing. Sending an empty array has
  been observed to be rejected; sending video groups without publishing video desynchronises the
  SFU's expectation of your simulcast layers.
* `muted=True` at join is allowed and is what a "listener" does; a publisher joins with
  `muted=False`. Note that even when `muted=False` you must additionally *not* be
  server-side-muted (`GroupCallParticipant.muted` + `can_self_unmute`), see §5.

The response is an `Updates` container. The interesting update is
`UpdateGroupCallConnection{presentation: flag, params: DataJSON}`; older layers/servers also deliver
the same payload as the return value of the method. `aytgcalls` scans `updates.updates` for
`UpdateGroupCallConnection` **and** falls back to any `DataJSON` on the result object, so both
shapes work.

**Confidence: CERTAIN for the TL signature and the field names. NEEDS LIVE VERIFICATION for the
exact rejection semantics of `ssrc-groups` and for `setup` role negotiation, since those are server
behaviours.**

---

## 3. Parsing the join response

`UpdateGroupCallConnection.params.data` is a JSON string. For a normal WebRTC call:

```json
{
  "transport": {
    "ufrag": "9aBc",
    "pwd": "server-ice-password",
    "fingerprints": [
      {"hash": "sha-256", "fingerprint": "11:22:...", "setup": "passive"}
    ],
    "candidates": [
      {
        "generation": "0", "component": "1", "protocol": "udp",
        "port": "44445", "ip": "91.108.9.1", "foundation": "1",
        "id": "6da76b9dd4", "priority": "2130706431",
        "type": "host", "network": "0"
      }
    ],
    "rtcp-mux": true,
    "xmlns": "urn:xmpp:jingle:transports:ice-udp:1"
  },
  "audio": {
    "ssrc": 987654321,
    "payload-types": [
      {
        "id": 111, "name": "opus", "clockrate": 48000, "channels": 2,
        "rtcp-fbs": [{"type": "transport-cc"}],
        "parameters": {"minptime": "10", "useinbandfec": "1"}
      }
    ],
    "rtp-hdrexts": [
      {"id": 1, "uri": "urn:ietf:params:rtp-hdrext:ssrc-audio-level"},
      {"id": 3, "uri": "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"}
    ]
  }
}
```

Observed variations that the parser must absorb (all handled in `transport/sdp.py`):

* `payload-types` / `rtp-hdrexts` sometimes appear at the **top level** instead of nested under
  `audio`.
* Candidate scalar fields arrive as **strings** (`"port": "44445"`) — they must be coerced.
* `transport.candidates` may be absent on the first payload and delivered later via a subsequent
  `UpdateGroupCallConnection`.
* `{"stream": true}` (optionally `{"stream": true, "rtmp": true}`) means the call is a **broadcast**,
  not a WebRTC conference — see §7.

### JSON → SDP answer

Because the SFU speaks JSON rather than SDP, we synthesise a unified-plan SDP *answer* so a standard
WebRTC stack can consume it. Single audio m-line, `rtcp-mux`, bundle:

```
v=0
o=- <session-id> 2 IN IP4 0.0.0.0
s=-
t=0 0
a=group:BUNDLE 0
a=msid-semantic: WMS *
m=audio 9 UDP/TLS/RTP/SAVPF 111
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:9aBc
a=ice-pwd:server-ice-password
a=ice-lite
a=fingerprint:sha-256 11:22:...
a=setup:passive
a=mid:0
a=recvonly
a=rtcp-mux
a=rtpmap:111 opus/48000/2
a=rtcp-fb:111 transport-cc
a=fmtp:111 minptime=10;useinbandfec=1
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=candidate:1 1 udp 2130706431 91.108.9.1 44445 typ host generation 0
a=end-of-candidates
```

`a=recvonly` is from the SFU's point of view: we send, it receives.

### SDP offer → JSON

The reverse direction takes a local offer and extracts `a=ice-ufrag`, `a=ice-pwd`,
`a=fingerprint`, `a=setup` and `a=ssrc:<n> cname:...` to build the §2 payload.

`transport/sdp.py` implements **both** directions plus a normalising round-trip
(`json → SDP → json` is asserted equal in `examples/tests/test_sdp.py`).

**Confidence: CERTAIN for the SDP grammar. NEEDS LIVE VERIFICATION for the exact key set the current
production SFU emits — the parser is deliberately tolerant rather than strict.**

---

## 4. Media transport

Pipeline: **ICE → DTLS → SRTP → RTP(Opus)**.

1. **ICE.** We are the *controlling* agent. Telegram's SFU behaves as an **ICE-lite** peer: it
   publishes only `host` candidates on public IPs and never initiates connectivity checks; it just
   answers our binding requests and latches onto the source address. aiortc's `RTCIceGatherer`
   hardcodes `ice_controlling=False`, so `transport/ice.py` explicitly flips
   `connection.ice_controlling = True` and sets `remote_is_lite` via
   `RTCIceParameters(iceLite=True)`. Because the SFU is on a public IP, a local *host* candidate is
   sufficient for the usual NAT case (our outbound check opens the mapping). STUN servers are
   therefore **not** required and are off by default; they remain configurable.
2. **DTLS.** We take the `client` role (`setup: active`), the SFU takes `server` (`setup: passive`).
   aiortc's ORTC-level `RTCDtlsTransport` would otherwise derive the role from the ICE role
   (controlling ⇒ server), which is the *wrong* answer here, so the role is set explicitly.
   SRTP profiles are negotiated by the DTLS handshake (`SRTP_AES128_CM_SHA1_80`).
3. **SRTP/RTP.** `RTCRtpSender` encrypts and paces out RTP with the negotiated payload type, our
   join SSRC, and the header extensions the SFU advertised (aiortc natively supports
   `ssrc-audio-level`, `sdes:mid` and `transport-wide-cc`; anything else it advertises is dropped
   from our answer rather than blindly echoed).

### How Telegram's SFU differs from vanilla WebRTC

| Vanilla WebRTC | Telegram group call |
|---|---|
| SDP offer/answer over your own signaling | JSON blobs over MTProto (`phone.*`) |
| Trickle ICE both directions | Server candidates arrive in the join response / later updates; we never trickle ours to the server |
| Peer may be `actpass` | Server is effectively always `setup: passive` (DTLS server) |
| Renegotiation via new offers | No renegotiation: to change media you re-join |
| Full ICE peer | ICE-lite-style: host candidates only, no outbound checks |
| One PeerConnection per peer | One connection to the SFU carrying every participant, demuxed by SSRC |
| Bundle + multiple m-lines | Single audio m-line for an audio-only publisher |

Because of the last two rows, `aytgcalls` does **not** use `RTCPeerConnection`. Driving the
offer/answer state machine would force us to munge SDP that aiortc regenerates internally (in
particular to pin the Opus payload type to the SFU's `111` and to pin the SSRC to the one we already
committed to in `phone.joinGroupCall`). Instead we use aiortc's supported ORTC-level objects —
`RTCIceGatherer`, `RTCIceTransport`, `RTCDtlsTransport`, `RTCRtpSender` — which let us set the ICE
credentials, DTLS role, payload type and SSRC exactly. `sdp.py` is still a real, tested,
public part of the API (and is what the analysis above describes), it simply is not required to be
in the hot path.

**Confidence: CERTAIN for the mechanism. NEEDS LIVE VERIFICATION that the current SFU accepts an
aiortc-generated DTLS `ClientHello` and our SRTP profile list — this is the single highest-risk step
and is exactly what `examples/scripts/live_check.py` exists to prove.**

---

## 5. Keepalive, state and teardown

| Purpose | Method |
|---|---|
| Keepalive / liveness | `phone.CheckGroupCall(call, sources=[our_ssrc])` — returns the subset of sources the server still considers connected. An empty list ⇒ the SFU dropped us ⇒ reconnect. |
| Mute / unmute / volume | `phone.EditGroupCallParticipant(call, participant=InputPeerSelf(), muted=..., volume=...)`. `volume` is server-side and expressed in hundredths of a percent (`10000` = 100%). |
| Leave | `phone.LeaveGroupCall(call, source=our_ssrc)` |

Updates to handle (`telegram/updates.py`):

* `UpdateGroupCall` — call ended/discarded (`GroupCallDiscarded`) or settings changed.
* `UpdateGroupCallParticipants` — our participant record: `muted`, `can_self_unmute`, `left`,
  `volume`, `just_joined`.
* `UpdateGroupCallConnection` — additional/refreshed transport parameters (late candidates).

**Confidence: CERTAIN for the methods and update names (verified against the Kurigram TL schema).
NEEDS LIVE VERIFICATION for the polling interval the SFU expects — we default to 10 s, which is
conservative.**

---

## 6. Audio parameters Telegram expects

| Parameter | Value |
|---|---|
| Codec | Opus |
| Sample rate | 48 000 Hz |
| Channels | 2 (stereo) |
| Frame duration | 20 ms ⇒ **960 samples per channel** per frame |
| Bitrate | 64–128 kbps (we default to 96 kbps) |
| RTP timestamp increment | +960 per frame (clock rate 48 000) |
| RTP sequence | monotonically increasing, random start |
| SSRC | must equal the `ssrc` sent in `phone.joinGroupCall` |
| Payload type | whatever the SFU advertised (normally 111) |
| Pacing | one packet every 20 ms of wall clock, drift-corrected |

Sequence numbers, timestamps and SRTP are produced by `RTCRtpSender`; the 20 ms wall-clock pacing
and the 960-sample framing are produced by `transport/track.py`, which is what feeds the sender.
When the player is paused or the buffer underruns the track emits **silence frames** rather than
stalling — stopping the RTP flow makes the SFU consider the source dead.

**Confidence: CERTAIN.**

---

## 7. Known limitations — stated plainly

1. **Bots cannot join voice chats.** Not a bug, not fixable. §0.
2. **RTMP / "stream mode" calls cannot be published into with WebRTC.** If the join response is
   `{"stream": true}` the call is a broadcast: media is exchanged as downloadable chunks
   (`upload.getFile` on `InputGroupCallStream`), not SRTP. `aytgcalls` detects this and raises
   `TelegramCallError("call is in RTMP/stream mode")` instead of pretending to play. Publishing to
   such a call requires pushing to the RTMP URL from `phone.GetGroupCallStreamRtmpUrl`, which is
   outside the scope of a WebRTC client.
3. **We cannot verify audibility from this build environment.** There is no Telegram account,
   API_ID or STRING_SESSION available here, and outbound UDP to Telegram's SFU is not available.
   Every layer that can be tested offline *is* tested offline (SDP bridge, FFmpeg→PCM pipeline,
   Opus encoding, pacing, queue/player state machine, task cleanup, backoff). The two steps that
   fundamentally require a real account are: (a) that the SFU accepts our join payload, and (b) that
   the DTLS/SRTP handshake completes. `examples/scripts/live_check.py` performs exactly those two checks and
   reports RTP `packetsSent` from `RTCRtpSender.getStats()` so you get a hard yes/no on a VPS.
4. **Receiving/mixing other participants is not implemented.** This package publishes audio. The
   SFU will send us other participants' streams once we are joined; we do not decode them. Adding
   an `RTCRtpReceiver` per remote SSRC is possible with the same transport but is out of scope.
5. **Video / screen share is not implemented.** `phone.JoinGroupCallPresentation` and the
   `ssrc-groups` payload shape exist, but audio-only publishing is the stated goal and the join
   payload deliberately omits `ssrc-groups`.
6. **`GROUPCALL_SSRC_DUPLICATE_MUCH`** is raised by the server if you re-join with an SSRC that is
   already registered. We generate a fresh SSRC on every join *and* on every reconnect.

---

## 8. Schema verification performed

Introspected from the installed Kurigram 2.2.24 (`pyrogram.raw.functions.phone`):

```
JoinGroupCall(call, join_as, params, muted=None, video_stopped=None,
              invite_hash=None, public_key=None, block=None)
LeaveGroupCall(call, source)
GetGroupCall(call, limit)
CheckGroupCall(call, sources: List[int])
EditGroupCallParticipant(call, participant, muted=None, volume=None, raise_hand=None,
                         video_stopped=None, video_paused=None, presentation_paused=None)
GetGroupCallJoinAs(peer)
```

These are the exact signatures `aytgcalls/telegram/signaling.py` calls, so the MTProto layer cannot
silently drift from the schema.

---

## 9. Build order actually followed

1. This document.
2. Repo skeleton + `pyproject.toml`.
3. Kurigram signaling layer (discovery / join / check / leave) — importable and unit-testable with a
   fake client.
4. `sdp.py` JSON ⇄ SDP bridge + round-trip unit tests.
5. aiortc ORTC transport (`ice.py`, `webrtc.py`, `track.py`).
6. FFmpeg → PCM → ring buffer → gain → Opus → RTP paced sender.
7. Player + queue.
8. Reconnect + deterministic cleanup.
9. Tests.
10. README + packaging.

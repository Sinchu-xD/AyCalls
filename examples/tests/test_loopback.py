"""End-to-end media test against a *simulated* SFU on localhost.

This is as close as it is possible to get to "verify DTLS/ICE reaches Telegram's SFU"
without a Telegram account. A second aiortc peer is stood up locally, its parameters are
serialised into **exactly the JSON shape Telegram sends** (PROTOCOL.md §3), and
:class:`TelegramTransport` consumes that JSON with no special-casing.

What passing this proves:

* the join payload we generate is internally consistent with the ICE/DTLS stack we run;
* ICE connectivity checks succeed with us as the controlling agent;
* the DTLS role logic (`setup: passive` -> we are the client) completes a handshake;
* SRTP keys are derived and RTP flows;
* the packets carry **our pinned SSRC** and **the SFU's payload type**;
* the far end decodes them back into 48 kHz stereo audio frames.

What it does NOT prove: that Telegram's production SFU accepts our
``phone.joinGroupCall`` payload. Only ``scripts/live_check.py`` on a real account can.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiortc.rtcdtlstransport import RTCCertificate, RTCDtlsParameters, RTCDtlsTransport
from aiortc.rtcicetransport import RTCIceGatherer, RTCIceParameters, RTCIceTransport
from aiortc.rtcrtpparameters import (
    RTCRtcpParameters,
    RTCRtpCodecParameters,
    RTCRtpDecodingParameters,
    RTCRtpReceiveParameters,
)
from aiortc.rtcrtpreceiver import RemoteStreamTrack, RTCRtpReceiver

from aytgcalls.transport.ice import from_aiortc_candidate, random_pwd, random_ufrag
from aytgcalls.transport.sdp import parse_join_response
from aytgcalls.transport.track import PcmStreamTrack
from aytgcalls.transport.webrtc import TelegramTransport
from aytgcalls.types import BYTES_PER_FRAME


class FakeSfu:
    """A minimal stand-in for Telegram's SFU: full ICE + DTLS server + Opus receiver."""

    def __init__(self) -> None:
        self.certificate = RTCCertificate.generateCertificate()
        self.gatherer = RTCIceGatherer(
            iceServers=[], local_username=random_ufrag(), local_password=random_pwd()
        )
        self.ice = RTCIceTransport(self.gatherer)
        self.dtls = RTCDtlsTransport(self.ice, [self.certificate])
        self.receiver: RTCRtpReceiver | None = None

    async def gather(self) -> None:
        await self.gatherer.gather()

    def join_response_json(self) -> dict[str, Any]:
        """Serialise our parameters the way Telegram serialises the SFU's."""
        params = self.gatherer.getLocalParameters()
        fingerprint = next(
            fp for fp in self.dtls.getLocalParameters().fingerprints if fp.algorithm == "sha-256"
        )
        candidates = [
            from_aiortc_candidate(candidate).to_json()
            for candidate in self.gatherer.getLocalCandidates()
            if candidate.protocol == "udp" and ":" not in candidate.ip  # IPv4 host candidates
        ]
        return {
            "transport": {
                "ufrag": params.usernameFragment,
                "pwd": params.password,
                "fingerprints": [
                    {
                        "hash": fingerprint.algorithm,
                        "fingerprint": fingerprint.value,
                        "setup": "passive",  # Telegram is always the DTLS server
                    }
                ],
                "candidates": candidates,
                "rtcp-mux": True,
                "xmlns": "urn:xmpp:jingle:transports:ice-udp:1",
            },
            "audio": {
                "ssrc": 4242,
                "payload-types": [
                    {
                        "id": 111,
                        "name": "opus",
                        "clockrate": 48000,
                        "channels": 2,
                        "rtcp-fbs": [{"type": "transport-cc"}],
                        "parameters": {"minptime": "10", "useinbandfec": "1"},
                    }
                ],
                "rtp-hdrexts": [
                    {"id": 1, "uri": "urn:ietf:params:rtp-hdrext:ssrc-audio-level"}
                ],
            },
        }

    async def accept(self, remote_ufrag: str, remote_pwd: str, remote_fingerprint: Any) -> None:
        await self.ice.start(RTCIceParameters(usernameFragment=remote_ufrag, password=remote_pwd))
        self.dtls._set_role("server")  # noqa: SLF001
        await self.dtls.start(
            RTCDtlsParameters(fingerprints=[remote_fingerprint], role="server")
        )

    async def start_receiver(self, ssrc: int) -> RTCRtpReceiver:
        receiver = RTCRtpReceiver("audio", self.dtls)
        # RTCPeerConnection normally attaches the remote track; at the ORTC level we do it.
        receiver._track = RemoteStreamTrack(kind="audio")  # noqa: SLF001
        await receiver.receive(
            RTCRtpReceiveParameters(
                codecs=[
                    RTCRtpCodecParameters(
                        mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111
                    )
                ],
                encodings=[RTCRtpDecodingParameters(ssrc=ssrc, payloadType=111)],
                rtcp=RTCRtcpParameters(cname="sfu", ssrc=ssrc, mux=True),
            )
        )
        self.receiver = receiver
        return receiver

    async def close(self) -> None:
        if self.receiver is not None:
            await self.receiver.stop()
        await self.dtls.stop()
        await self.ice.stop()


def _tone_frame(step: int) -> bytes:
    """A 20 ms frame of obviously-not-silence PCM."""
    samples = bytearray()
    for index in range(BYTES_PER_FRAME // 4):
        value = int(8000 * (((index + step) % 96) / 96 - 0.5))
        samples += value.to_bytes(2, "little", signed=True) * 2
    return bytes(samples)


async def test_ice_dtls_srtp_rtp_end_to_end() -> None:
    counter = {"n": 0}

    async def provider() -> bytes:
        counter["n"] += 1
        return _tone_frame(counter["n"])

    track = PcmStreamTrack(provider)
    transport = TelegramTransport(track, connect_timeout=30.0)
    sfu = FakeSfu()
    try:
        await sfu.gather()
        payload = await transport.prepare()

        # A real full-ICE peer needs our candidates. Telegram's SFU is ICE-lite and
        # latches onto the source address of our checks instead, which is why the join
        # payload does not carry them.
        for candidate in transport.local_candidates:
            if candidate.protocol == "udp" and ":" not in candidate.ip:
                await sfu.ice.addRemoteCandidate(candidate)
        await sfu.ice.addRemoteCandidate(None)

        response = parse_join_response(sfu.join_response_json())
        assert response.transport.setup == "passive"
        assert response.opus.id == 111

        from aiortc.rtcdtlstransport import RTCDtlsFingerprint

        our_fingerprint = RTCDtlsFingerprint(
            algorithm=payload.fingerprints[0].hash, value=payload.fingerprints[0].fingerprint
        )
        results = await asyncio.gather(
            transport.connect(response),
            sfu.accept(payload.ufrag, payload.pwd, our_fingerprint),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

        assert transport.is_connected
        assert transport.ice_state in {"completed", "connected"}
        assert transport.dtls_state == "connected"

        receiver = await sfu.start_receiver(transport.ssrc)

        # The far end must decode real audio frames out of our SRTP stream.
        frame = await asyncio.wait_for(receiver.track.recv(), timeout=20)
        assert frame.sample_rate == 48_000
        assert frame.samples > 0

        stats = await transport.refresh_stats()
        assert stats.packets_sent > 0, "no RTP packets were sent"
        assert stats.bytes_sent > 0

        sources = receiver.getSynchronizationSources()
        assert any(source.source == transport.ssrc for source in sources), (
            f"expected our pinned SSRC {transport.ssrc} in {sources}"
        )
    finally:
        await transport.close()
        await sfu.close()
        track.stop()


async def test_transport_closes_cleanly_after_connect() -> None:
    """No leaked tasks once a connected call is torn down."""
    baseline = len(asyncio.all_tasks())
    track = PcmStreamTrack(lambda: asyncio.sleep(0, _tone_frame(0)))
    transport = TelegramTransport(track, connect_timeout=30.0)
    sfu = FakeSfu()
    try:
        await sfu.gather()
        payload = await transport.prepare()
        for candidate in transport.local_candidates:
            if candidate.protocol == "udp" and ":" not in candidate.ip:
                await sfu.ice.addRemoteCandidate(candidate)
        await sfu.ice.addRemoteCandidate(None)

        from aiortc.rtcdtlstransport import RTCDtlsFingerprint

        fingerprint = RTCDtlsFingerprint(
            algorithm=payload.fingerprints[0].hash, value=payload.fingerprints[0].fingerprint
        )
        await asyncio.gather(
            transport.connect(parse_join_response(sfu.join_response_json())),
            sfu.accept(payload.ufrag, payload.pwd, fingerprint),
        )
        await asyncio.sleep(0.3)
        assert transport.is_connected
    finally:
        await transport.close()
        await sfu.close()
        track.stop()
    await asyncio.sleep(0.3)
    assert not transport.is_connected
    # Everything the transport (and the simulated peer) started must be gone.
    assert len(asyncio.all_tasks()) <= baseline, sorted(
        task.get_coro().__qualname__ for task in asyncio.all_tasks()
    )

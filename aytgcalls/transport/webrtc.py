"""WebRTC transport against Telegram's group-call SFU.

Why this does not use :class:`RTCPeerConnection`
------------------------------------------------
Telegram signals with JSON over MTProto, not SDP offer/answer, and two values must be
pinned *before* the answer exists:

* the **SSRC**, because it is committed to in ``phone.joinGroupCall``;
* the **Opus payload type**, because the SFU picks it (normally 111) and, as the
  offerer, we would otherwise have advertised aiortc's own dynamic PT.

``RTCPeerConnection`` regenerates SDP internally, so pinning either value would mean
munging strings it is free to ignore. aiortc also exposes its ORTC-level objects as
public API — :class:`RTCIceGatherer`, :class:`RTCIceTransport`, :class:`RTCDtlsTransport`,
:class:`RTCRtpSender` — and those let us set ICE credentials, DTLS role, payload type and
SSRC exactly. :mod:`aytgcalls.transport.sdp` remains a real, tested bridge for anything
that wants the SDP view (and is what ``PROTOCOL.md`` documents).
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiortc.rtcdtlstransport import RTCCertificate, RTCDtlsParameters, RTCDtlsTransport
from aiortc.rtcdtlstransport import RTCDtlsFingerprint as AiortcFingerprint
from aiortc.rtcrtpparameters import (
    RTCRtcpParameters,
    RTCRtpCodecParameters,
    RTCRtpEncodingParameters,
    RTCRtpHeaderExtensionParameters,
    RTCRtpSendParameters,
)
from aiortc.rtcrtpsender import RTCRtpSender

from ..exceptions import DTLSHandshakeFailed, ICEFailed, TransportError
from ..logger import get_logger
from ..types import CallStats
from .ice import (
    build_ice_transport,
    random_pwd,
    random_ssrc,
    random_ufrag,
    remote_ice_parameters,
    to_aiortc_candidate,
)
from .sdp import Fingerprint, JoinPayload, JoinResponse
from .track import PcmStreamTrack

logger = get_logger("transport.webrtc")

__all__ = ["TelegramTransport", "SUPPORTED_HEADER_EXTENSIONS"]

#: Header extension URIs aiortc can actually serialise. Anything else the SFU
#: advertises is dropped rather than echoed back (echoing an unsupported id makes
#: the receiver mis-parse the extension block).
SUPPORTED_HEADER_EXTENSIONS = frozenset(
    {
        "urn:ietf:params:rtp-hdrext:sdes:mid",
        "urn:ietf:params:rtp-hdrext:ssrc-audio-level",
        "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
        "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
    }
)


def _dtls_role_for(remote_setup: str) -> str:
    """Pick our DTLS role from the role the SFU claims.

    Telegram answers ``passive`` (it is the DTLS server), so we are the ``client`` and
    send the ClientHello. We tolerate the inverse and ``actpass`` rather than assuming.
    """
    setup = (remote_setup or "passive").lower()
    if setup == "active":
        return "server"
    return "client"  # remote passive / actpass / unknown -> we are the client


class TelegramTransport:
    """ICE + DTLS-SRTP + RTP(Opus) publisher for one group call.

    Lifecycle::

        transport = TelegramTransport(track)
        payload = await transport.prepare()          # -> phone.joinGroupCall params
        await transport.connect(join_response)       # ICE + DTLS + RTP start
        ...
        await transport.close()
    """

    def __init__(
        self,
        track: PcmStreamTrack,
        *,
        ice_servers: tuple[str, ...] = (),
        connect_timeout: float = 20.0,
        opus_bitrate: int = 96_000,
        stats: CallStats | None = None,
        mid: str = "0",
        cname: str = "aytgcalls",
    ) -> None:
        self._track = track
        self._ice_servers = ice_servers
        self._connect_timeout = connect_timeout
        self._opus_bitrate = opus_bitrate
        self._stats = stats if stats is not None else track.stats
        self._mid = mid
        self._cname = cname

        self.ssrc: int = random_ssrc()
        self._ufrag = random_ufrag()
        self._pwd = random_pwd()
        self._certificate: RTCCertificate | None = None
        self._ice: Any = None
        self._dtls: RTCDtlsTransport | None = None
        self._sender: RTCRtpSender | None = None
        self._closed = False
        self._connected = asyncio.Event()

    # -- state ------------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and not self._closed

    @property
    def ice_state(self) -> str:
        return getattr(self._ice, "state", "closed")

    @property
    def local_candidates(self) -> list[Any]:
        """Our gathered ICE candidates.

        Telegram's SFU never needs these (it latches onto the source address of our
        connectivity checks), but they are useful for diagnostics and for testing against
        a full-ICE peer.
        """
        if self._ice is None:
            return []
        return list(self._ice.iceGatherer.getLocalCandidates())

    @property
    def dtls_state(self) -> str:
        return getattr(self._dtls, "state", "closed")

    # -- phase 1: local parameters ------------------------------------------------------

    async def prepare(self) -> JoinPayload:
        """Gather local ICE candidates and build the ``phone.joinGroupCall`` payload."""
        if self._ice is not None:
            raise TransportError("TelegramTransport.prepare() called twice")

        self._certificate = RTCCertificate.generateCertificate()
        self._ice = build_ice_transport(
            ufrag=self._ufrag, pwd=self._pwd, ice_servers=self._ice_servers
        )
        self._dtls = RTCDtlsTransport(self._ice, [self._certificate])

        await self._ice.iceGatherer.gather()
        local = self._ice.iceGatherer.getLocalParameters()
        candidates = self._ice.iceGatherer.getLocalCandidates()
        logger.debug("Gathered %d local ICE candidates", len(candidates))
        if not candidates:
            raise ICEFailed(
                "No local ICE candidates could be gathered. The host has no usable network "
                "interface, or UDP is fully blocked."
            )

        fingerprint = self._local_fingerprint()
        payload = JoinPayload(
            ssrc=self.ssrc,
            ufrag=local.usernameFragment or self._ufrag,
            pwd=local.password or self._pwd,
            fingerprints=(fingerprint,),
        )
        logger.info("Local transport ready (ssrc=%d, ufrag=%s)", self.ssrc, payload.ufrag)
        return payload

    def _local_fingerprint(self) -> Fingerprint:
        assert self._dtls is not None
        prints = self._dtls.getLocalParameters().fingerprints
        chosen = next((fp for fp in prints if fp.algorithm.lower() == "sha-256"), None)
        if chosen is None:
            if not prints:
                raise TransportError("DTLS certificate produced no fingerprints")
            chosen = prints[0]
        return Fingerprint(hash=chosen.algorithm, fingerprint=chosen.value, setup="active")

    # -- phase 2: connect ---------------------------------------------------------------

    async def connect(self, response: JoinResponse) -> None:
        """Apply the SFU's parameters, run ICE + DTLS, and start sending RTP."""
        if self._ice is None or self._dtls is None:
            raise TransportError("connect() called before prepare()")
        if response.is_stream:
            raise TransportError(
                "This group call is an RTMP/stream broadcast, not a WebRTC conference; "
                "audio cannot be published over SRTP. See PROTOCOL.md §7."
            )

        await self.add_remote_candidates(response, end_of_candidates=True)

        remote = response.transport
        try:
            await asyncio.wait_for(
                self._ice.start(remote_ice_parameters(remote.ufrag, remote.pwd)),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ICEFailed(
                f"ICE did not connect within {self._connect_timeout:g}s. Outbound UDP to "
                "Telegram's media servers is probably blocked."
            ) from exc
        if self._ice.state not in {"completed", "connected"}:
            raise ICEFailed(f"ICE ended in state {self._ice.state!r}")
        self._stats.ice_state = self._ice.state
        logger.info("ICE connected (%s)", self._ice.state)

        role = _dtls_role_for(remote.setup)
        self._dtls._set_role(role)
        logger.debug("DTLS role=%s (remote setup=%s)", role, remote.setup)
        fingerprints = [
            AiortcFingerprint(algorithm=fp.hash, value=fp.fingerprint)
            for fp in remote.fingerprints
        ]
        try:
            await asyncio.wait_for(
                self._dtls.start(RTCDtlsParameters(fingerprints=fingerprints, role=role)),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DTLSHandshakeFailed(
                f"DTLS handshake did not complete within {self._connect_timeout:g}s."
            ) from exc
        except Exception as exc:  # aiortc raises plain exceptions on handshake failure
            raise DTLSHandshakeFailed(f"DTLS handshake failed: {exc}") from exc
        if self._dtls.state != "connected":
            raise DTLSHandshakeFailed(f"DTLS ended in state {self._dtls.state!r}")
        self._stats.dtls_state = self._dtls.state
        logger.info("DTLS-SRTP established")

        await self._start_sender(response)
        self._connected.set()

    async def add_remote_candidates(
        self, response: JoinResponse, *, end_of_candidates: bool = False
    ) -> None:
        """Feed the SFU's ICE candidates to the transport (also used for late updates).

        ``RTCIceTransport.addRemoteCandidate`` is a coroutine in aiortc; it must be
        awaited or the candidate is silently dropped and ICE can never pair.
        """
        if self._ice is None:
            raise TransportError("add_remote_candidates() called before prepare()")
        for candidate in response.transport.candidates:
            await self._ice.addRemoteCandidate(to_aiortc_candidate(candidate, sdp_mid=self._mid))
            logger.debug(
                "Remote candidate %s:%s (%s)", candidate.ip, candidate.port, candidate.type
            )
        if end_of_candidates:
            await self._ice.addRemoteCandidate(None)

    async def _start_sender(self, response: JoinResponse) -> None:
        assert self._dtls is not None
        opus = response.opus
        codec = RTCRtpCodecParameters(
            mimeType=f"audio/{opus.name}",
            clockRate=opus.clockrate,
            channels=opus.channels or 2,
            payloadType=opus.id,
            parameters=dict(opus.parameters),
        )
        extensions = [
            RTCRtpHeaderExtensionParameters(id=ext.id, uri=ext.uri)
            for ext in response.header_extensions
            if ext.uri in SUPPORTED_HEADER_EXTENSIONS
        ]
        dropped = [
            ext.uri
            for ext in response.header_extensions
            if ext.uri not in SUPPORTED_HEADER_EXTENSIONS
        ]
        if dropped:
            logger.debug("Ignoring unsupported RTP header extensions: %s", dropped)

        sender = RTCRtpSender(self._track, self._dtls)
        # Pin the SSRC we already committed to in phone.joinGroupCall.
        sender._ssrc = self.ssrc
        parameters = RTCRtpSendParameters(
            codecs=[codec],
            headerExtensions=extensions,
            muxId=self._mid,
            rtcp=RTCRtcpParameters(cname=self._cname, ssrc=self.ssrc, mux=True),
            encodings=[RTCRtpEncodingParameters(ssrc=self.ssrc, payloadType=opus.id)],
        )
        await sender.send(parameters)
        self._sender = sender
        self._tune_opus_bitrate()
        logger.info(
            "RTP sender started (pt=%d, ssrc=%d, %d header extensions)",
            opus.id,
            self.ssrc,
            len(extensions),
        )

    def _tune_opus_bitrate(self) -> None:
        """Best-effort: apply the configured bitrate to aiortc's Opus encoder.

        aiortc hardcodes 96 kbps and exposes no setter, so this reaches for the encoder
        once it exists. Failure is logged and ignored — 96 kbps is inside Telegram's
        acceptable range anyway.
        """
        if self._opus_bitrate == 96_000 or self._sender is None:
            return

        async def _apply() -> None:
            for _ in range(50):
                encoder = getattr(self._sender, "_RTCRtpSender__encoder", None)
                codec_ctx = getattr(encoder, "codec", None)
                if codec_ctx is not None:
                    try:
                        codec_ctx.bit_rate = self._opus_bitrate
                        logger.debug("Opus bitrate set to %d bps", self._opus_bitrate)
                    except Exception as exc:  # pragma: no cover - PyAV version dependent
                        logger.debug("Could not set Opus bitrate: %s", exc)
                    return
                await asyncio.sleep(0.05)
            logger.debug("Opus encoder never appeared; keeping aiortc default bitrate")

        asyncio.ensure_future(_apply())

    # -- stats / teardown ---------------------------------------------------------------

    async def refresh_stats(self) -> CallStats:
        """Pull ``packetsSent`` / ``bytesSent`` from the RTP sender."""
        if self._sender is not None:
            try:
                report = await self._sender.getStats()
                for entry in report.values():
                    if getattr(entry, "type", "") == "outbound-rtp":
                        self._stats.packets_sent = int(getattr(entry, "packetsSent", 0))
                        self._stats.bytes_sent = int(getattr(entry, "bytesSent", 0))
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("getStats() failed: %s", exc)
        self._stats.ice_state = self.ice_state
        self._stats.dtls_state = self.dtls_state
        return self._stats

    async def close(self) -> None:
        """Tear everything down deterministically. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._connected.clear()
        if self._sender is not None:
            try:
                await self._sender.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Sender stop failed: %s", exc)
            self._sender = None
        if self._dtls is not None:
            try:
                await self._dtls.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("DTLS stop failed: %s", exc)
            self._dtls = None
        if self._ice is not None:
            try:
                await self._ice.stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("ICE stop failed: %s", exc)
            self._ice = None
        logger.info("Transport closed")

    async def __aenter__(self) -> TelegramTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

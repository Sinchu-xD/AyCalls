"""ICE helpers for Telegram's SFU.

Two things differ from a browser-to-browser WebRTC session and are handled here:

1. We must be the **controlling** agent. aiortc's :class:`RTCIceGatherer` hardcodes
   ``ice_controlling=False`` (it expects to be driven by ``RTCPeerConnection``), so we
   flip the flag on the underlying aioice connection.
2. Telegram's SFU behaves like an **ICE-lite** peer: host candidates on public IPs, no
   outbound connectivity checks. We therefore mark the remote parameters as lite and do
   not need STUN by default.
"""

from __future__ import annotations

import random
import string
from typing import TYPE_CHECKING, Any

from aiortc.rtcconfiguration import RTCIceServer
from aiortc.rtcicetransport import (
    RTCIceCandidate,
    RTCIceGatherer,
    RTCIceParameters,
    RTCIceTransport,
)

from ..logger import get_logger
from .sdp import IceCandidate

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger("transport.ice")

__all__ = [
    "random_ufrag",
    "random_pwd",
    "random_ssrc",
    "build_ice_transport",
    "to_aiortc_candidate",
    "from_aiortc_candidate",
]

_ALPHABET = string.ascii_letters + string.digits + "+/"


def random_ufrag(length: int = 8) -> str:
    """RFC 5245 requires 4..256 characters of ice-char."""
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(length))


def random_pwd(length: int = 24) -> str:
    """RFC 5245 requires 22..256 characters."""
    return "".join(random.choice(_ALPHABET) for _ in range(length))


def random_ssrc() -> int:
    """A random non-zero **positive int32** SSRC.

    Telegram's ``source`` fields are signed 32-bit ints, so staying below 2**31 avoids
    sign-conversion mismatches between our RTP SSRC and ``phone.leaveGroupCall``.
    """
    return random.randint(1, 2**31 - 1)


def build_ice_transport(
    *,
    ufrag: str,
    pwd: str,
    ice_servers: Iterable[str] = (),
) -> RTCIceTransport:
    """Create a controlling :class:`RTCIceTransport` with fixed local credentials."""
    servers = [RTCIceServer(urls=url) for url in ice_servers]
    gatherer = RTCIceGatherer(
        iceServers=servers or [],
        local_username=ufrag,
        local_password=pwd,
    )
    transport = RTCIceTransport(gatherer)
    # Telegram expects the joining client to drive connectivity checks.
    transport._connection.ice_controlling = True
    logger.debug("ICE transport built (controlling=True, stun_servers=%d)", len(servers))
    return transport


def remote_ice_parameters(ufrag: str, pwd: str, *, ice_lite: bool = True) -> RTCIceParameters:
    """Build the remote ICE parameters for Telegram's (ICE-lite style) SFU."""
    return RTCIceParameters(usernameFragment=ufrag, password=pwd, iceLite=ice_lite)


def to_aiortc_candidate(candidate: IceCandidate, *, sdp_mid: str = "0") -> RTCIceCandidate:
    """Convert our JSON candidate model into an aiortc candidate."""
    return RTCIceCandidate(
        component=candidate.component,
        foundation=candidate.foundation,
        ip=candidate.ip,
        port=candidate.port,
        priority=candidate.priority,
        protocol=candidate.protocol,
        type=candidate.type,
        relatedAddress=candidate.rel_addr,
        relatedPort=candidate.rel_port,
        tcpType=candidate.tcptype,
        sdpMid=sdp_mid,
        sdpMLineIndex=0,
    )


def from_aiortc_candidate(candidate: Any) -> IceCandidate:
    """Convert an aiortc candidate back into our JSON model."""
    return IceCandidate(
        foundation=candidate.foundation,
        component=candidate.component,
        protocol=candidate.protocol,
        priority=candidate.priority,
        ip=candidate.ip,
        port=candidate.port,
        type=candidate.type,
        rel_addr=candidate.relatedAddress,
        rel_port=candidate.relatedPort,
        tcptype=candidate.tcpType,
    )

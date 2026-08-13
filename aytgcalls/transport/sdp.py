"""Telegram group-call JSON  <->  SDP (unified plan) bridge.

Telegram signals WebRTC transport parameters as JSON blobs over MTProto instead of
SDP offer/answer. This module converts in both directions:

* :func:`join_response_to_sdp`  — server JSON  -> SDP *answer* (``setRemoteDescription``)
* :func:`sdp_offer_to_join_params` — local SDP *offer* -> ``phone.joinGroupCall`` params
* :func:`sdp_answer_to_join_response` — SDP answer -> server JSON (the exact inverse,
  which makes the round trip testable)

The parser is deliberately tolerant: field values arrive as strings or ints depending
on the server build, and ``payload-types`` / ``rtp-hdrexts`` are sometimes nested under
``audio`` and sometimes at the top level.

See ``PROTOCOL.md`` §3 for the payload shapes this implements.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..exceptions import TransportError
from ..types import CHANNELS, SAMPLE_RATE

__all__ = [
    "Fingerprint",
    "IceCandidate",
    "RtcpFeedback",
    "PayloadType",
    "HeaderExtension",
    "TransportDescription",
    "JoinResponse",
    "JoinPayload",
    "parse_join_response",
    "join_response_to_sdp",
    "sdp_answer_to_join_response",
    "sdp_offer_to_join_params",
    "join_params_to_sdp_offer",
    "OPUS_DEFAULT",
]


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return int(str(value).strip())


def _as_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


@dataclass(frozen=True)
class Fingerprint:
    """A DTLS certificate fingerprint plus the DTLS setup role that owns it."""

    hash: str
    fingerprint: str
    setup: str = "active"

    def to_json(self) -> dict[str, str]:
        return {"hash": self.hash, "fingerprint": self.fingerprint, "setup": self.setup}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Fingerprint:
        return cls(
            hash=_as_str(data.get("hash", "sha-256")),
            fingerprint=_as_str(data.get("fingerprint")),
            setup=_as_str(data.get("setup", "active")),
        )


@dataclass(frozen=True)
class IceCandidate:
    """A single ICE candidate from Telegram's SFU."""

    foundation: str
    component: int
    protocol: str
    priority: int
    ip: str
    port: int
    type: str
    generation: int = 0
    network: int = 0
    rel_addr: str | None = None
    rel_port: int | None = None
    tcptype: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IceCandidate:
        return cls(
            foundation=_as_str(data.get("foundation", "1")),
            component=_as_int(data.get("component"), 1),
            protocol=_as_str(data.get("protocol", "udp")).lower(),
            priority=_as_int(data.get("priority"), 1),
            ip=_as_str(data.get("ip")),
            port=_as_int(data.get("port")),
            type=_as_str(data.get("type", "host")),
            generation=_as_int(data.get("generation")),
            network=_as_int(data.get("network")),
            rel_addr=data.get("rel-addr") or data.get("rel_addr") or None,
            rel_port=(
                _as_int(data.get("rel-port") or data.get("rel_port"))
                if (data.get("rel-port") or data.get("rel_port"))
                else None
            ),
            tcptype=data.get("tcptype") or None,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "generation": str(self.generation),
            "component": str(self.component),
            "protocol": self.protocol,
            "port": str(self.port),
            "ip": self.ip,
            "foundation": self.foundation,
            "id": f"{self.foundation}{self.component}{self.port}",
            "priority": str(self.priority),
            "type": self.type,
            "network": str(self.network),
        }
        if self.rel_addr:
            out["rel-addr"] = self.rel_addr
        if self.rel_port is not None:
            out["rel-port"] = str(self.rel_port)
        if self.tcptype:
            out["tcptype"] = self.tcptype
        return out

    def to_sdp(self) -> str:
        """Render as the value of an ``a=candidate:`` attribute (without the prefix)."""
        parts = [
            self.foundation,
            str(self.component),
            self.protocol,
            str(self.priority),
            self.ip,
            str(self.port),
            "typ",
            self.type,
        ]
        if self.rel_addr and self.rel_port is not None:
            parts += ["raddr", self.rel_addr, "rport", str(self.rel_port)]
        if self.tcptype:
            parts += ["tcptype", self.tcptype]
        parts += ["generation", str(self.generation)]
        return " ".join(parts)

    @classmethod
    def from_sdp(cls, value: str) -> IceCandidate:
        bits = value.split()
        if len(bits) < 8 or bits[6] != "typ":
            raise TransportError(f"Malformed ICE candidate: {value!r}")
        extra = bits[8:]
        pairs = dict(zip(extra[::2], extra[1::2], strict=False))
        return cls(
            foundation=bits[0],
            component=int(bits[1]),
            protocol=bits[2].lower(),
            priority=int(bits[3]),
            ip=bits[4],
            port=int(bits[5]),
            type=bits[7],
            generation=int(pairs.get("generation", 0)),
            rel_addr=pairs.get("raddr"),
            rel_port=int(pairs["rport"]) if "rport" in pairs else None,
            tcptype=pairs.get("tcptype"),
        )


@dataclass(frozen=True)
class RtcpFeedback:
    type: str
    subtype: str | None = None

    def to_json(self) -> dict[str, str]:
        out = {"type": self.type}
        if self.subtype:
            out["subtype"] = self.subtype
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RtcpFeedback:
        return cls(type=_as_str(data.get("type")), subtype=data.get("subtype") or None)

    def to_sdp(self) -> str:
        return f"{self.type} {self.subtype}" if self.subtype else self.type


@dataclass(frozen=True)
class PayloadType:
    """An RTP payload type advertised by the SFU."""

    id: int
    name: str
    clockrate: int = SAMPLE_RATE
    channels: int = CHANNELS
    parameters: dict[str, str] = field(default_factory=dict)
    rtcp_fbs: tuple[RtcpFeedback, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PayloadType:
        return cls(
            id=_as_int(data.get("id")),
            name=_as_str(data.get("name")),
            clockrate=_as_int(data.get("clockrate"), SAMPLE_RATE),
            channels=_as_int(data.get("channels"), 1),
            parameters={str(k): str(v) for k, v in (data.get("parameters") or {}).items()},
            rtcp_fbs=tuple(RtcpFeedback.from_json(fb) for fb in (data.get("rtcp-fbs") or [])),
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "clockrate": self.clockrate,
            "channels": self.channels,
        }
        if self.rtcp_fbs:
            out["rtcp-fbs"] = [fb.to_json() for fb in self.rtcp_fbs]
        if self.parameters:
            out["parameters"] = dict(self.parameters)
        return out

    @property
    def is_opus(self) -> bool:
        return self.name.lower() == "opus"


OPUS_DEFAULT = PayloadType(
    id=111,
    name="opus",
    clockrate=SAMPLE_RATE,
    channels=CHANNELS,
    parameters={"minptime": "10", "useinbandfec": "1"},
    rtcp_fbs=(RtcpFeedback("transport-cc"),),
)


@dataclass(frozen=True)
class HeaderExtension:
    id: int
    uri: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HeaderExtension:
        return cls(id=_as_int(data.get("id")), uri=_as_str(data.get("uri")))

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "uri": self.uri}


@dataclass
class TransportDescription:
    """ICE + DTLS parameters for one side of the connection."""

    ufrag: str
    pwd: str
    fingerprints: tuple[Fingerprint, ...] = ()
    candidates: tuple[IceCandidate, ...] = ()
    rtcp_mux: bool = True

    @property
    def setup(self) -> str:
        return self.fingerprints[0].setup if self.fingerprints else "actpass"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TransportDescription:
        return cls(
            ufrag=_as_str(data.get("ufrag")),
            pwd=_as_str(data.get("pwd")),
            fingerprints=tuple(
                Fingerprint.from_json(fp) for fp in (data.get("fingerprints") or [])
            ),
            candidates=tuple(IceCandidate.from_json(c) for c in (data.get("candidates") or [])),
            rtcp_mux=bool(data.get("rtcp-mux", True)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "ufrag": self.ufrag,
            "pwd": self.pwd,
            "fingerprints": [fp.to_json() for fp in self.fingerprints],
            "candidates": [c.to_json() for c in self.candidates],
            "rtcp-mux": self.rtcp_mux,
            "xmlns": "urn:xmpp:jingle:transports:ice-udp:1",
        }


@dataclass
class JoinResponse:
    """Parsed ``UpdateGroupCallConnection.params`` payload."""

    transport: TransportDescription
    payload_types: tuple[PayloadType, ...] = ()
    header_extensions: tuple[HeaderExtension, ...] = ()
    #: SSRC the SFU assigns to itself (informational; may be absent).
    server_ssrc: int | None = None
    is_stream: bool = False
    is_rtmp: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def opus(self) -> PayloadType:
        """The Opus payload type, falling back to Telegram's canonical PT 111."""
        for pt in self.payload_types:
            if pt.is_opus:
                return pt
        return OPUS_DEFAULT

    def to_json(self) -> dict[str, Any]:
        if self.is_stream:
            out: dict[str, Any] = {"stream": True}
            if self.is_rtmp:
                out["rtmp"] = True
            return out
        audio: dict[str, Any] = {}
        if self.server_ssrc is not None:
            audio["ssrc"] = self.server_ssrc
        if self.payload_types:
            audio["payload-types"] = [pt.to_json() for pt in self.payload_types]
        if self.header_extensions:
            audio["rtp-hdrexts"] = [ext.to_json() for ext in self.header_extensions]
        result: dict[str, Any] = {"transport": self.transport.to_json()}
        if audio:
            result["audio"] = audio
        return result


def parse_join_response(payload: str | bytes | dict[str, Any]) -> JoinResponse:
    """Parse the JSON returned by ``phone.joinGroupCall``.

    Accepts the raw ``DataJSON.data`` string or an already-decoded dict.
    Raises :class:`TransportError` if the payload is unusable.
    """
    if isinstance(payload, (str, bytes)):
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise TransportError(f"Join response is not valid JSON: {exc}") from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise TransportError(f"Join response must be a JSON object, got {type(data).__name__}")

    if data.get("stream"):
        return JoinResponse(
            transport=TransportDescription(ufrag="", pwd=""),
            is_stream=True,
            is_rtmp=bool(data.get("rtmp")),
            raw=data,
        )

    transport_json = data.get("transport")
    if not isinstance(transport_json, dict):
        # Some builds flatten the transport fields into the root object.
        if "ufrag" in data and "pwd" in data:
            transport_json = data
        else:
            raise TransportError(
                "Join response has no 'transport' object and no top-level ufrag/pwd; "
                f"keys were {sorted(data)}"
            )
    transport = TransportDescription.from_json(transport_json)
    if not transport.ufrag or not transport.pwd:
        raise TransportError("Join response transport is missing ICE ufrag/pwd")
    if not transport.fingerprints:
        raise TransportError("Join response transport is missing DTLS fingerprints")

    raw_audio = data.get("audio")
    audio: dict[str, Any] = raw_audio if isinstance(raw_audio, dict) else {}
    pt_json = audio.get("payload-types") or data.get("payload-types") or []
    ext_json = audio.get("rtp-hdrexts") or data.get("rtp-hdrexts") or []
    ssrc = audio.get("ssrc", data.get("ssrc"))

    return JoinResponse(
        transport=transport,
        payload_types=tuple(PayloadType.from_json(pt) for pt in pt_json),
        header_extensions=tuple(HeaderExtension.from_json(ext) for ext in ext_json),
        server_ssrc=_as_int(ssrc) if ssrc not in (None, "") else None,
        raw=data,
    )


# --------------------------------------------------------------------------------------
# JSON -> SDP
# --------------------------------------------------------------------------------------


def join_response_to_sdp(
    response: JoinResponse,
    *,
    session_id: int = 1,
    mid: str = "0",
    direction: str = "recvonly",
    ice_lite: bool = True,
) -> str:
    """Render a :class:`JoinResponse` as a unified-plan SDP **answer**.

    ``direction`` is written from the SFU's point of view: it *receives* what we send,
    so the default is ``recvonly``.
    """
    if response.is_stream:
        raise TransportError(
            "This group call is in RTMP/stream mode; there is no WebRTC transport to describe. "
            "See PROTOCOL.md §7."
        )
    payload_types = response.payload_types or (OPUS_DEFAULT,)
    pt_ids = " ".join(str(pt.id) for pt in payload_types)
    lines = [
        "v=0",
        f"o=- {session_id} 2 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        f"a=group:BUNDLE {mid}",
        "a=msid-semantic: WMS *",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {pt_ids}",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{response.transport.ufrag}",
        f"a=ice-pwd:{response.transport.pwd}",
    ]
    if ice_lite:
        lines.append("a=ice-lite")
    for fp in response.transport.fingerprints:
        lines.append(f"a=fingerprint:{fp.hash} {fp.fingerprint}")
    lines.append(f"a=setup:{response.transport.setup}")
    lines.append(f"a=mid:{mid}")
    lines.append(f"a={direction}")
    if response.transport.rtcp_mux:
        lines.append("a=rtcp-mux")
    for pt in payload_types:
        suffix = f"/{pt.channels}" if pt.channels and pt.channels > 1 else ""
        lines.append(f"a=rtpmap:{pt.id} {pt.name}/{pt.clockrate}{suffix}")
        for fb in pt.rtcp_fbs:
            lines.append(f"a=rtcp-fb:{pt.id} {fb.to_sdp()}")
        if pt.parameters:
            params = ";".join(f"{k}={v}" for k, v in pt.parameters.items())
            lines.append(f"a=fmtp:{pt.id} {params}")
    for ext in response.header_extensions:
        lines.append(f"a=extmap:{ext.id} {ext.uri}")
    for cand in response.transport.candidates:
        lines.append(f"a=candidate:{cand.to_sdp()}")
    if response.transport.candidates:
        lines.append("a=end-of-candidates")
    if response.server_ssrc is not None:
        lines.append(f"a=ssrc:{response.server_ssrc} cname:tgcall")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------------------
# SDP -> JSON
# --------------------------------------------------------------------------------------

_ATTR_RE = re.compile(r"^a=([^:]+)(?::(.*))?$")


def _parse_attributes(sdp: str) -> list[tuple[str, str]]:
    attrs: list[tuple[str, str]] = []
    for raw_line in sdp.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line.startswith("a="):
            continue
        match = _ATTR_RE.match(line)
        if match:
            attrs.append((match.group(1), match.group(2) or ""))
    return attrs


def _first(attrs: list[tuple[str, str]], name: str) -> str | None:
    for key, value in attrs:
        if key == name:
            return value
    return None


def sdp_answer_to_join_response(sdp: str) -> JoinResponse:
    """Inverse of :func:`join_response_to_sdp` — parse an SDP answer back into JSON model.

    Used for round-trip testing and for interoperating with tooling that speaks SDP.
    """
    attrs = _parse_attributes(sdp)
    ufrag = _first(attrs, "ice-ufrag") or ""
    pwd = _first(attrs, "ice-pwd") or ""
    setup = _first(attrs, "setup") or "passive"
    fingerprints = []
    for key, value in attrs:
        if key == "fingerprint":
            algorithm, _, digest = value.partition(" ")
            fingerprints.append(Fingerprint(hash=algorithm, fingerprint=digest, setup=setup))
    candidates = tuple(
        IceCandidate.from_sdp(value) for key, value in attrs if key == "candidate"
    )
    rtcp_mux = any(key == "rtcp-mux" for key, _ in attrs)

    rtpmaps: dict[int, tuple[str, int, int]] = {}
    fmtps: dict[int, dict[str, str]] = {}
    fbs: dict[int, list[RtcpFeedback]] = {}
    order: list[int] = []
    for key, value in attrs:
        if key == "rtpmap":
            pt_str, _, rest = value.partition(" ")
            pt_id = int(pt_str)
            name, _, tail = rest.partition("/")
            clock_str, _, chan_str = tail.partition("/")
            rtpmaps[pt_id] = (name, int(clock_str or SAMPLE_RATE), int(chan_str or 1))
            if pt_id not in order:
                order.append(pt_id)
        elif key == "fmtp":
            pt_str, _, rest = value.partition(" ")
            params: dict[str, str] = {}
            for item in rest.split(";"):
                if not item.strip():
                    continue
                pk, _, pv = item.partition("=")
                params[pk.strip()] = pv.strip()
            fmtps[int(pt_str)] = params
        elif key == "rtcp-fb":
            pt_str, _, rest = value.partition(" ")
            fb_type, _, fb_sub = rest.partition(" ")
            fbs.setdefault(int(pt_str), []).append(
                RtcpFeedback(fb_type.strip(), fb_sub.strip() or None)
            )

    payload_types = tuple(
        PayloadType(
            id=pt_id,
            name=rtpmaps[pt_id][0],
            clockrate=rtpmaps[pt_id][1],
            channels=rtpmaps[pt_id][2],
            parameters=fmtps.get(pt_id, {}),
            rtcp_fbs=tuple(fbs.get(pt_id, ())),
        )
        for pt_id in order
    )
    header_extensions = tuple(
        HeaderExtension(id=int(value.split(" ", 1)[0].split("/")[0]), uri=value.split(" ", 1)[1])
        for key, value in attrs
        if key == "extmap" and " " in value
    )
    server_ssrc = None
    for key, value in attrs:
        if key == "ssrc":
            server_ssrc = int(value.split()[0])
            break

    return JoinResponse(
        transport=TransportDescription(
            ufrag=ufrag,
            pwd=pwd,
            fingerprints=tuple(fingerprints),
            candidates=candidates,
            rtcp_mux=rtcp_mux,
        ),
        payload_types=payload_types,
        header_extensions=header_extensions,
        server_ssrc=server_ssrc,
    )


@dataclass
class JoinPayload:
    """The JSON body of ``phone.joinGroupCall``'s ``params`` argument.

    ``ssrc-groups`` is intentionally never emitted: this package publishes audio only,
    and Telegram rejects an empty group list (PROTOCOL.md §2).
    """

    ssrc: int
    ufrag: str
    pwd: str
    fingerprints: tuple[Fingerprint, ...]

    def to_json(self) -> dict[str, Any]:
        if not 0 < self.ssrc < 2**31:
            raise TransportError(f"SSRC must be a non-zero positive int32, got {self.ssrc}")
        if not self.ufrag or not self.pwd:
            raise TransportError("Join payload requires local ICE ufrag and pwd")
        if not self.fingerprints:
            raise TransportError("Join payload requires a local DTLS fingerprint")
        return {
            "ssrc": self.ssrc,
            "ufrag": self.ufrag,
            "pwd": self.pwd,
            "fingerprints": [fp.to_json() for fp in self.fingerprints],
        }

    def to_data_json(self) -> str:
        return json.dumps(self.to_json())


def sdp_offer_to_join_params(sdp: str, *, ssrc: int | None = None) -> JoinPayload:
    """Extract ``phone.joinGroupCall`` params from a local SDP **offer**."""
    attrs = _parse_attributes(sdp)
    ufrag = _first(attrs, "ice-ufrag")
    pwd = _first(attrs, "ice-pwd")
    if not ufrag or not pwd:
        raise TransportError("SDP offer has no ICE credentials")
    setup = _first(attrs, "setup") or "actpass"
    if setup == "actpass":
        # We always take the DTLS client role against Telegram's SFU.
        setup = "active"
    fingerprints = []
    for key, value in attrs:
        if key == "fingerprint":
            algorithm, _, digest = value.partition(" ")
            fingerprints.append(Fingerprint(hash=algorithm, fingerprint=digest, setup=setup))
    if ssrc is None:
        raw_ssrc = _first(attrs, "ssrc")
        if raw_ssrc is None:
            raise TransportError("SDP offer has no a=ssrc line and no ssrc was supplied")
        ssrc = int(raw_ssrc.split()[0])
    return JoinPayload(ssrc=ssrc, ufrag=ufrag, pwd=pwd, fingerprints=tuple(fingerprints))


def join_params_to_sdp_offer(
    payload: JoinPayload,
    *,
    payload_type: PayloadType = OPUS_DEFAULT,
    header_extensions: tuple[HeaderExtension, ...] = (),
    candidates: tuple[IceCandidate, ...] = (),
    mid: str = "0",
    session_id: int = 1,
    cname: str = "aytgcalls",
) -> str:
    """Render join params as a unified-plan SDP **offer** (inverse of the above)."""
    lines = [
        "v=0",
        f"o=- {session_id} 2 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        f"a=group:BUNDLE {mid}",
        "a=msid-semantic: WMS *",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {payload_type.id}",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{payload.ufrag}",
        f"a=ice-pwd:{payload.pwd}",
    ]
    for fp in payload.fingerprints:
        lines.append(f"a=fingerprint:{fp.hash} {fp.fingerprint}")
    lines.append(f"a=setup:{payload.fingerprints[0].setup if payload.fingerprints else 'active'}")
    lines.append(f"a=mid:{mid}")
    lines.append("a=sendonly")
    lines.append("a=rtcp-mux")
    suffix = f"/{payload_type.channels}" if payload_type.channels > 1 else ""
    lines.append(f"a=rtpmap:{payload_type.id} {payload_type.name}/{payload_type.clockrate}{suffix}")
    for fb in payload_type.rtcp_fbs:
        lines.append(f"a=rtcp-fb:{payload_type.id} {fb.to_sdp()}")
    if payload_type.parameters:
        params = ";".join(f"{k}={v}" for k, v in payload_type.parameters.items())
        lines.append(f"a=fmtp:{payload_type.id} {params}")
    for ext in header_extensions:
        lines.append(f"a=extmap:{ext.id} {ext.uri}")
    for cand in candidates:
        lines.append(f"a=candidate:{cand.to_sdp()}")
    lines.append(f"a=ssrc:{payload.ssrc} cname:{cname}")
    return "\r\n".join(lines) + "\r\n"

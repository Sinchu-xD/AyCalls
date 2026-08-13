"""Transport layer: local parameter generation, DTLS role logic, teardown."""

from __future__ import annotations

import asyncio
import json

import pytest

from aytgcalls.exceptions import TransportError
from aytgcalls.transport.ice import (
    from_aiortc_candidate,
    random_pwd,
    random_ssrc,
    random_ufrag,
    to_aiortc_candidate,
)
from aytgcalls.transport.sdp import IceCandidate, parse_join_response
from aytgcalls.transport.track import PcmStreamTrack
from aytgcalls.transport.webrtc import (
    SUPPORTED_HEADER_EXTENSIONS,
    TelegramTransport,
    _dtls_role_for,
)


def test_random_ice_credentials_meet_rfc5245_lengths() -> None:
    for _ in range(20):
        assert 4 <= len(random_ufrag()) <= 256
        assert 22 <= len(random_pwd()) <= 256


def test_random_ssrc_is_positive_int32() -> None:
    values = {random_ssrc() for _ in range(500)}
    assert all(0 < value < 2**31 for value in values)
    assert len(values) > 400  # not a constant


@pytest.mark.parametrize(
    ("remote_setup", "expected"),
    [("passive", "client"), ("actpass", "client"), ("", "client"), ("active", "server")],
)
def test_dtls_role_follows_remote_setup(remote_setup: str, expected: str) -> None:
    assert _dtls_role_for(remote_setup) == expected


def test_candidate_conversion_round_trip() -> None:
    candidate = IceCandidate(
        foundation="1",
        component=1,
        protocol="udp",
        priority=2130706431,
        ip="91.108.9.1",
        port=44445,
        type="host",
    )
    converted = to_aiortc_candidate(candidate)
    assert converted.sdpMid == "0"
    assert converted.port == 44445
    back = from_aiortc_candidate(converted)
    assert back.ip == candidate.ip and back.priority == candidate.priority


async def test_prepare_produces_a_valid_join_payload() -> None:
    transport = TelegramTransport(PcmStreamTrack())
    try:
        payload = await transport.prepare()
        data = payload.to_json()
        assert set(data) == {"ssrc", "ufrag", "pwd", "fingerprints"}
        assert 0 < data["ssrc"] < 2**31
        assert data["ssrc"] == transport.ssrc
        fingerprint = data["fingerprints"][0]
        assert fingerprint["hash"] == "sha-256"
        assert fingerprint["setup"] == "active"
        # 32 colon-separated hex octets
        assert len(fingerprint["fingerprint"].split(":")) == 32
        assert transport.local_candidates, "no ICE candidates gathered"
        assert json.loads(payload.to_data_json()) == data
    finally:
        await transport.close()


async def test_prepare_twice_is_rejected() -> None:
    transport = TelegramTransport(PcmStreamTrack())
    try:
        await transport.prepare()
        with pytest.raises(TransportError, match="called twice"):
            await transport.prepare()
    finally:
        await transport.close()


async def test_connect_before_prepare_is_rejected(telegram_join_response_json: str) -> None:
    transport = TelegramTransport(PcmStreamTrack())
    with pytest.raises(TransportError, match="before prepare"):
        await transport.connect(parse_join_response(telegram_join_response_json))
    await transport.close()


async def test_connect_refuses_rtmp_stream_calls() -> None:
    transport = TelegramTransport(PcmStreamTrack())
    try:
        await transport.prepare()
        with pytest.raises(TransportError, match="RTMP/stream broadcast"):
            await transport.connect(parse_join_response({"stream": True, "rtmp": True}))
    finally:
        await transport.close()


async def test_close_is_idempotent() -> None:
    transport = TelegramTransport(PcmStreamTrack())
    await transport.prepare()
    await transport.close()
    await transport.close()
    assert not transport.is_connected


async def test_unsupported_header_extensions_are_dropped(telegram_join_response: dict) -> None:
    telegram_join_response["audio"]["rtp-hdrexts"].append(
        {"id": 9, "uri": "urn:example:not-supported-by-aiortc"}
    )
    response = parse_join_response(telegram_join_response)
    kept = [ext for ext in response.header_extensions if ext.uri in SUPPORTED_HEADER_EXTENSIONS]
    assert len(response.header_extensions) == 3
    assert len(kept) == 2
    assert all(ext.uri in SUPPORTED_HEADER_EXTENSIONS for ext in kept)


async def test_ice_failure_is_reported_not_hidden(telegram_join_response: dict) -> None:
    """A candidate that cannot be reached must raise ICEFailed, never hang forever."""
    from aytgcalls.exceptions import ICEFailed

    telegram_join_response["transport"]["candidates"] = [
        {
            "generation": "0", "component": "1", "protocol": "udp", "port": "9",
            "ip": "192.0.2.1",  # TEST-NET-1, guaranteed unroutable
            "foundation": "1", "priority": "1", "type": "host", "network": "0",
        }
    ]
    transport = TelegramTransport(PcmStreamTrack(), connect_timeout=2.0)
    try:
        await transport.prepare()
        with pytest.raises(ICEFailed):
            await asyncio.wait_for(
                transport.connect(parse_join_response(telegram_join_response)), timeout=30
            )
    finally:
        await transport.close()

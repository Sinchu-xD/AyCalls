"""SDP <-> Telegram JSON bridge."""

from __future__ import annotations

import json

import pytest

from aytgcalls.exceptions import TransportError
from aytgcalls.transport.sdp import (
    Fingerprint,
    IceCandidate,
    JoinPayload,
    join_params_to_sdp_offer,
    join_response_to_sdp,
    parse_join_response,
    sdp_answer_to_join_response,
    sdp_offer_to_join_params,
)


def test_parse_nested_payload(telegram_join_response_json: str) -> None:
    response = parse_join_response(telegram_join_response_json)
    assert response.transport.ufrag == "9aBc"
    assert response.transport.setup == "passive"
    assert len(response.transport.candidates) == 2
    assert response.transport.candidates[0].port == 44445  # coerced from string
    assert response.transport.candidates[0].priority == 2130706431
    assert response.opus.id == 111
    assert response.opus.channels == 2
    assert response.server_ssrc == 987654321
    assert not response.is_stream


def test_parse_accepts_dict_and_string(telegram_join_response: dict) -> None:
    from_dict = parse_join_response(telegram_join_response)
    from_str = parse_join_response(json.dumps(telegram_join_response))
    assert from_dict.transport.ufrag == from_str.transport.ufrag


def test_parse_flattened_transport(telegram_join_response: dict) -> None:
    """Some server builds put the transport fields at the top level."""
    flat = dict(telegram_join_response["transport"])
    flat["payload-types"] = telegram_join_response["audio"]["payload-types"]
    flat["rtp-hdrexts"] = telegram_join_response["audio"]["rtp-hdrexts"]
    response = parse_join_response(flat)
    assert response.transport.pwd == telegram_join_response["transport"]["pwd"]
    assert response.opus.id == 111
    assert len(response.header_extensions) == 2


def test_parse_stream_mode_is_flagged() -> None:
    response = parse_join_response({"stream": True, "rtmp": True})
    assert response.is_stream and response.is_rtmp
    with pytest.raises(TransportError, match="RTMP/stream mode"):
        join_response_to_sdp(response)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not json", "not valid JSON"),
        ({"audio": {}}, "no 'transport' object"),
        ({"transport": {"ufrag": "a"}}, "ICE ufrag/pwd"),
        ({"transport": {"ufrag": "a", "pwd": "b"}}, "DTLS fingerprints"),
    ],
)
def test_parse_rejects_garbage(payload: object, match: str) -> None:
    with pytest.raises(TransportError, match=match):
        parse_join_response(payload)  # type: ignore[arg-type]


def test_missing_opus_falls_back_to_pt_111(telegram_join_response: dict) -> None:
    telegram_join_response["audio"]["payload-types"] = []
    response = parse_join_response(telegram_join_response)
    assert response.opus.id == 111
    assert response.opus.name == "opus"


def test_json_to_sdp_shape(telegram_join_response_json: str) -> None:
    sdp = join_response_to_sdp(parse_join_response(telegram_join_response_json))
    assert sdp.startswith("v=0\r\n")
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 111" in sdp
    assert "a=ice-ufrag:9aBc" in sdp
    assert "a=setup:passive" in sdp
    assert "a=rtcp-mux" in sdp
    assert "a=rtpmap:111 opus/48000/2" in sdp
    assert "a=fmtp:111 minptime=10;useinbandfec=1" in sdp
    assert "a=rtcp-fb:111 transport-cc" in sdp
    assert "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level" in sdp
    assert "a=candidate:1 1 udp 2130706431 91.108.9.1 44445 typ host generation 0" in sdp
    assert "a=end-of-candidates" in sdp
    # single audio m-line only
    assert sdp.count("m=") == 1


def test_json_sdp_json_round_trip(telegram_join_response_json: str) -> None:
    """json -> SDP -> json must be lossless for everything we model."""
    original = parse_join_response(telegram_join_response_json)
    recovered = sdp_answer_to_join_response(join_response_to_sdp(original))

    assert recovered.transport.ufrag == original.transport.ufrag
    assert recovered.transport.pwd == original.transport.pwd
    assert recovered.transport.setup == original.transport.setup
    assert recovered.transport.rtcp_mux == original.transport.rtcp_mux
    assert [fp.fingerprint for fp in recovered.transport.fingerprints] == [
        fp.fingerprint for fp in original.transport.fingerprints
    ]
    assert recovered.transport.candidates == original.transport.candidates
    assert recovered.payload_types == original.payload_types
    assert recovered.header_extensions == original.header_extensions
    assert recovered.server_ssrc == original.server_ssrc
    assert recovered.to_json() == original.to_json()


def test_candidate_sdp_round_trip() -> None:
    candidate = IceCandidate(
        foundation="3",
        component=1,
        protocol="udp",
        priority=1686052607,
        ip="10.0.0.5",
        port=54321,
        type="srflx",
        rel_addr="192.168.1.5",
        rel_port=54320,
        generation=0,
    )
    assert IceCandidate.from_sdp(candidate.to_sdp()) == candidate


def test_candidate_from_sdp_rejects_garbage() -> None:
    with pytest.raises(TransportError, match="Malformed ICE candidate"):
        IceCandidate.from_sdp("nonsense")


def _payload() -> JoinPayload:
    return JoinPayload(
        ssrc=123456789,
        ufrag="Ku4t",
        pwd="0Nx04rHqQhoUlVfEPHNqRSU9",
        fingerprints=(Fingerprint("sha-256", "AA:BB:CC", "active"),),
    )


def test_join_payload_json_is_audio_only() -> None:
    data = _payload().to_json()
    assert set(data) == {"ssrc", "ufrag", "pwd", "fingerprints"}
    # Telegram rejects an empty ssrc-groups array for audio-only publishers.
    assert "ssrc-groups" not in data
    assert data["fingerprints"][0]["setup"] == "active"
    assert json.loads(_payload().to_data_json()) == data


@pytest.mark.parametrize("ssrc", [0, -1, 2**31, 2**32 - 1])
def test_join_payload_rejects_bad_ssrc(ssrc: int) -> None:
    payload = JoinPayload(
        ssrc=ssrc, ufrag="a", pwd="b", fingerprints=(Fingerprint("sha-256", "X"),)
    )
    with pytest.raises(TransportError, match="non-zero positive int32"):
        payload.to_json()


def test_join_payload_requires_credentials_and_fingerprint() -> None:
    with pytest.raises(TransportError, match="ufrag and pwd"):
        JoinPayload(ssrc=1, ufrag="", pwd="", fingerprints=()).to_json()
    with pytest.raises(TransportError, match="DTLS fingerprint"):
        JoinPayload(ssrc=1, ufrag="a", pwd="b", fingerprints=()).to_json()


def test_offer_to_params_round_trip() -> None:
    payload = _payload()
    offer = join_params_to_sdp_offer(payload)
    assert "a=sendonly" in offer
    assert f"a=ssrc:{payload.ssrc} cname:aytgcalls" in offer
    recovered = sdp_offer_to_join_params(offer)
    assert recovered.to_json() == payload.to_json()


def test_offer_actpass_becomes_active() -> None:
    offer = (
        "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        "a=ice-ufrag:abcd\r\na=ice-pwd:0123456789012345678901\r\n"
        "a=fingerprint:sha-256 DE:AD:BE:EF\r\na=setup:actpass\r\n"
        "a=ssrc:42 cname:x\r\n"
    )
    params = sdp_offer_to_join_params(offer)
    assert params.fingerprints[0].setup == "active"
    assert params.ssrc == 42


def test_offer_without_ssrc_requires_explicit_value() -> None:
    offer = (
        "v=0\r\na=ice-ufrag:abcd\r\na=ice-pwd:0123456789012345678901\r\n"
        "a=fingerprint:sha-256 DE:AD:BE:EF\r\na=setup:active\r\n"
    )
    with pytest.raises(TransportError, match="no a=ssrc line"):
        sdp_offer_to_join_params(offer)
    assert sdp_offer_to_join_params(offer, ssrc=7).ssrc == 7


def test_offer_without_ice_credentials_is_rejected() -> None:
    with pytest.raises(TransportError, match="no ICE credentials"):
        sdp_offer_to_join_params("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n")

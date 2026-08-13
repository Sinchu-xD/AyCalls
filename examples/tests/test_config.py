"""Configuration, credential loading and log redaction."""

from __future__ import annotations

import json
import logging

import pytest

from aytgcalls.config import CallConfig, TelegramCredentials
from aytgcalls.exceptions import FFmpegNotInstalled
from aytgcalls.logger import dump_signaling, get_logger, redact


def test_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "deadbeef")
    monkeypatch.setenv("STRING_SESSION", "BQ...session...")
    credentials = TelegramCredentials.from_env()
    assert credentials.api_id == 12345
    assert credentials.is_complete
    credentials.require()


def test_credentials_report_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("API_ID", "API_HASH", "STRING_SESSION", "SESSION_STRING"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="API_ID, API_HASH, STRING_SESSION"):
        TelegramCredentials.from_env().require()


def test_credentials_repr_never_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_HASH", "super-secret-hash")
    monkeypatch.setenv("STRING_SESSION", "super-secret-session")
    text = repr(TelegramCredentials.from_env())
    assert "super-secret" not in text
    assert "api_hash=set" in text


def test_credentials_reject_non_integer_api_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ID", "not-a-number")
    with pytest.raises(ValueError, match="not an integer"):
        TelegramCredentials.from_env()


def test_config_defaults_match_telegram_expectations() -> None:
    config = CallConfig()
    assert config.opus_bitrate == 96_000  # inside Telegram's 64-128 kbps window
    assert config.buffer_ms >= config.prefetch_ms
    assert config.ice_servers == ()  # SFU is ICE-lite on a public IP; STUN not needed


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"buffer_ms": 10}, "buffer_ms must be at least"),
        ({"prefetch_ms": 5000}, "prefetch_ms cannot exceed"),
        ({"volume": 500}, "volume must be between"),
        ({"opus_bitrate": 10}, "opus_bitrate must be between"),
    ],
)
def test_config_validates_ranges(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CallConfig(**kwargs)


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AYTGCALLS_OPUS_BITRATE", "64000")
    monkeypatch.setenv("AYTGCALLS_BUFFER_MS", "600")
    monkeypatch.setenv("AYTGCALLS_ICE_SERVERS", "stun:a.example:3478, stun:b.example:3478")
    config = CallConfig.from_env()
    assert config.opus_bitrate == 64_000
    assert config.buffer_ms == 600
    assert config.ice_servers == ("stun:a.example:3478", "stun:b.example:3478")


def test_config_rejects_unknown_overrides() -> None:
    with pytest.raises(TypeError, match="Unknown CallConfig fields"):
        CallConfig.from_env(nonsense=1)


def test_missing_ffmpeg_is_actionable() -> None:
    with pytest.raises(FFmpegNotInstalled, match="AYTGCALLS_FFMPEG"):
        CallConfig(ffmpeg_path="definitely-not-ffmpeg-binary").resolve_ffmpeg()


def test_redaction_masks_secrets() -> None:
    payload = {
        "ufrag": "public",
        "pwd": "super-secret-ice-password",
        "fingerprints": [{"hash": "sha-256", "fingerprint": "AA:BB:CC:DD:EE:FF"}],
        "nested": {"access_hash": 12345678},
    }
    masked = redact(payload)
    assert masked["ufrag"] == "public"
    assert "super-secret-ice-password" not in json.dumps(masked)
    assert "AA:BB:CC:DD:EE:FF" not in json.dumps(masked)
    assert masked["nested"]["access_hash"] == "***"


def test_dump_signaling_is_a_noop_when_debug_is_off(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.dump")
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        dump_signaling(logger, "payload", {"pwd": "secret"})
    assert not caplog.records


def test_dump_signaling_redacts_when_debug_is_on(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("test.dump2")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="aytgcalls.test.dump2"):
        dump_signaling(logger, "payload", json.dumps({"pwd": "secret-value-here", "ssrc": 7}))
    text = caplog.text
    assert "secret-value-here" not in text
    assert '"ssrc": 7' in text

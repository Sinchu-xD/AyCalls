"""Exception hierarchy for :mod:`aytgcalls`.

Every failure mode in the package raises a subclass of :class:`AytgcallsError`.
Nothing is ever swallowed silently.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AytgcallsError",
    "BotClientNotAllowed",
    "GroupCallNotFound",
    "NotInGroup",
    "AlreadyJoined",
    "NotJoined",
    "AlreadyPlaying",
    "NotPlaying",
    "InvalidAudioSource",
    "MediaSourceError",
    "FFmpegError",
    "FFmpegNotInstalled",
    "OpusError",
    "TransportError",
    "ICEFailed",
    "DTLSHandshakeFailed",
    "TelegramCallError",
]


class AytgcallsError(Exception):
    """Base class for every error raised by aytgcalls."""


class BotClientNotAllowed(AytgcallsError):
    """Raised when a bot session is used where a user session is required.

    A bot account cannot become a group-call participant. Only a user session
    created from ``API_ID`` + ``API_HASH`` + ``STRING_SESSION`` can join a voice chat.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "A bot account cannot join a Telegram group voice chat. "
            "Pass a Kurigram *user* session (API_ID + API_HASH + STRING_SESSION) to "
            "GroupCall(). A bot token may only be used for the command interface, which "
            "then dispatches to the user ('assistant') client." + (f" {detail}" if detail else "")
        )


class GroupCallNotFound(AytgcallsError):
    """No active voice chat in the target chat."""


class NotInGroup(AytgcallsError):
    """The user session is not a member of the target chat."""


class AlreadyJoined(AytgcallsError):
    """join() called while already joined."""


class NotJoined(AytgcallsError):
    """An operation requiring an active call was attempted before joining."""


class AlreadyPlaying(AytgcallsError):
    """play() called while a track is already playing and replace was not requested."""


class NotPlaying(AytgcallsError):
    """pause()/resume()/skip() called with nothing playing."""


class InvalidAudioSource(AytgcallsError):
    """The audio source is not a readable local file, nor an http(s) URL."""


class MediaSourceError(AytgcallsError):
    """The source exists but FFmpeg could not decode it."""


class FFmpegError(AytgcallsError):
    """FFmpeg exited non-zero or died mid-stream."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        detail = message
        if returncode is not None:
            detail += f" (exit code {returncode})"
        if stderr:
            detail += f"\nffmpeg stderr:\n{stderr.strip()}"
        super().__init__(detail)


class FFmpegNotInstalled(FFmpegError):
    """The ffmpeg binary could not be found on PATH."""

    def __init__(self, binary: str = "ffmpeg") -> None:
        super().__init__(
            f"{binary!r} was not found. Install it first:\n"
            "  Debian/Ubuntu: sudo apt install -y ffmpeg\n"
            "  Fedora:        sudo dnf install -y ffmpeg\n"
            "  macOS:         brew install ffmpeg\n"
            "Or set AYTGCALLS_FFMPEG=/path/to/ffmpeg."
        )


class OpusError(AytgcallsError):
    """Opus encoding failed."""


class TransportError(AytgcallsError):
    """Generic WebRTC transport failure."""


class ICEFailed(TransportError):
    """ICE connectivity checks never succeeded."""


class DTLSHandshakeFailed(TransportError):
    """The DTLS-SRTP handshake with Telegram's SFU failed."""


class TelegramCallError(AytgcallsError):
    """Wraps an RPC error returned by a ``phone.*`` method.

    Known RPC errors are given an actionable explanation.
    """

    #: RPC error string -> human explanation.
    EXPLANATIONS: dict[str, str] = {
        "DATA_JSON_INVALID": (
            "Telegram rejected the join payload JSON. The payload must contain exactly "
            "ssrc/ufrag/pwd/fingerprints and must NOT contain an empty 'ssrc-groups' array."
        ),
        "GROUPCALL_INVALID": (
            "The InputGroupCall is stale. The voice chat was probably restarted; "
            "re-run discovery to get a fresh call id/access_hash."
        ),
        "GROUPCALL_FORBIDDEN": (
            "This account is not allowed in that voice chat (kicked, banned, or the call "
            "is restricted to admins)."
        ),
        "CHAT_ADMIN_REQUIRED": (
            "Admin rights are required for this action in this chat. For joining, ask an admin "
            "to allow members to speak, or promote the assistant account."
        ),
        "GROUPCALL_SSRC_DUPLICATE_MUCH": (
            "The SSRC is already registered with the SFU — a previous session was not cleanly "
            "left. aytgcalls generates a fresh SSRC per join; wait a few seconds and retry."
        ),
        "JOIN_AS_PEER_INVALID": (
            "The join_as peer is not allowed. Use phone.GetGroupCallJoinAs to list valid peers, "
            "or pass join_as=None to join as yourself."
        ),
        "PARTICIPANT_JOIN_MISSING": (
            "The account is not currently a participant of the call."
        ),
        "GROUPCALL_JOIN_MISSING": (
            "The account is not currently joined to the call (the SFU dropped the session)."
        ),
    }

    def __init__(self, message: str, *, rpc_error: BaseException | None = None) -> None:
        self.rpc_error = rpc_error
        self.error_id = self._extract_id(rpc_error) if rpc_error is not None else None
        detail = message
        if self.error_id:
            detail += f" [{self.error_id}]"
            hint = self.EXPLANATIONS.get(self.error_id)
            if hint:
                detail += f"\n{hint}"
        elif rpc_error is not None:
            detail += f": {rpc_error}"
        super().__init__(detail)

    @staticmethod
    def _extract_id(rpc_error: Any) -> str | None:
        for attr in ("ID", "id", "MESSAGE"):
            value = getattr(rpc_error, attr, None)
            if isinstance(value, str) and value:
                return value
        text = str(rpc_error)
        for known in TelegramCallError.EXPLANATIONS:
            if known in text:
                return known
        return None

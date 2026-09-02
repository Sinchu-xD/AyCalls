"""Kurigram user-client helpers and the bot-session guard.

``kurigram`` installs under the import name ``pyrogram``. Everything in this module
imports it lazily so the rest of the package (SDP bridge, media pipeline, queue) stays
importable and unit-testable without an MTProto stack present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..config import TelegramCredentials
from ..exceptions import AytgcallsError, BotClientNotAllowed
from ..logger import get_logger

if TYPE_CHECKING:
    from pyrogram import Client

logger = get_logger("telegram.client")

__all__ = [
    "import_pyrogram",
    "build_user_client",
    "ensure_user_session",
    "resolve_self_id",
]

_CONFLICT_HINT = (
    "kurigram must be installed alone. Run:\n"
    "  pip uninstall -y pyrogram pyrofork tgcalls py-tgcalls pytgcalls\n"
    "  pip install -U kurigram\n"
    "(kurigram is a maintained Pyrogram fork and still imports as `pyrogram`.)"
)


def import_pyrogram() -> Any:
    """Import kurigram (as ``pyrogram``) with a helpful error if it is missing/conflicting."""
    try:
        import pyrogram
    except ImportError as exc:
        raise AytgcallsError(
            "kurigram is not installed. " + _CONFLICT_HINT
        ) from exc
    if not hasattr(pyrogram, "Client"):
        raise AytgcallsError("The installed `pyrogram` module looks broken. " + _CONFLICT_HINT)
    return pyrogram


def build_user_client(
    credentials: TelegramCredentials | None = None,
    *,
    name: str = "aytgcalls_assistant",
    **client_kwargs: Any,
) -> Client:
    """Create (but do not start) a Kurigram user client from a string session.

    Credentials default to :meth:`TelegramCredentials.from_env`; they are never hardcoded.
    """
    credentials = (credentials or TelegramCredentials.from_env()).require()
    pyrogram = import_pyrogram()
    return pyrogram.Client(
        name=name,
        api_id=credentials.api_id,
        api_hash=credentials.api_hash,
        session_string=credentials.session_string,
        in_memory=True,
        **client_kwargs,
    )


async def ensure_user_session(client: Client) -> Any:
    """Assert that ``client`` is a started **user** session.

    :raises BotClientNotAllowed: if the client is a bot.
    :returns: the ``User`` object for the session.
    """
    me = getattr(client, "me", None)
    if me is None:
        get_me = getattr(client, "get_me", None)
        if get_me is None:
            raise AytgcallsError(
                "The object passed to GroupCall() is not a Kurigram Client "
                "(no .me and no .get_me())."
            )
        me = await get_me()
    if getattr(me, "is_bot", False):
        raise BotClientNotAllowed(f"Session belongs to bot @{getattr(me, 'username', '?')}.")
    logger.debug(
        "Using user session id=%s username=%s", getattr(me, "id", "?"), getattr(me, "username", "?")
    )
    return me


async def resolve_self_id(client: Client) -> int:
    """Return the numeric user id of the session."""
    me = await ensure_user_session(client)
    return int(me.id)

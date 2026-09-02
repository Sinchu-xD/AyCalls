"""Raw ``phone.*`` MTProto calls.

This layer is deliberately thin and standalone-testable: it takes a Kurigram client,
invokes raw TL functions, and translates RPC errors into :class:`TelegramCallError`.
It knows nothing about WebRTC beyond passing opaque JSON strings around.

Verified against the TL schema shipped with kurigram 2.2.24 (PROTOCOL.md §8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..exceptions import TelegramCallError, TransportError
from ..logger import dump_signaling, get_logger
from .client import import_pyrogram

if TYPE_CHECKING:
    from pyrogram import Client

logger = get_logger("telegram.signaling")

__all__ = ["GroupCallSignaling", "JoinResult"]


@dataclass
class JoinResult:
    """Outcome of ``phone.joinGroupCall``."""

    #: The raw JSON string Telegram returned in ``UpdateGroupCallConnection.params``.
    params: str
    #: The full ``Updates`` object, for callers that want participant info.
    updates: Any = None

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.params)


class GroupCallSignaling:
    """Raw MTProto operations for one group call.

    All methods raise :class:`TelegramCallError` on RPC failure, with an explanation
    attached for the well-known error ids.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._pyrogram = import_pyrogram()

    # -- internals --------------------------------------------------------------------

    @property
    def raw(self) -> Any:
        return self._pyrogram.raw

    async def _invoke(self, request: Any, *, what: str) -> Any:
        errors = self._pyrogram.errors
        try:
            return await self._client.invoke(request)
        except errors.RPCError as exc:
            raise TelegramCallError(f"{what} failed", rpc_error=exc) from exc

    # -- operations -------------------------------------------------------------------

    async def get_group_call(self, call: Any, *, limit: int = 1) -> Any:
        """``phone.GetGroupCall`` — current state and participants."""
        return await self._invoke(
            self.raw.functions.phone.GetGroupCall(call=call, limit=limit),
            what="phone.getGroupCall",
        )

    async def get_join_as_peers(self, peer: Any) -> Any:
        """``phone.GetGroupCallJoinAs`` — peers this account may join as."""
        return await self._invoke(
            self.raw.functions.phone.GetGroupCallJoinAs(peer=peer),
            what="phone.getGroupCallJoinAs",
        )

    async def join(
        self,
        call: Any,
        *,
        params_json: str,
        join_as: Any = None,
        muted: bool = False,
        video_stopped: bool = True,
        invite_hash: str | None = None,
    ) -> JoinResult:
        """``phone.JoinGroupCall`` — returns the SFU's transport JSON.

        ``params_json`` must be the audio-only payload documented in PROTOCOL.md §2.
        """
        if join_as is None:
            join_as = self.raw.types.InputPeerSelf()
        dump_signaling(logger, "phone.joinGroupCall params", params_json)
        request = self.raw.functions.phone.JoinGroupCall(
            call=call,
            join_as=join_as,
            params=self.raw.types.DataJSON(data=params_json),
            muted=muted,
            video_stopped=video_stopped,
            invite_hash=invite_hash,
        )
        updates = await self._invoke(request, what="phone.joinGroupCall")
        response = self.extract_connection_params(updates)
        if response is None:
            raise TransportError(
                "phone.joinGroupCall succeeded but returned no UpdateGroupCallConnection; "
                f"got updates of type {type(updates).__name__}"
            )
        dump_signaling(logger, "phone.joinGroupCall response", response)
        return JoinResult(params=response, updates=updates)

    def extract_connection_params(self, updates: Any) -> str | None:
        """Find the transport JSON in an ``Updates`` container.

        Handles both ``UpdateGroupCallConnection`` (current) and a bare ``DataJSON``
        on the result object (older/edge server builds).
        """
        connection_cls = getattr(
            self.raw.types, "UpdateGroupCallConnection", None
        )
        for update in getattr(updates, "updates", []) or []:
            if connection_cls is not None and isinstance(update, connection_cls):
                if getattr(update, "presentation", False):
                    continue  # presentation connection belongs to screen sharing
                return update.params.data
        params = getattr(updates, "params", None)
        if params is not None and hasattr(params, "data"):
            return params.data
        return None

    async def check(self, call: Any, sources: list[int]) -> list[int]:
        """``phone.CheckGroupCall`` — returns the subset of sources still connected."""
        result = await self._invoke(
            self.raw.functions.phone.CheckGroupCall(call=call, sources=list(sources)),
            what="phone.checkGroupCall",
        )
        if isinstance(result, list):
            return [int(x) for x in result]
        return [int(x) for x in getattr(result, "sources", []) or []]

    async def edit_participant(
        self,
        call: Any,
        *,
        participant: Any = None,
        muted: bool | None = None,
        volume: int | None = None,
        raise_hand: bool | None = None,
    ) -> Any:
        """``phone.EditGroupCallParticipant`` — server-side mute/volume.

        ``volume`` is Telegram's hundredths-of-a-percent scale: 10000 == 100%.
        """
        if participant is None:
            participant = self.raw.types.InputPeerSelf()
        if volume is not None:
            volume = max(0, min(20_000, int(volume)))
        return await self._invoke(
            self.raw.functions.phone.EditGroupCallParticipant(
                call=call,
                participant=participant,
                muted=muted,
                volume=volume,
                raise_hand=raise_hand,
            ),
            what="phone.editGroupCallParticipant",
        )

    async def leave(self, call: Any, source: int) -> Any:
        """``phone.LeaveGroupCall``."""
        return await self._invoke(
            self.raw.functions.phone.LeaveGroupCall(call=call, source=int(source)),
            what="phone.leaveGroupCall",
        )

    async def set_title(self, call: Any, title: str) -> Any:
        """``phone.EditGroupCallTitle`` — rename the voice chat."""
        return await self._invoke(
            self.raw.functions.phone.EditGroupCallTitle(call=call, title=title),
            what="phone.editGroupCallTitle",
        )

    async def get_participants(self, call: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        """``phone.GetGroupCall`` with enough participants.

        :returns: a list of ``{"user_id": int, "muted": bool, ...}`` dicts.
        """
        result = await self.get_group_call(call, limit=limit)
        participants = getattr(result, "participants", []) or []
        out: list[dict[str, Any]] = []
        for p in participants:
            peer = getattr(p, "peer", None)
            user_id = getattr(getattr(peer, "user_id", None), "user_id", None) or getattr(
                peer, "user_id", None
            )
            if user_id is None:
                continue
            out.append(
                {
                    "user_id": int(user_id),
                    "muted": bool(getattr(p, "muted", False)),
                    "can_self_unmute": bool(getattr(p, "can_self_unmute", True)),
                    "is_speaking": bool(getattr(p, "is_speaking", False)),
                    "raise_hand": bool(getattr(p, "raise_hand", False)),
                    "volume": int(getattr(p, "volume", 0)),
                }
            )
        return out

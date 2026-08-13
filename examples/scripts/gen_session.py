#!/usr/bin/env python3
"""Generate a Kurigram STRING_SESSION for the assistant (user) account.

    export API_ID=...      # from https://my.telegram.org -> API development tools
    export API_HASH=...
    python examples/scripts/gen_session.py

The session string is printed **once**. Treat it like a password: anyone holding it has
full access to the account. Store it in an environment variable or a secret manager —
never in source control.
"""

from __future__ import annotations

import asyncio
import os

from pyrogram import Client  # provided by kurigram


async def main() -> None:
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Set API_ID and API_HASH first (https://my.telegram.org).")

    async with Client(
        name="aytgcalls_session_generator",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        me = await app.get_me()
        if me.is_bot:
            raise SystemExit(
                "That is a bot account. A bot cannot join a voice chat — log in with a "
                "real user account (phone number)."
            )
        print("\n" + "=" * 72)
        print(f"Logged in as: {me.first_name} (@{me.username or '-'}, id={me.id})")
        print("=" * 72)
        print("\nSTRING_SESSION (keep this secret):\n")
        print(await app.export_session_string())
        print("\nUse it as:  export STRING_SESSION='…'\n")


if __name__ == "__main__":
    asyncio.run(main())

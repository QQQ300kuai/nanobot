"""React to accounts leaving and rejoining the allowlist."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from loguru import logger

from nanobot.multiuser.allowlist import UserAllowlist
from nanobot.multiuser.identity import UserPaths, validate_user_id
from nanobot.multiuser.stack import UserStackPool


class UserLifecycle:
    """Tear an account's runtime down when it is removed, and wake its schedule when it returns.

    Tokens are revoked by the account store and the router drops messages for non-members,
    so what remains is closing the live connections and the account's runtime. Closing the
    runtime cancels its in-flight turns and stops its cron service; the job definitions
    stay on disk and run again once the account's runtime reopens.
    """

    def __init__(
        self,
        pool: UserStackPool,
        workspaces_root: Path,
        *,
        disconnect: Callable[[str], Awaitable[None]],
    ) -> None:
        self._pool = pool
        self._root = Path(workspaces_root)
        self._disconnect = disconnect
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, allowlist: UserAllowlist) -> None:
        allowlist.subscribe_removed(lambda uid: self._spawn(self.on_user_removed(uid)))
        allowlist.subscribe_added(lambda uid: self._spawn(self.on_user_added(uid)))

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(self._guarded(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guarded(work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:
            logger.exception("User lifecycle handler failed")

    async def on_user_removed(self, user_id: str) -> None:
        uid = validate_user_id(user_id)
        try:
            await self._disconnect(uid)
        finally:
            await self._pool.evict(uid)
        logger.info("Account {} removed: connections closed, runtime and schedule stopped", uid)

    async def on_user_added(self, user_id: str) -> None:
        """Reopen the runtime of a returning account that has a schedule, so its jobs run again."""
        uid = validate_user_id(user_id)
        if UserPaths(self._root, uid).cron_store.exists():
            await self._pool.get(uid)
            logger.info("Account {} returned: schedule resumed", uid)

    async def aclose(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def settled(self) -> None:
        """Wait for the removal and return handlers started so far."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

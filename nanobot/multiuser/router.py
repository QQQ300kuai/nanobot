"""Route authenticated inbound messages to per-account runtimes."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.runtime_admission import RuntimeMaintenanceError

if TYPE_CHECKING:
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.multiuser.stack import UserStackPool

_MAX_SWEEP_INTERVAL_S = 60.0


class UserRouter:
    """Consume the front bus and hand each message to its account's stack.

    Channels keep publishing to and consuming from the front bus. Each stack's outbound
    queue is forwarded back to the front bus, so channel delivery is unchanged. A message
    without a server-assigned ``user_id`` is dropped: there is no default account.
    """

    def __init__(
        self,
        front_bus: MessageBus,
        pool: UserStackPool,
        *,
        is_member: Callable[[str], bool] | None = None,
    ) -> None:
        self._front = front_bus
        self._pool = pool
        # Re-checked at every hand-off: an account removed from the allowlist gets nothing.
        self._is_member = is_member or (lambda _user_id: True)
        # Per-account FIFO of messages waiting for their stack to open.
        self._queues: dict[str, deque[InboundMessage]] = {}
        self._dispatches: set[asyncio.Task[None]] = set()
        self._running = False

    async def run(self) -> None:
        self._running = True
        sweeper = asyncio.create_task(self._sweep_periodically(), name="nanobot-user-pool-sweep")
        try:
            while self._running:
                self.route(await self._front.consume_inbound())
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        """Abandon hand-offs still waiting for a runtime to open."""
        self.stop()
        for task in tuple(self._dispatches):
            task.cancel()
        await asyncio.gather(*self._dispatches, return_exceptions=True)

    def route(self, msg: InboundMessage) -> None:
        """Queue one message for its account; order is preserved per account."""
        user_id = msg.user_id
        if user_id is None:
            logger.warning(
                "Dropping inbound message without an account: channel={} chat_id={} sender={}",
                msg.channel, msg.chat_id, msg.sender_id,
            )
            return
        if not self._is_member(user_id):
            logger.warning("Dropping inbound message for removed account {}", user_id)
            return
        queue = self._queues.get(user_id)
        if queue is None:
            queue = self._queues[user_id] = deque()
            task = asyncio.create_task(
                self._dispatch(user_id, queue), name=f"nanobot-user-route-{user_id}",
            )
            self._dispatches.add(task)
            task.add_done_callback(self._dispatches.discard)
        queue.append(msg)

    async def _dispatch(self, user_id: str, queue: deque[InboundMessage]) -> None:
        try:
            stack = await self._pool.get(user_id)
            if not self._is_member(user_id):
                # Removed while the stack was opening: nothing may run for this account.
                await self._pool.evict(user_id)
                logger.warning("Dropping {} inbound message(s) for removed account {}", len(queue), user_id)
                return
            stack.attach_outbound(self._front.publish_outbound)
            while queue:
                if not self._is_member(user_id):
                    logger.warning("Dropping {} inbound message(s) for removed account {}", len(queue), user_id)
                    return
                msg = queue.popleft()
                try:
                    await stack.bus.publish_inbound(msg)
                except RuntimeMaintenanceError:
                    logger.warning("Dropping inbound message for user {} during maintenance", user_id)
        except Exception:
            logger.exception(
                "Dropping {} inbound message(s): runtime for user {} is unavailable",
                len(queue), user_id,
            )
        finally:
            self._queues.pop(user_id, None)

    def has_pending_work(self) -> bool:
        return bool(
            self._front.inbound_size or self._queues
            or any(stack.busy() for stack in self._pool.active())
        )

    def maintenance(self) -> AbstractContextManager[None]:
        """Close intake only when no account runtime holds work (stacks share the front admission)."""
        return self._front.admission.pause_if_idle(self.has_pending_work)

    async def _sweep_periodically(self) -> None:
        interval = max(1.0, min(self._pool.idle_ttl_s / 2, _MAX_SWEEP_INTERVAL_S))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._pool.sweep()
            except Exception:
                logger.exception("User runtime eviction sweep failed")

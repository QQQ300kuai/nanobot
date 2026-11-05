"""Per-account agent runtimes and their bounded, lazily opened pool."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.bus.events import OUTBOUND_META_ACCOUNT
from nanobot.llm_usage.context import llm_usage_user
from nanobot.multiuser.identity import UserPaths, validate_user_id

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService
    from nanobot.session.manager import SessionManager
    from nanobot.session.webui_turns import WebuiTurnCoordinator
    from nanobot.webui.recovery import RecoveryCoordinator

OutboundSink = Callable[["OutboundMessage"], Awaitable[None]]

_CLOSE_TIMEOUT_S = 15.0


class UserStack:
    """One account's agent runtime: its own bus, session store, workspace and agent loop.

    The bus is private to the stack. Inbound messages arrive only through the router;
    outbound messages leave only through the sink attached with ``attach_outbound``.
    """

    def __init__(
        self,
        *,
        user_id: str,
        paths: UserPaths,
        bus: MessageBus,
        sessions: SessionManager,
        agent: AgentLoop,
        recovery: RecoveryCoordinator | None = None,
        webui_turns: WebuiTurnCoordinator | None = None,
        cron: CronService | None = None,
    ) -> None:
        self.user_id = user_id
        self.paths = paths
        self.bus = bus
        self.sessions = sessions
        self.agent = agent
        self._recovery = recovery
        self._webui_turns = webui_turns
        # The account's own schedule; it fires only while the stack is open.
        self.cron = cron
        self._connections = ExitStack()
        # Run once the stack has stopped, e.g. to release resources held for this account.
        self.close_hooks: list[Callable[[], Awaitable[None]]] = []
        self._run_task: asyncio.Task[None] | None = None
        self._outbound_task: asyncio.Task[None] | None = None
        self._sink: OutboundSink | None = None

    async def start(self) -> None:
        """Recover interrupted work, then start consuming the stack's inbound queue."""
        # Tasks created below (agent loop, cron timer, subagent recovery) inherit the owner.
        with llm_usage_user(self.user_id):
            await self._start()

    async def _start(self) -> None:
        if self._webui_turns is not None:
            self._connections.enter_context(self._webui_turns.connected())
        await self.agent.subagents.recover_interrupted()
        if self._recovery is not None:
            # Recovery must settle before the first routed message reaches the loop.
            await self._recovery.scan()
        self._run_task = asyncio.create_task(
            self.agent.run(), name=f"nanobot-user-agent-{self.user_id}",
        )
        if self.cron is not None:
            try:
                await self.cron.start()
            except RuntimeError as exc:
                # A corrupt schedule must not lock the account out of chat.
                logger.error("Cron of user {} is not running: {}", self.user_id, exc)

    def attach_outbound(self, sink: OutboundSink) -> None:
        """Forward this stack's outbound queue to ``sink``; later calls keep the first sink."""
        if self._outbound_task is not None:
            return
        self._sink = sink
        self._outbound_task = asyncio.create_task(
            self._forward_outbound(sink), name=f"nanobot-user-outbound-{self.user_id}",
        )

    def _stamped(self, msg: OutboundMessage) -> OutboundMessage:
        # The runtime is the trust anchor for ownership: overwrite whatever the message carried.
        return replace(msg, metadata={**msg.metadata, OUTBOUND_META_ACCOUNT: self.user_id})

    async def _forward_outbound(self, sink: OutboundSink) -> None:
        while True:
            await sink(self._stamped(await self.bus.consume_outbound()))

    def busy(self) -> bool:
        """Whether the stack holds queued, running or undelivered work."""
        return self.agent.has_pending_work() or self.bus.outbound_size > 0

    async def close(self) -> None:
        """Stop the agent, keeping in-flight checkpoints, and deliver what it already produced."""
        try:
            if self.cron is not None:
                self.cron.stop()
            self.agent.preserve_inflight_turns_on_shutdown()
            self.agent.stop()
            if self._run_task is not None:
                self._run_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self._run_task, timeout=_CLOSE_TIMEOUT_S)
            try:
                await asyncio.wait_for(self.agent.aclose(), timeout=_CLOSE_TIMEOUT_S)
            except BaseException as exc:  # noqa: BLE001 - closing must proceed
                logger.warning("User runtime {} cleanup incomplete: {}", self.user_id, exc)
            await self.bus.drain()
        finally:
            self._connections.close()
            for hook in self.close_hooks:
                try:
                    await hook()
                except Exception as exc:  # noqa: BLE001 - closing must proceed
                    logger.warning("User runtime {} close hook failed: {}", self.user_id, exc)
            if self._outbound_task is not None:
                # Forwarding only suspends while waiting for the queue, so cancelling loses nothing.
                self._outbound_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._outbound_task
            if self._sink is not None:
                while self.bus.outbound_size:
                    await self._sink(self._stamped(self.bus.outbound.get_nowait()))


UserStackFactory = Callable[[str], Awaitable[UserStack]]


class UserStackPool:
    """Open account runtimes on demand and close idle ones.

    At most one stack exists per account. ``max_active`` and ``idle_ttl_s`` bound memory,
    but automatic eviction never closes a busy stack, so the pool may exceed
    ``max_active`` while every stack is working.
    """

    def __init__(
        self,
        factory: UserStackFactory,
        max_active: int,
        idle_ttl_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self.max_active = max_active
        self.idle_ttl_s = idle_ttl_s
        self._clock = clock
        self._stacks: OrderedDict[str, UserStack] = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._opening: dict[str, asyncio.Future[UserStack]] = {}
        self._closing: dict[str, asyncio.Future[None]] = {}
        self._sweeps: set[asyncio.Task[list[str]]] = set()
        self._closed = False

    def active(self) -> tuple[UserStack, ...]:
        return tuple(self._stacks.values())

    def peek(self, user_id: str) -> UserStack | None:
        """Return the account's open stack without opening it or refreshing its idle time."""
        return self._stacks.get(validate_user_id(user_id))

    async def get(self, user_id: str) -> UserStack:
        """Return the account's stack, opening it once even under concurrent callers.

        The stack is only ever returned from the lookup below, with no suspension point
        after it, so a caller that hands work to the stack before awaiting anything cannot
        race a concurrent eviction.
        """
        uid = validate_user_id(user_id)
        while True:
            if self._closed:
                raise RuntimeError("user runtime pool is closed")
            closing = self._closing.get(uid)
            if closing is not None:
                # Never let two runtimes own one account's workspace and session store.
                await asyncio.shield(closing)
                continue
            stack = self._stacks.get(uid)
            if stack is not None:
                self._touch(uid)
                return stack
            opening = self._opening.get(uid)
            if opening is None:
                opening = asyncio.ensure_future(self._open(uid))
                self._opening[uid] = opening
            # Other tasks run before this resumes and may already have evicted the new stack.
            await asyncio.shield(opening)

    async def _open(self, uid: str) -> UserStack:
        try:
            stack = await self._factory(uid)
            try:
                await stack.start()
            except BaseException:
                await stack.close()
                raise
            self._stacks[uid] = stack
            self._touch(uid)
        finally:
            self._opening.pop(uid, None)
        # Closing other stacks can take seconds; never hold the opener behind it.
        sweep = asyncio.create_task(self.sweep(keep=uid), name="nanobot-user-pool-sweep")
        self._sweeps.add(sweep)
        sweep.add_done_callback(self._sweeps.discard)
        return stack

    def _touch(self, uid: str) -> None:
        self._stacks.move_to_end(uid)
        self._last_used[uid] = self._clock()

    async def evict(self, user_id: str) -> None:
        """Close the account's stack now, whether or not it is busy."""
        uid = validate_user_id(user_id)
        stack = self._stacks.pop(uid, None)
        self._last_used.pop(uid, None)
        if stack is not None:
            await self._close(uid, stack)
        elif (closing := self._closing.get(uid)) is not None:
            await asyncio.shield(closing)

    async def _close(self, uid: str, stack: UserStack) -> None:
        closing = asyncio.ensure_future(stack.close())
        self._closing[uid] = closing
        try:
            await asyncio.shield(closing)
        except Exception:
            logger.exception("Closing the runtime of user {} failed", uid)
        finally:
            self._closing.pop(uid, None)

    async def sweep(self, *, keep: str | None = None) -> list[str]:
        """Close idle-expired stacks, then least recently used ones over capacity; skip busy ones."""
        now = self._clock()
        victims = [
            uid for uid, stack in self._stacks.items()
            if uid != keep and now - self._last_used[uid] >= self.idle_ttl_s and not stack.busy()
        ]
        excess = len(self._stacks) - len(victims) - self.max_active
        for uid, stack in self._stacks.items():
            if excess <= 0:
                break
            if uid != keep and uid not in victims and not stack.busy():
                victims.append(uid)
                excess -= 1
        for uid in victims:
            await self.evict(uid)
        return victims

    async def close_all(self) -> None:
        self._closed = True
        await asyncio.gather(*self._sweeps, return_exceptions=True)
        for opening in tuple(self._opening.values()):
            with suppress(BaseException):
                await asyncio.shield(opening)
        for uid in tuple(self._stacks):
            await self.evict(uid)
        for closing in tuple(self._closing.values()):
            with suppress(BaseException):
                await asyncio.shield(closing)

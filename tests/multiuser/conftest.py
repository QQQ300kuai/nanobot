import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.multiuser.allowlist import UserAllowlist
from nanobot.multiuser.lifecycle import UserLifecycle
from nanobot.multiuser.router import UserRouter
from nanobot.multiuser.stack import UserStack, UserStackPool
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.factory import ProviderSnapshot


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


class ScriptedProvider(LLMProvider):
    """``remember <word>`` writes memory/MEMORY.md through the file tool; ``block`` waits for ``release``."""

    def __init__(self) -> None:
        super().__init__(provider_name="scripted")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_default_model(self) -> str:
        return "test-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        return 10, "scripted"

    async def chat(self, messages, **kwargs):
        last = messages[-1]
        if last.get("role") == "tool":
            return LLMResponse(content="done")
        text = _text(last.get("content"))
        if "remember " in text:
            word = text.split("remember ", 1)[1].split()[0]
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="call-1", name="write_file",
                    arguments={"path": "memory/MEMORY.md", "content": word},
                )],
            )
        if "block" in text:
            self.entered.set()
            await self.release.wait()
        return LLMResponse(content="done")

    async def chat_stream_with_retry(self, **kwargs):
        return await self.chat(**kwargs)

    async def chat_with_retry(self, **kwargs):
        return await self.chat(**kwargs)


@dataclass
class MultiUserHarness:
    config: Config
    front: MessageBus
    pool: UserStackPool
    router: UserRouter
    provider: ScriptedProvider
    opened: list[str]
    disconnected: list[str] = field(default_factory=list)
    lifecycle: UserLifecycle | None = None
    unclaimed: list[OutboundMessage] = field(default_factory=list)

    def message(self, user_id: str | None, content: str, chat_id: str | None = None) -> InboundMessage:
        return InboundMessage(
            channel="websocket", sender_id="client", chat_id=chat_id or f"{user_id}-chat",
            content=content, user_id=user_id,
        )

    async def reply(self, chat_id: str, timeout: float = 10.0) -> OutboundMessage:
        """Return the next final text reply delivered to the front bus for ``chat_id``."""
        def _is_reply(msg: OutboundMessage) -> bool:
            return msg.chat_id == chat_id and msg.event is None and msg.content == "done"

        async def _next() -> OutboundMessage:
            for msg in self.unclaimed:
                if _is_reply(msg):
                    self.unclaimed.remove(msg)
                    return msg
            while True:
                msg = await self.front.consume_outbound()
                if _is_reply(msg):
                    return msg
                self.unclaimed.append(msg)
        return await asyncio.wait_for(_next(), timeout)

    async def idle(self, stack: UserStack, timeout: float = 10.0) -> None:
        async def _wait() -> None:
            while stack.busy():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_wait(), timeout)


@pytest.fixture
async def make_harness(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    from nanobot.cli.gateway_runtime import _build_user_stack

    started: list[tuple[MultiUserHarness, asyncio.Task[None]]] = []

    async def _make(
        *, max_active: int = 8, idle_ttl_s: float = 3600.0, allowlist: UserAllowlist | None = None,
    ) -> MultiUserHarness:
        config = Config()
        config.agents.defaults.workspace = str(tmp_path / "global-workspace")
        config.multi_user.enabled = True
        config.multi_user.workspaces_root = tmp_path / "workspaces"
        provider = ScriptedProvider()
        snapshot = ProviderSnapshot(
            provider=provider, model="test-model", context_window_tokens=128_000, signature=("test",),
        )
        front = MessageBus()
        opened: list[str] = []

        async def factory(user_id: str) -> UserStack:
            if allowlist is not None and not allowlist.contains(user_id):
                raise PermissionError(user_id)
            opened.append(user_id)
            return await _build_user_stack(
                config, user_id, front_bus=front,
                provider_snapshot=snapshot, provider_snapshot_loader=lambda *_a, **_k: snapshot,
            )

        pool = UserStackPool(factory, max_active=max_active, idle_ttl_s=idle_ttl_s)
        router = UserRouter(front, pool, is_member=allowlist.contains if allowlist else None)
        harness = MultiUserHarness(config, front, pool, router, provider, opened)

        async def disconnect(user_id: str) -> None:
            harness.disconnected.append(user_id)

        if allowlist is not None:
            harness.lifecycle = UserLifecycle(pool, config.multi_user.workspaces_root, disconnect=disconnect)
            harness.lifecycle.attach(allowlist)
        started.append((harness, asyncio.create_task(router.run())))
        return harness

    yield _make
    for harness, task in started:
        harness.provider.release.set()
        await harness.router.aclose()
        if harness.lifecycle is not None:
            await harness.lifecycle.aclose()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.pool.close_all()


@pytest.fixture
async def harness(make_harness) -> MultiUserHarness:
    return await make_harness()

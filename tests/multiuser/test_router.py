import asyncio

from loguru import logger

from nanobot.bus.runtime_events import UserInputAccepted
from nanobot.multiuser.stack import UserStack


async def test_messages_reach_only_their_account_runtime(harness) -> None:
    await harness.front.publish_inbound(harness.message("alice", "hello"))

    reply = await harness.reply("alice-chat")

    assert reply.channel == "websocket"
    assert [stack.user_id for stack in harness.pool.active()] == ["alice"]
    alice = await harness.pool.get("alice")
    assert await alice.sessions.state.read("websocket:alice-chat") is not None


async def test_message_without_account_is_dropped_and_logged(harness) -> None:
    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        await harness.front.publish_inbound(harness.message(None, "hello", chat_id="anon"))
        await harness.front.publish_inbound(harness.message("alice", "hello"))
        await harness.reply("alice-chat")
    finally:
        logger.remove(sink)

    assert harness.opened == ["alice"]
    assert any("without an account" in record and "anon" in record for record in records)


async def test_outbound_returns_to_front_bus_without_entering_other_runtime(harness) -> None:
    await harness.front.publish_inbound(harness.message("bob", "hello"))
    await harness.reply("bob-chat")
    bob = await harness.pool.get("bob")

    await harness.front.publish_inbound(harness.message("alice", "hello"))
    await harness.reply("alice-chat")
    await harness.idle(bob)

    assert bob.bus.inbound_size == 0
    assert bob.bus.outbound_size == 0
    assert [item["key"] for item in await bob.sessions.state.list_sessions()] == ["websocket:bob-chat"]


async def test_concurrent_get_opens_one_runtime(harness) -> None:
    stacks = await asyncio.gather(*(harness.pool.get("alice") for _ in range(5)))

    assert harness.opened == ["alice"]
    assert all(stack is stacks[0] for stack in stacks)


async def test_eviction_skips_runtime_with_inflight_turn(make_harness) -> None:
    harness = await make_harness(max_active=1, idle_ttl_s=0)
    await harness.front.publish_inbound(harness.message("alice", "block"))
    await asyncio.wait_for(harness.provider.entered.wait(), 10)
    alice = await harness.pool.get("alice")

    await harness.pool.get("bob")
    assert "alice" not in await harness.pool.sweep()

    assert alice in harness.pool.active()
    harness.provider.release.set()
    await harness.reply("alice-chat")
    await harness.idle(alice)
    assert "alice" in await harness.pool.sweep()
    assert alice not in harness.pool.active()


async def test_evicted_runtime_reopens_with_persisted_session(harness) -> None:
    await harness.front.publish_inbound(harness.message("alice", "hello"))
    await harness.reply("alice-chat")
    first = await harness.pool.get("alice")
    await harness.idle(first)

    await harness.pool.evict("alice")
    second: UserStack = await harness.pool.get("alice")

    assert second is not first
    session = await second.sessions.state.read("websocket:alice-chat")
    assert session is not None
    assert [m["content"] for m in session.messages if m["role"] == "assistant"] == ["done"]
    assert harness.opened == ["alice", "alice"]


def _forwarders() -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks()
            if task.get_name().startswith("nanobot-user-outbound-") and not task.done()]


async def test_capacity_one_keeps_new_runtime_while_another_closes_slowly(make_harness) -> None:
    harness = await make_harness(max_active=1)
    alice = await harness.pool.get("alice")
    closing = asyncio.Event()
    finish_close = asyncio.Event()
    close = alice.close

    async def slow_close() -> None:
        closing.set()
        await finish_close.wait()
        await close()

    alice.close = slow_close
    # Opening bob evicts idle alice, whose close stalls; carol opens meanwhile and must not evict bob.
    await harness.front.publish_inbound(harness.message("bob", "hello"))
    await asyncio.wait_for(closing.wait(), 10)
    await harness.front.publish_inbound(harness.message("carol", "hello"))
    await harness.reply("carol-chat")
    finish_close.set()

    await harness.reply("bob-chat")
    await harness.pool.close_all()
    assert _forwarders() == []


async def test_eviction_skips_runtime_dispatching_an_accepted_message(make_harness) -> None:
    harness = await make_harness(idle_ttl_s=0)
    alice = await harness.pool.get("alice")
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def hold(_event: UserInputAccepted) -> None:
        accepted.set()
        await release.wait()

    alice.bus.subscribe(hold, UserInputAccepted)
    await harness.front.publish_inbound(harness.message("alice", "hello"))
    await asyncio.wait_for(accepted.wait(), 10)

    assert "alice" not in await harness.pool.sweep()
    release.set()
    await harness.reply("alice-chat")

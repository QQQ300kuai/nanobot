import asyncio
from pathlib import Path

from nanobot.bus.events import OUTBOUND_META_ACCOUNT
from nanobot.cron.types import CronSchedule
from nanobot.multiuser.allowlist import UserAllowlist


def _allowlist(tmp_path: Path, *users: str) -> UserAllowlist:
    path = tmp_path / "users.txt"
    path.write_text("".join(f"{user}\n" for user in users), encoding="utf-8")
    return UserAllowlist(path)


def _rewrite(allowlist: UserAllowlist, *users: str) -> None:
    allowlist.path.write_text("".join(f"{user}\n" for user in users), encoding="utf-8")
    allowlist.reload()


def _clear_front(harness) -> None:
    harness.unclaimed.clear()
    while harness.front.outbound_size:
        harness.front.outbound.get_nowait()


async def test_removed_user_is_disconnected_cancelled_and_no_longer_routed(make_harness, tmp_path) -> None:
    allowlist = _allowlist(tmp_path, "alice", "bob")
    harness = await make_harness(allowlist=allowlist)
    await harness.front.publish_inbound(harness.message("alice", "block"))
    await asyncio.wait_for(harness.provider.entered.wait(), 10)
    alice = await harness.pool.get("alice")
    assert alice.agent.has_pending_work()

    _rewrite(allowlist, "bob")
    await harness.lifecycle.settled()

    assert harness.disconnected == ["alice"]
    assert harness.pool.peek("alice") is None
    assert not alice.agent.has_pending_work()

    # A message for the removed account is dropped; it never reaches (or reopens) a runtime.
    _clear_front(harness)
    harness.router.route(harness.message("alice", "hello"))
    await asyncio.sleep(0.2)
    assert harness.opened.count("alice") == 1
    assert harness.pool.peek("alice") is None
    assert harness.front.outbound_size == 0

    await harness.front.publish_inbound(harness.message("bob", "hello"))
    assert (await harness.reply("bob-chat")).metadata[OUTBOUND_META_ACCOUNT] == "bob"


async def test_cron_runs_on_the_owners_stack_and_follows_the_allowlist(make_harness, tmp_path) -> None:
    allowlist = _allowlist(tmp_path, "alice", "bob")
    harness = await make_harness(allowlist=allowlist)
    root = Path(harness.config.multi_user.workspaces_root)
    await harness.front.publish_inbound(harness.message("alice", "hello"))
    await harness.reply("alice-chat")
    alice = await harness.pool.get("alice")
    job = alice.cron.add_job(
        "tick", CronSchedule(kind="every", every_ms=100), "tick",
        session_key="websocket:alice-chat", origin_channel="websocket", origin_chat_id="alice-chat",
    )
    await harness.front.publish_inbound(harness.message("bob", "hello"))
    await harness.reply("bob-chat")
    bob = await harness.pool.get("bob")

    scheduled = await harness.reply("alice-chat")
    assert scheduled.metadata[OUTBOUND_META_ACCOUNT] == "alice"
    assert job.id in (root / "alice" / "cron" / "jobs.json").read_text(encoding="utf-8")
    assert job.id not in (root / "bob" / "cron" / "jobs.json").read_text(encoding="utf-8")
    assert {item["key"] for item in await bob.sessions.state.list_sessions()} == {"websocket:bob-chat"}
    assert not (Path(harness.config.workspace_path) / "cron").exists()

    _rewrite(allowlist, "bob")
    await harness.lifecycle.settled()
    _clear_front(harness)
    await asyncio.sleep(0.5)
    assert harness.front.outbound_size == 0
    assert job.id in (root / "alice" / "cron" / "jobs.json").read_text(encoding="utf-8")

    _rewrite(allowlist, "alice", "bob")
    await harness.lifecycle.settled()
    resumed = await harness.reply("alice-chat")
    assert resumed.metadata[OUTBOUND_META_ACCOUNT] == "alice"

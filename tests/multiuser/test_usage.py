"""Provider calls made for an account are recorded under that account, whoever triggered them."""

import time

import pytest

from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.llm_usage.context import current_llm_usage_user
from nanobot.llm_usage.models import LLMCallRecord
from nanobot.llm_usage.store import LLMUsageStore


@pytest.fixture
def usage(harness, tmp_path) -> LLMUsageStore:
    store = LLMUsageStore(tmp_path / "usage.sqlite3")
    provider = harness.provider
    scripted = provider.chat

    async def observed(*args, **kwargs):
        store.record(LLMCallRecord(
            started_at_ms=int(time.time() * 1000), duration_ms=1, provider="scripted", model="test-model",
            source="user", stream=True, finish_reason="stop", user_id=current_llm_usage_user(),
        ))
        return await scripted(*args, **kwargs)

    provider.chat = observed
    return store


async def test_turns_and_dream_are_recorded_under_the_owning_account(harness, usage) -> None:
    await harness.front.publish_inbound(harness.message("alice", "hello"))
    await harness.reply("alice-chat")
    await harness.front.publish_inbound(harness.message("bob", "hello"))
    await harness.reply("bob-chat")
    before = {row["user_id"]: row["requests"] for row in usage.summary_by_user(days=36500)}
    assert set(before) == {"alice", "bob"}

    alice = await harness.pool.get("alice")
    alice.agent.context.memory.append_history("alice likes tea")
    dream = CronJob(
        id="dream", name="dream", schedule=CronSchedule(kind="every", every_ms=3_600_000),
        payload=CronPayload(kind="system_event"),
    )
    assert alice.cron is not None and alice.cron.on_job is not None
    await alice.cron.on_job(dream)  # the cron timer and manual runs both go through this handler

    after = {row["user_id"]: row["requests"] for row in usage.summary_by_user(days=36500)}
    assert None not in after
    assert after["alice"] > before["alice"]
    assert after["bob"] == before["bob"]

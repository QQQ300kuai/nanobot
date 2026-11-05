from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.llm_usage.context import llm_usage_user
from nanobot.llm_usage.store import LLMUsageStore
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(provider_name="scripted")

    def get_default_model(self) -> str:
        return "m"

    async def chat(self, messages, **kwargs):
        return LLMResponse(content="ok", usage=LLMUsage.reported(input_tokens=10, output_tokens=5))


@pytest.mark.asyncio
async def test_two_users_aggregate_separately(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "usage.sqlite3")
    provider = _Provider()
    provider.set_llm_call_observer(store.record)

    for user_id, calls in (("alice", 2), ("bob", 1)):
        with llm_usage_user(user_id):
            for _ in range(calls):
                await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    alice = store.usage_payload(user_id="alice")
    bob = store.usage_payload(user_id="bob")
    assert (alice["requests_30d"], bob["requests_30d"]) == (2, 1)
    assert alice["total_tokens"] == 2 * bob["total_tokens"] > 0
    assert store.usage_payload()["requests_30d"] == 3
    summary = {row["user_id"]: row["requests"] for row in store.summary_by_user()}
    assert summary == {"alice": 2, "bob": 1}


def test_old_database_upgrades_and_keeps_unowned_rows(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    seeded = LLMUsageStore(path)
    seeded.record(_old_call())
    seeded.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX llm_calls_user_time_idx")
        db.execute("ALTER TABLE llm_calls DROP COLUMN user_id")
        db.execute("PRAGMA user_version = 1")

    store = LLMUsageStore(path)
    store.record(_old_call(user_id="alice"))

    users = {row["user_id"]: row["requests"] for row in store.summary_by_user(days=3650)}
    assert users == {None: 1, "alice": 1}
    assert store.usage_payload(user_id="alice", now=_NOW)["requests_30d"] == 1


_NOW = datetime(2026, 6, 3, 12, tzinfo=timezone.utc)


def _old_call(user_id: str | None = None):
    from nanobot.llm_usage.models import LLMCallRecord

    return LLMCallRecord(
        started_at_ms=int(_NOW.timestamp() * 1000), duration_ms=1, provider="p", model="m",
        source="user", stream=False, finish_reason="stop", user_id=user_id,
    )

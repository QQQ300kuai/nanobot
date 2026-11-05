"""Multi-user mode serves accounts over the websocket channel only."""

import pytest
from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config

from test_channel_manager_hot_reload import _HotChannel, _plugin, _stub_registry


def _config(*, multi_user: bool) -> Config:
    return Config.model_validate({
        "channels": {"websocket": {"enabled": False}, "hot": {"enabled": True}},
        "multi_user": {"enabled": multi_user},
    })


def test_multi_user_ignores_configured_channels_and_says_so(monkeypatch):
    _stub_registry(monkeypatch, _plugin(_HotChannel))
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        manager = ChannelManager(_config(multi_user=True), MessageBus())
    finally:
        logger.remove(sink)

    assert manager.channels == {}
    assert any("ignoring configured channels: hot" in m for m in messages)


@pytest.mark.asyncio
async def test_multi_user_refuses_to_enable_another_channel_at_runtime(monkeypatch):
    config = _config(multi_user=True)
    _stub_registry(monkeypatch, _plugin(_HotChannel))
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda: config)
    manager = ChannelManager(config, MessageBus())
    manager._started = True

    try:
        await manager.apply_channel_feature_action("enable", "hot")
    except Exception:
        pass

    assert "hot" not in manager.channels


def test_single_user_still_starts_configured_channels(monkeypatch):
    _stub_registry(monkeypatch, _plugin(_HotChannel))
    manager = ChannelManager(_config(multi_user=False), MessageBus())
    assert "hot" in manager.channels

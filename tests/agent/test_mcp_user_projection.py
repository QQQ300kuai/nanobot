"""Catalog MCP servers: per-account projection over shared connections, and the identity they receive."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import types as mcp_types

from nanobot.agent.tools import mcp as mcp_runtime
from nanobot.agent.tools.mcp import MCPProvider, MCPToolWrapper
from nanobot.multiuser.catalog import Catalog, CatalogEntry

_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f0005fe02fea7d6050c0000000049454e44ae426082"
    )
).decode()


class _Session:
    def __init__(self, image: bool) -> None:
        self.image = image
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any):
        self.calls.append((name, kwargs))
        block = (
            mcp_types.ImageContent(type="image", data=_PNG, mimeType="image/png")
            if self.image
            else mcp_types.TextContent(type="text", text="ok")
        )
        return mcp_types.CallToolResult(content=[block])


class _Connection:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, _Session]]:
    """Replace the transport: record each connection's final config and expose its fake session."""
    made: list[tuple[str, Any, _Session]] = []

    async def fake_connect(servers, registry, **_kwargs):
        out = {}
        for name, cfg in servers.items():
            session = _Session(image=name == "pics")
            tool = mcp_types.Tool(name="ping", inputSchema={"type": "object", "properties": {}})
            registry.register(MCPToolWrapper(session, name, tool))
            made.append((name, cfg, session))
            out[name] = _Connection()
        return out

    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", fake_connect)
    return made


def _catalog(*entries: dict[str, Any]) -> Catalog:
    return Catalog([CatalogEntry.model_validate(e) for e in entries])


def _provider(catalog: Catalog) -> MCPProvider:
    return MCPProvider({}, mcp_runtime.ToolRegistry(), catalog=catalog)


@pytest.mark.asyncio
async def test_registry_holds_only_the_tools_the_user_enabled(connects, tmp_path: Path) -> None:
    provider = _provider(_catalog(
        {"name": "notes", "config": {"command": "notes-mcp"}},
        {"name": "wiki", "config": {"command": "wiki-mcp"}},
    ))

    alice = await provider.registry_for("alice", {"notes"}, {}, media_dir=tmp_path / "a")
    bob = await provider.registry_for("bob", {"wiki", "invented"}, {}, media_dir=tmp_path / "b")

    assert alice.tool_names == ["mcp_notes_ping"]
    assert bob.tool_names == ["mcp_wiki_ping"]
    # The shared stdio server was connected once per entry, not per user.
    assert sorted(name for name, _, _ in connects) == ["notes", "wiki"]


@pytest.mark.asyncio
async def test_shared_stdio_receives_the_caller_in_request_meta(connects, tmp_path: Path) -> None:
    provider = _provider(_catalog({"name": "notes", "config": {"command": "notes-mcp"}}))
    alice = await provider.registry_for("alice", {"notes"}, {}, media_dir=tmp_path)
    bob = await provider.registry_for("bob", {"notes"}, {}, media_dir=tmp_path)

    await alice.get("mcp_notes_ping").execute()
    await bob.get("mcp_notes_ping").execute()

    session = connects[0][2]
    assert [meta for _, meta in session.calls] == [{"meta": {"user_id": "alice"}}, {"meta": {"user_id": "bob"}}]


@pytest.mark.asyncio
async def test_remote_and_personal_servers_get_their_own_instance_with_identity(connects, tmp_path: Path) -> None:
    provider = _provider(_catalog(
        {"name": "remote", "config": {"url": "https://mcp.example.com/mcp"}},
        {
            "name": "personal",
            "config": {"command": "p-mcp", "env": {"TOKEN": "{{token}}"}},
            "paramSchema": {"token": {"secret": True, "required": True}},
        },
    ))

    await provider.registry_for("alice", {"remote", "personal"}, {"personal": {"token": "t-alice"}}, media_dir=tmp_path)
    await provider.registry_for("bob", {"remote", "personal"}, {"personal": {"token": "t-bob"}}, media_dir=tmp_path)

    configs = {(name, user): cfg for (name, cfg, _), user in zip(connects, ["alice", "alice", "bob", "bob"])}
    assert configs[("remote", "alice")].headers["X-Nanobot-User"] == "alice"
    assert configs[("remote", "bob")].headers["X-Nanobot-User"] == "bob"
    assert configs[("personal", "alice")].env == {"TOKEN": "t-alice", "NANOBOT_USER_ID": "alice"}
    assert configs[("personal", "bob")].env == {"TOKEN": "t-bob", "NANOBOT_USER_ID": "bob"}


@pytest.mark.asyncio
async def test_entry_missing_a_required_parameter_is_not_connected(connects, tmp_path: Path) -> None:
    provider = _provider(_catalog({
        "name": "personal",
        "config": {"command": "p-mcp", "env": {"TOKEN": "{{token}}"}},
        "paramSchema": {"token": {"secret": True, "required": True}},
    }))

    registry = await provider.registry_for("alice", {"personal"}, {}, media_dir=tmp_path)

    assert registry.tool_names == [] and connects == []


@pytest.mark.asyncio
async def test_mcp_image_is_written_to_the_calling_accounts_media_dir(connects, tmp_path: Path) -> None:
    provider = _provider(_catalog({"name": "pics", "config": {"command": "pics-mcp"}}))
    alice_media = tmp_path / "alice" / "media"
    registry = await provider.registry_for("alice", {"pics"}, {}, media_dir=alice_media)

    result = await registry.get("mcp_pics_ping").execute()

    path = Path(json.loads(result)["artifacts"][0]["path"])
    assert path.is_file() and path.resolve().is_relative_to(alice_media.resolve())


@pytest.mark.asyncio
async def test_plugin_dropped_into_a_workspace_creates_no_connection(connects, tmp_path: Path) -> None:
    plugin = tmp_path / "workspace" / "plugins" / "evil"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "evil"}))
    (plugin / "mcp.json").write_text(json.dumps({"mcpServers": {"evil": {"command": "rm"}}}))
    provider = _provider(_catalog({"name": "notes", "config": {"command": "notes-mcp"}}))

    registry = await provider.registry_for("alice", {"evil"}, {}, media_dir=tmp_path / "media")

    assert registry.tool_names == [] and connects == []

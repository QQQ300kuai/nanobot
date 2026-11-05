"""Per-user view of the administrator's MCP/plugin catalog, applied to an account's tool registry."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nanobot.agent.tools.mcp import MCPProvider, replace_mcp_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.multiuser.catalog import Catalog
from nanobot.multiuser.identity import UserPaths
from nanobot.multiuser.user_settings import UserSettings


class UserMCPAccess:
    """What an account may do with the catalog: switch entries and fill in its own parameters."""

    def __init__(
        self,
        catalog: Catalog,
        provider: MCPProvider,
        fernet_key: bytes,
        workspaces_root: Path,
        open_tools: Callable[[str], ToolRegistry | None],
    ) -> None:
        self._catalog = catalog
        self._provider = provider
        self._key = fernet_key
        self._root = Path(workspaces_root)
        self._open_tools = open_tools

    def _settings(self, user_id: str) -> UserSettings:
        return UserSettings(UserPaths(self._root, user_id), self._key, self._catalog)

    def view(self, user_id: str) -> list[dict[str, Any]]:
        return self._settings(user_id).view()

    async def apply(self, user_id: str, tools: ToolRegistry) -> None:
        """Make the MCP tools in ``tools`` match what the account has enabled right now."""
        settings = self._settings(user_id)
        enabled = settings.enabled()
        projected = await self._provider.registry_for(
            user_id, enabled, {name: settings.get_params(name) for name in enabled},
            media_dir=UserPaths(self._root, user_id).media_dir,
        )
        replace_mcp_tools(tools, projected)

    async def _refresh(self, user_id: str) -> None:
        tools = self._open_tools(user_id)
        if tools is not None:
            await self.apply(user_id, tools)

    async def set_enabled(self, user_id: str, name: str, on: bool) -> None:
        self._settings(user_id).set_enabled(name, on)
        await self._refresh(user_id)

    async def set_params(self, user_id: str, name: str, params: dict[str, Any]) -> None:
        self._settings(user_id).set_params(name, params)
        await self._refresh(user_id)

    async def release(self, user_id: str) -> None:
        await self._provider.release_user(user_id)

"""Session-bound WebUI services, resolved per account in multi-user mode.

The single-user gateway has exactly one tenant built over the shared session store. In
multi-user mode every authenticated account gets a tenant over its own runtime's session
store, workspace and media directory, and the front layer never holds conversation data.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from nanobot.session.webui_turns import WebsocketTurnScope, websocket_turn_scope
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.session_identity import webui_session_key
from nanobot.webui.session_projection import WebUISessionProjection
from nanobot.webui.sidebar_state import WebUISidebarState
from nanobot.webui.temporary_chats import WebUITemporaryChats
from nanobot.webui.transcript import WebUITranscriptRecorder
from nanobot.webui.workspaces import WebUIWorkspaceController

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.cron.service import CronService
    from nanobot.multiuser.mcp_access import UserMCPAccess
    from nanobot.session.manager import SessionManager

# Chat ids issued to one account in this process (new chats, temporary chats, connection
# defaults) before they have a stored session. Older entries fall back to the store check.
_MAX_ISSUED_CHATS_PER_ACCOUNT = 4096


@dataclass(frozen=True)
class WebUIAccountDirs:
    """Where one account's files live; known without opening its runtime."""

    workspace: Path
    media_dir: Path


@dataclass(frozen=True)
class WebUIAccountRuntime:
    """The parts of one account's open runtime the WebUI layer works on."""

    user_id: str
    sessions: SessionManager
    workspace: Path
    media_dir: Path
    # Account-private WebUI state outside the agent's workspace (sidebar preferences).
    state_dir: Path
    # The account runtime's own turn cancellation and subagents (deleting a session stops its work).
    subagent_manager: SubagentManager | None = None
    discard_session: Callable[[str], Awaitable[None]] | None = None
    cron_service: CronService | None = None


class WebUIAccountRuntimes(Protocol):
    """Composition-root access to account runtimes (multi-user mode only)."""

    async def open(self, user_id: str) -> WebUIAccountRuntime:
        """Return the account's runtime, opening it when needed."""
        ...

    def peek(self, user_id: str) -> WebUIAccountRuntime | None:
        """Return the account's runtime only if it is already open."""
        ...

    def dirs(self, user_id: str) -> WebUIAccountDirs:
        """Return the account's directories without opening its runtime."""
        ...


@dataclass(frozen=True)
class WebUITenant:
    """WebUI services bound to one session store: one account's, or the single-user gateway's."""

    user_id: str | None
    session_manager: SessionManager | None
    workspaces: WebUIWorkspaceController
    transcripts: WebUITranscriptRecorder
    temporary_chats: WebUITemporaryChats
    session_projection: WebUISessionProjection
    sidebar_state: WebUISidebarState
    media: WebUIMediaGateway
    media_dir: Path | None = None
    subagent_manager: SubagentManager | None = None
    discard_session: Callable[[str], Awaitable[None]] | None = None
    cron_service: CronService | None = None

    @property
    def turn_scope(self) -> WebsocketTurnScope:
        """Keyword arguments selecting this tenant's table in ``session.webui_turns``."""
        return websocket_turn_scope(self.user_id)


class WebUITenantError(RuntimeError):
    """An account's WebUI services could not be resolved."""


class WebUITenants:
    """Resolve the tenant for a connection, request or outbound message.

    ``runtimes`` is ``None`` in single-user mode, where every caller gets the shared tenant.
    In multi-user mode a missing account is an error, never a fallback to the shared tenant.
    """

    def __init__(
        self,
        shared: WebUITenant,
        *,
        runtimes: WebUIAccountRuntimes | None = None,
        build: Callable[[WebUIAccountRuntime], WebUITenant] | None = None,
    ) -> None:
        if (runtimes is None) != (build is None):
            raise ValueError("account runtimes and the tenant builder go together")
        self.shared = shared
        self._runtimes = runtimes
        self._build = build
        self._tenants: dict[str, WebUITenant] = {}
        self._issued: dict[str, OrderedDict[str, None]] = {}

    @property
    def multi_user(self) -> bool:
        return self._runtimes is not None

    @property
    def user_mcp(self) -> UserMCPAccess | None:
        """The caller-facing MCP catalog access (multi-user mode)."""
        return getattr(self._runtimes, "user_mcp", None)

    def _bind(self, runtime: WebUIAccountRuntime) -> WebUITenant:
        assert self._build is not None
        tenant = self._tenants.get(runtime.user_id)
        # A runtime reopened after eviction owns a new session store; never keep using the old one.
        if tenant is None or tenant.session_manager is not runtime.sessions:
            if tenant is not None:
                tenant.temporary_chats.close()
            tenant = self._tenants[runtime.user_id] = self._build(runtime)
        return tenant

    async def resolve(self, user_id: str | None) -> WebUITenant:
        """Return the caller's tenant, opening the account runtime when needed."""
        if self._runtimes is None:
            return self.shared
        if user_id is None:
            raise WebUITenantError("multi-user WebUI access requires an account")
        try:
            runtime = await self._runtimes.open(user_id)
        except Exception as exc:
            raise WebUITenantError(f"runtime for account {user_id} is unavailable") from exc
        return self._bind(runtime)

    def current(self, user_id: str | None) -> WebUITenant | None:
        """Return the tenant without opening a runtime (outbound delivery and cleanup)."""
        if self._runtimes is None:
            return self.shared
        if user_id is None:
            return None
        runtime = self._runtimes.peek(user_id)
        if runtime is not None:
            return self._bind(runtime)
        # A closing runtime still flushes its last messages to the connections.
        return self._tenants.get(user_id)

    def close(self) -> None:
        """Release process-local Temporary Chat state of every tenant at gateway shutdown."""
        for tenant in (self.shared, *self._tenants.values()):
            tenant.temporary_chats.close()

    def issue(self, tenant: WebUITenant, chat_id: str) -> None:
        """Record a server-generated chat id as owned by the tenant's account."""
        if tenant.user_id is None:
            return
        issued = self._issued.setdefault(tenant.user_id, OrderedDict())
        issued[chat_id] = None
        while len(issued) > _MAX_ISSUED_CHATS_PER_ACCOUNT:
            issued.popitem(last=False)

    async def owns(self, tenant: WebUITenant, chat_id: str) -> bool:
        """Whether the chat belongs to the tenant's account.

        An account owns the chats issued to it in this process and the chats stored in its own
        session store. Everything else, including another account's chat id, is not found.
        """
        if tenant.user_id is None:
            return True
        if chat_id in self._issued.get(tenant.user_id, ()):
            return True
        sessions = tenant.session_manager
        return sessions is not None and (
            await sessions.state.read_metadata(webui_session_key(chat_id)) is not None
        )

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger as default_logger

from nanobot.channels.websocket.attachment_http import AttachmentHTTP
from nanobot.channels.websocket.attachment_store import AttachmentStore
from nanobot.config.loader import get_config_path
from nanobot.config.paths import get_media_dir
from nanobot.webui.gateway_endpoint import WebUIGatewayEndpoint
from nanobot.webui.gateway_tokens import GatewayTokenStore
from nanobot.webui.ingress_policy import DEFAULT_WEBUI_INGRESS_POLICY, WebUIIngressPolicy
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.session_projection import WebUISessionProjection
from nanobot.webui.settings_services import WebUISettingsServices
from nanobot.webui.sidebar_state import WebUISidebarState
from nanobot.webui.temporary_chats import WebUITemporaryChats
from nanobot.webui.tenants import (
    WebUIAccountRuntime,
    WebUIAccountRuntimes,
    WebUITenant,
    WebUITenants,
)
from nanobot.webui.transcript import WebUITranscriptRecorder
from nanobot.webui.workspaces import WebUIWorkspaceController, account_workspace_scope
from nanobot.webui.ws_http import GatewayHTTPHandler

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.websocket.runtime import WebSocketConfig
    from nanobot.cron.service import CronService
    from nanobot.multiuser.accounts import AccountStore
    from nanobot.security.workspace_access import WorkspaceScope
    from nanobot.session.manager import SessionManager
    from nanobot.triggers.local_store import LocalTriggerStore


@dataclass(frozen=True)
class GatewayServices:
    """Explicit dependencies shared by WebSocket transport and HTTP routes."""

    http: GatewayHTTPHandler
    endpoint: WebUIGatewayEndpoint
    settings: WebUISettingsServices
    tokens: GatewayTokenStore
    media: WebUIMediaGateway
    uploads: AttachmentHTTP
    ingress: WebUIIngressPolicy
    transcripts: WebUITranscriptRecorder
    workspaces: WebUIWorkspaceController
    temporary_chats: WebUITemporaryChats
    session_projection: WebUISessionProjection
    session_manager: SessionManager | None
    cron_service: CronService | None
    local_trigger_store: LocalTriggerStore | None
    cron_pending_job_ids: Callable[[str], set[str]] | None
    local_trigger_pending_ids: Callable[[str], set[str]] | None
    tenants: WebUITenants
    unified_session: bool = False
    accounts: AccountStore | None = None


def build_gateway_services(
    *,
    config: WebSocketConfig,
    bus: MessageBus,
    session_manager: SessionManager | None,
    static_dist_path: Path | None,
    workspace_path: Path,
    default_restrict_to_workspace: bool,
    unified_session: bool = False,
    config_path: Path | None = None,
    runtime_model_name: Callable[[], str | None] | None,
    refresh_runtime_config: Callable[[], None] | None = None,
    runtime_surface: str,
    runtime_capabilities_overrides: dict[str, Any] | None,
    disabled_skills: set[str] | None = None,
    cron_service: CronService | None = None,
    local_trigger_store: LocalTriggerStore | None = None,
    cron_pending_job_ids: Callable[[str], set[str]] | None = None,
    local_trigger_pending_ids: Callable[[str], set[str]] | None = None,
    channel_feature_action: Callable[..., Any] | None = None,
    channel_runtime_status: Callable[[], dict[str, Any]] | None = None,
    mcp_runtime_status: Callable[[], Mapping[str, str]] | None = None,
    mcp_reload: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    image_generation_reload: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    skill_state_action: Callable[[set[str]], None] | None = None,
    recovery_action: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    subagent_manager: SubagentManager | None = None,
    discard_session: Callable[[str], Awaitable[None]] | None = None,
    accounts: AccountStore | None = None,
    admins: Iterable[str] = (),
    account_runtimes: WebUIAccountRuntimes | None = None,
    logger: Any = default_logger,
) -> GatewayServices:
    if (accounts is None) != (account_runtimes is None):
        # Multi-user WebUI state must come from the account runtimes, never the shared store.
        raise ValueError("multi-user WebUI requires both an account store and account runtimes")
    settings = WebUISettingsServices.create(
        config_path or get_config_path(),
        rename_model_preset=(
            session_manager.rename_model_preset
            if session_manager is not None
            else None
        ),
        refresh_runtime_config=refresh_runtime_config,
    )
    tokens = GatewayTokenStore()
    ingress = DEFAULT_WEBUI_INGRESS_POLICY
    minimum_frame_bytes = ingress.minimum_full_policy_frame_bytes()
    if config.max_message_bytes < minimum_frame_bytes:
        logger.warning(
            "WebSocket maxMessageBytes={} is below the WebUI ingress policy capacity={}; "
            "policy-valid messages may still hit the transport frame guard",
            config.max_message_bytes,
            minimum_frame_bytes,
        )
    def account_media_roots(user_id: str) -> tuple[Path, WorkspaceScope] | None:
        # Serving never opens a runtime; the roots of an allowlisted account are fixed paths.
        if account_runtimes is None or accounts is None or not accounts.is_member(user_id):
            return None
        dirs = account_runtimes.dirs(user_id)
        return dirs.media_dir, account_workspace_scope(user_id, dirs.workspace, dirs.media_dir)

    media = WebUIMediaGateway(
        workspace_path=workspace_path,
        logger=logger,
        account_roots=account_media_roots if account_runtimes is not None else None,
    )
    uploads = AttachmentHTTP(AttachmentStore(
        get_media_dir("websocket"), limits=ingress.attachments,
    ))
    transcripts = WebUITranscriptRecorder(session_manager, log=logger)
    workspaces = WebUIWorkspaceController(
        session_manager=session_manager,
        default_workspace=workspace_path,
        default_restrict_to_workspace=default_restrict_to_workspace,
    )
    temporary_chats = WebUITemporaryChats(
        bus=bus,
        session_manager=session_manager,
        workspaces=workspaces,
        logger=logger,
    )
    session_projection = WebUISessionProjection(session_manager, subagent_manager=subagent_manager, log=logger)
    shared_tenant = WebUITenant(
        user_id=None,
        session_manager=session_manager,
        workspaces=workspaces,
        transcripts=transcripts,
        temporary_chats=temporary_chats,
        session_projection=session_projection,
        sidebar_state=WebUISidebarState(session_manager.state if session_manager is not None else None),
        media=media,
    )

    def account_tenant(runtime: WebUIAccountRuntime) -> WebUITenant:
        account_workspaces = WebUIWorkspaceController(
            session_manager=runtime.sessions,
            default_workspace=runtime.workspace,
            default_restrict_to_workspace=True,
            account=runtime.user_id,
            media_dir=runtime.media_dir,
        )
        return WebUITenant(
            user_id=runtime.user_id,
            session_manager=runtime.sessions,
            workspaces=account_workspaces,
            transcripts=WebUITranscriptRecorder(runtime.sessions, log=logger),
            temporary_chats=WebUITemporaryChats(
                bus=bus, session_manager=runtime.sessions, workspaces=account_workspaces, logger=logger,
            ),
            session_projection=WebUISessionProjection(
                runtime.sessions, subagent_manager=runtime.subagent_manager, log=logger, account=runtime.user_id,
            ),
            sidebar_state=WebUISidebarState(runtime.sessions.state, runtime.state_dir / "sidebar-state.json"),
            media=WebUIMediaGateway(
                workspace_path=runtime.workspace,
                logger=logger,
                media_dir=lambda channel: runtime.media_dir / channel if channel else runtime.media_dir,
                secret=media.secret,
                account=runtime.user_id,
                source_roots=(runtime.workspace, runtime.media_dir),
            ),
            media_dir=runtime.media_dir,
            subagent_manager=runtime.subagent_manager,
            discard_session=runtime.discard_session,
            cron_service=runtime.cron_service,
        )

    tenants = WebUITenants(
        shared_tenant,
        runtimes=account_runtimes,
        build=account_tenant if account_runtimes is not None else None,
    )
    http = GatewayHTTPHandler(
        config=config,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        runtime_model_name=runtime_model_name,
        runtime_surface=runtime_surface,
        runtime_capabilities_overrides=runtime_capabilities_overrides,
        bus=bus,
        tokens=tokens,
        media=media,
        ingress=ingress,
        workspaces=workspaces,
        settings=settings,
        skills_workspace_path=workspace_path,
        disabled_skills=disabled_skills,
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
        cron_pending_job_ids=cron_pending_job_ids,
        local_trigger_pending_ids=local_trigger_pending_ids,
        channel_feature_action=channel_feature_action,
        channel_runtime_status=channel_runtime_status,
        mcp_runtime_status=mcp_runtime_status,
        mcp_reload=mcp_reload,
        image_generation_reload=image_generation_reload,
        skill_state_action=skill_state_action,
        recovery_action=recovery_action,
        subagent_manager=subagent_manager,
        discard_session=discard_session,
        accounts=accounts,
        tenants=tenants,
        admins=admins,
        log=logger,
    )
    endpoint = WebUIGatewayEndpoint(
        config=config, http=http, tokens=tokens, accounts=accounts, admins=admins,
    )
    return GatewayServices(
        http=http,
        endpoint=endpoint,
        settings=settings,
        tokens=tokens,
        media=media,
        uploads=uploads,
        ingress=ingress,
        transcripts=transcripts,
        workspaces=workspaces,
        temporary_chats=temporary_chats,
        session_projection=session_projection,
        session_manager=session_manager,
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
        cron_pending_job_ids=cron_pending_job_ids,
        local_trigger_pending_ids=local_trigger_pending_ids,
        tenants=tenants,
        unified_session=unified_session,
        accounts=accounts,
    )

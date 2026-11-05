"""Multi-user mode: account login, password change and trusted WebSocket identity."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request as WsRequest

from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.multiuser.accounts import AccountStore, hash_password
from nanobot.multiuser.allowlist import UserAllowlist
from nanobot.security.workspace_access import WORKSPACE_SCOPE_METADATA_KEY
from nanobot.session.manager import SessionManager
from nanobot.webui.gateway_services import GatewayServices, build_gateway_services
from nanobot.webui.tenants import WebUIAccountDirs, WebUIAccountRuntime
from nanobot.webui.ws_http import request_user_id


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path)


@pytest.fixture
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.fixture
def accounts(tmp_path: Path) -> AccountStore:
    users = tmp_path / "users.txt"
    users.write_text("a\nb\n", encoding="utf-8")
    return AccountStore(tmp_path / "accounts.db", UserAllowlist(users), hash_password("initial-pw"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


_PROXY_HEADER = "X-Auth-User"


class _AccountRuntimes:
    """Account session stores under ``root/<user>/`` (the real gateway opens UserStacks)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._runtimes: dict[str, WebUIAccountRuntime] = {}

    async def open(self, user_id: str) -> WebUIAccountRuntime:
        runtime = self._runtimes.get(user_id)
        if runtime is None:
            base = self._root / user_id
            (base / "workspace").mkdir(parents=True)
            runtime = self._runtimes[user_id] = WebUIAccountRuntime(
                user_id=user_id, sessions=SessionManager(base / "workspace"),
                workspace=base / "workspace", media_dir=base / "media", state_dir=base / "webui",
            )
        return runtime

    def peek(self, user_id: str) -> WebUIAccountRuntime | None:
        return self._runtimes.get(user_id)

    def dirs(self, user_id: str) -> WebUIAccountDirs:
        base = self._root / user_id
        return WebUIAccountDirs(workspace=base / "workspace", media_dir=base / "media")


@pytest.fixture
async def gateway(
    bus: MagicMock, accounts: AccountStore, workspace: Path, tmp_path: Path,
) -> GatewayServices:
    cfg = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 0,
        "path": "/ws",
        "token": "shared-static-token",
        "tokenIssuePath": "/token",
        "trustedProxyAuth": {"trustedPeerCidrs": ["127.0.0.1/32"], "assertionHeader": _PROXY_HEADER},
    })
    return build_gateway_services(
        config=cfg,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=workspace,
        default_restrict_to_workspace=True,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        config_path=tmp_path / "config.json",
        accounts=accounts,
        admins=["B"],
        account_runtimes=_AccountRuntimes(tmp_path / "accounts"),
    )


@pytest.fixture
async def channel(bus: MagicMock, gateway: GatewayServices):
    channel = WebSocketChannel(gateway.http.config, bus, gateway=gateway)
    task = asyncio.create_task(channel.start())
    async with asyncio.timeout(5):
        while channel._server is None or not channel._server.sockets:
            assert not task.done()
            await asyncio.sleep(0.01)
    try:
        yield channel
    finally:
        await asyncio.wait_for(channel.stop(), timeout=5)
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def server(channel: WebSocketChannel) -> str:
    assert channel._server is not None
    return f"127.0.0.1:{channel._server.sockets[0].getsockname()[1]}"


async def _post(address: str, path: str, body: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(f"http://{address}{path}", json=body, headers=headers)


async def _login(address: str, user_id: str, password: str) -> httpx.Response:
    return await _post(address, "/auth/login", {"user_id": user_id, "password": password})


async def _ws_url(address: str, token: str) -> str:
    """The WebSocket URL for an account session token: a fresh one-shot ticket, never the token itself."""
    response = await _post(address, "/auth/ws-ticket", {}, token)
    assert response.status_code == 200
    return f"ws://{address}/ws?ticket={response.json()['ticket']}"


async def _handshake_status(address: str, token: str | None) -> int:
    """Open a socket for an account session token (``None`` and unknown tokens get no ticket)."""
    query = ""
    if token:
        ticket = await _post(address, "/auth/ws-ticket", {}, token)
        query = f"?ticket={ticket.json()['ticket']}" if ticket.status_code == 200 else "?ticket=none"
    try:
        async with websockets.connect(f"ws://{address}/ws{query}") as client:
            assert json.loads(await client.recv())["event"] == "ready"
            return 101
    except InvalidStatus as exc:
        return exc.response.status_code


@pytest.mark.asyncio
async def test_handshake_requires_an_account_token(server: str) -> None:
    assert await _handshake_status(server, None) == 401
    assert await _handshake_status(server, "shared-static-token") == 401
    assert await _handshake_status(server, "not-a-session-token") == 401


@pytest.mark.asyncio
async def test_a_ticket_connects_as_its_account_and_works_once(server: str) -> None:
    token = (await _login(server, "a", "initial-pw")).json()["token"]
    issued = await _post(server, "/auth/ws-ticket", {}, token)
    assert issued.status_code == 200
    assert issued.json()["expires_in"] == 30
    ticket = issued.json()["ticket"]
    assert (await _post(server, "/auth/ws-ticket", {})).status_code == 401
    assert (await _post(server, "/auth/ws-ticket", {}, "not-a-session-token")).status_code == 401

    async with websockets.connect(f"ws://{server}/ws?ticket={ticket}") as client:
        assert json.loads(await client.recv())["event"] == "ready"
    with pytest.raises(InvalidStatus) as reused:
        async with websockets.connect(f"ws://{server}/ws?ticket={ticket}"):
            pass
    assert reused.value.response.status_code == 401


@pytest.mark.asyncio
async def test_an_expired_ticket_is_rejected(server: str, gateway: GatewayServices) -> None:
    token = (await _login(server, "a", "initial-pw")).json()["token"]
    ticket = (await _post(server, "/auth/ws-ticket", {}, token)).json()["ticket"]
    gateway.tokens.ws_tickets[ticket] = (0.0, "a")
    with pytest.raises(InvalidStatus) as expired:
        async with websockets.connect(f"ws://{server}/ws?ticket={ticket}"):
            pass
    assert expired.value.response.status_code == 401


@pytest.mark.asyncio
async def test_the_account_token_is_not_accepted_in_the_url(server: str) -> None:
    token = (await _login(server, "a", "initial-pw")).json()["token"]
    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(f"ws://{server}/ws?token={token}"):
            pass
    assert refused.value.response.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_and_unknown_user(server: str) -> None:
    assert (await _login(server, "a", "wrong")).status_code == 401
    assert (await _login(server, "mallory", "initial-pw")).status_code == 401


@pytest.mark.asyncio
async def test_inbound_identity_comes_from_the_authenticated_connection(
    server: str, bus: MagicMock, workspace: Path, tmp_path: Path,
) -> None:
    response = await _login(server, "a", "initial-pw")
    assert response.status_code == 200
    token = response.json()["token"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async with websockets.connect(await _ws_url(server, token)) as client:
        chat_id = json.loads(await client.recv())["chat_id"]
        await client.send(json.dumps({
            "type": "message",
            "chat_id": chat_id,
            "content": "hello",
            "user_id": "b",
            "workspace_scope": {"project_path": str(elsewhere), "access_mode": "full"},
        }))
        await client.send("plain text frame")
        async with asyncio.timeout(5):
            while bus.publish_inbound.await_count < 2:
                await asyncio.sleep(0.01)

    envelope_msg, plain_msg = (call.args[0] for call in bus.publish_inbound.await_args_list)
    assert envelope_msg.user_id == "a"
    assert plain_msg.user_id == "a"
    # The turn scope is the account's own workspace, built by the server.
    account_workspace = tmp_path / "accounts" / "a" / "workspace"
    assert envelope_msg.metadata[WORKSPACE_SCOPE_METADATA_KEY]["project_path"] == str(account_workspace.resolve())


@pytest.mark.asyncio
async def test_password_change_rotates_the_session_token(server: str) -> None:
    old_token = (await _login(server, "a", "initial-pw")).json()["token"]

    rejected = await _post(server, "/auth/password", {"old": "wrong", "new": "new-pw"}, old_token)
    assert rejected.status_code == 403
    unauthenticated = await _post(server, "/auth/password", {"old": "initial-pw", "new": "new-pw"})
    assert unauthenticated.status_code == 401

    changed = await _post(server, "/auth/password", {"old": "initial-pw", "new": "new-pw"}, old_token)
    assert changed.status_code == 200
    new_token = changed.json()["token"]

    assert await _handshake_status(server, old_token) == 401
    assert await _handshake_status(server, new_token) == 101
    assert (await _login(server, "a", "initial-pw")).status_code == 401
    assert (await _login(server, "a", "new-pw")).status_code == 200


async def _webui_request(address: str, token: str, action: str) -> dict[str, Any]:
    async with websockets.connect(await _ws_url(address, token)) as client:
        assert json.loads(await client.recv())["event"] == "ready"
        await client.send(json.dumps({
            "type": "webui_request", "request_id": "r1", "action": action, "payload": {},
        }))
        async with asyncio.timeout(10):
            while True:
                event = json.loads(await client.recv())
                if event.get("event") == "webui_response":
                    return event


@pytest.mark.asyncio
async def test_trusted_webui_controls_are_limited_to_admins(
    server: str, gateway: GatewayServices,
) -> None:
    user_token = (await _login(server, "a", "initial-pw")).json()["token"]
    admin_token = (await _login(server, "b", "initial-pw")).json()["token"]

    refused = await _webui_request(server, user_token, "settings.mcp.custom")
    assert refused["ok"] is False
    assert refused["error"]["status"] == 403
    assert refused["error"]["message"] == "access_denied"

    admitted = await _webui_request(server, admin_token, "settings.mcp.custom")
    # Reached the settings handler itself (payload validation), past the trust and auth gates.
    assert admitted["error"] == {"status": 400, "message": "invalid MCP server name"}

    async with websockets.connect(await _ws_url(server, user_token)) as client:
        await client.recv()
        assert not gateway.endpoint.webui_connections


async def _get(address: str, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.get(f"http://{address}{path}", headers=headers or {})


@pytest.mark.asyncio
async def test_http_api_accepts_only_account_tokens(server: str, gateway: GatewayServices) -> None:
    token = (await _login(server, "a", "initial-pw")).json()["token"]
    assert (await _get(server, "/api/commands", {"Authorization": f"Bearer {token}"})).status_code == 200
    assert (await _get(server, "/api/commands")).status_code == 401
    legacy = gateway.tokens.issue_api_token(60)
    assert (await _get(server, "/api/commands", {"Authorization": f"Bearer {legacy}"})).status_code == 401

    request = WsRequest("/api/commands", Headers({"Authorization": f"Bearer {token}"}))
    assert gateway.http.check_api_token(request)
    assert request_user_id(request) == "a"


@pytest.mark.asyncio
async def test_trusted_proxy_assertion_is_not_an_identity(server: str) -> None:
    response = await _get(server, "/api/commands", {_PROXY_HEADER: "a"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_and_token_issue_hand_out_no_legacy_tokens(server: str) -> None:
    anonymous = await _get(server, "/webui/bootstrap")
    assert anonymous.status_code == 401
    assert anonymous.json()["auth"] == "account"
    shared = {"Authorization": "Bearer shared-static-token"}
    assert (await _get(server, "/webui/bootstrap", shared)).status_code == 401
    token = (await _login(server, "a", "initial-pw")).json()["token"]
    bootstrap = await _get(server, "/webui/bootstrap", {"Authorization": f"Bearer {token}"})
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    assert "token" not in body and "api_token" not in body
    assert body["ws_path"] == "/ws"
    issued = await _get(server, "/token", shared)
    assert issued.status_code == 404


@pytest.mark.asyncio
async def test_bootstrap_tells_the_client_who_it_is_and_hides_global_config_from_non_admins(server: str) -> None:
    async def bootstrap(user_id: str) -> dict[str, Any]:
        token = (await _login(server, user_id, "initial-pw")).json()["token"]
        return (await _get(server, "/webui/bootstrap", {"Authorization": f"Bearer {token}"})).json()

    member, admin = await bootstrap("a"), await bootstrap("b")
    assert (member["multi_user"], member["user_id"], member["is_admin"]) == (True, "a", False)
    assert "model_name" not in member
    assert (admin["multi_user"], admin["user_id"], admin["is_admin"]) == (True, "b", True)
    assert "model_name" in admin


@pytest.mark.asyncio
async def test_disconnect_user_closes_only_that_accounts_connections(
    server: str, channel: WebSocketChannel,
) -> None:
    tokens = {user: (await _login(server, user, "initial-pw")).json()["token"] for user in ("a", "b")}
    async with (
        websockets.connect(await _ws_url(server, tokens["a"])) as alice,
        websockets.connect(await _ws_url(server, tokens["b"])) as bob,
    ):
        for client in (alice, bob):
            assert json.loads(await client.recv())["event"] == "ready"

        await channel.disconnect_user("a")

        async with asyncio.timeout(5):
            await alice.wait_closed()
        assert alice.close_code == 1008
        await bob.ping()


@pytest.mark.asyncio
async def test_usage_routes_scope_to_the_caller_and_the_admin_view_is_admin_only(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from nanobot.llm_usage.models import LLMCallRecord
    from nanobot.llm_usage.service import get_llm_usage_store

    monkeypatch.setattr("nanobot.llm_usage.service.get_data_dir", lambda: tmp_path / "usage")
    store = get_llm_usage_store()
    for user_id in ("a", "a", "b"):
        store.record(LLMCallRecord(
            started_at_ms=int(time.time() * 1000), duration_ms=1, provider="p", model="m",
            source="user", stream=False, finish_reason="stop", user_id=user_id,
        ))
    user = {"Authorization": "Bearer " + (await _login(server, "a", "initial-pw")).json()["token"]}
    admin = {"Authorization": "Bearer " + (await _login(server, "b", "initial-pw")).json()["token"]}

    own = await _get(server, "/api/usage", user)
    assert own.status_code == 200 and own.json()["requests_30d"] == 2
    assert (await _get(server, "/api/usage")).status_code == 401
    assert (await _get(server, "/api/admin/usage", user)).status_code == 403

    overview = await _get(server, "/api/admin/usage", admin)
    assert overview.status_code == 200
    assert overview.json()["requests_30d"] == 3
    assert {row["user_id"]: row["requests"] for row in overview.json()["users_30d"]} == {"a": 2, "b": 1}

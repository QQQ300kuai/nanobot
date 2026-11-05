"""Multi-user WebUI isolation over real WebSocket connections and real account runtimes."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from cryptography.fernet import Fernet
from websockets.asyncio.client import ClientConnection

from nanobot.agent.tools.mcp import MCPProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.config.schema import Config
from nanobot.multiuser.accounts import AccountStore, hash_password
from nanobot.multiuser.allowlist import UserAllowlist
from nanobot.multiuser.catalog import Catalog, CatalogEntry
from nanobot.multiuser.mcp_access import UserMCPAccess
from nanobot.multiuser.router import UserRouter
from nanobot.multiuser.stack import UserStack, UserStackPool
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.factory import ProviderSnapshot
from nanobot.webui.gateway_services import build_gateway_services


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


class ReadingProvider(LLMProvider):
    """``read <path>`` calls read_file and replies with the tool result; anything else replies ``done``."""

    def __init__(self) -> None:
        super().__init__(provider_name="scripted")
        self.release = asyncio.Event()
        self.release.set()

    def get_default_model(self) -> str:
        return "test-model"

    def estimate_prompt_tokens(self, messages, tools, model):
        return 10, "scripted"

    async def chat(self, messages=None, **kwargs):
        messages = messages or kwargs.get("messages") or []
        last = messages[-1]
        if last.get("role") == "tool":
            return LLMResponse(content=f"tool said: {_text(last.get('content'))[:300]}")
        text = _text(last.get("content"))
        if "read " in text:
            path = text.split("read ", 1)[1].split()[0]
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="read_file", arguments={"path": path})],
            )
        if "wait" in text:
            await self.release.wait()
        return LLMResponse(content="done")

    async def chat_stream_with_retry(self, *args, **kwargs):
        return await self.chat(*args, **kwargs)

    async def chat_with_retry(self, *args, **kwargs):
        return await self.chat(*args, **kwargs)


@dataclass
class Deployment:
    address: str
    config: Config
    pool: UserStackPool
    provider: ReadingProvider
    workspaces_root: Path
    data_dir: Path


@pytest.fixture
async def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Deployment]:
    from nanobot.cli.gateway_runtime import WebUIAccountRuntimes, _build_user_stack

    monkeypatch.setattr("nanobot.config.paths.get_data_dir", lambda: tmp_path / "data")
    users = tmp_path / "users.txt"
    users.write_text("alice\nbob\ncarol\n", encoding="utf-8")
    accounts = AccountStore(tmp_path / "accounts.db", UserAllowlist(users), hash_password("pw"))

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "global-workspace")
    config.multi_user.enabled = True
    config.multi_user.workspaces_root = tmp_path / "workspaces"
    config.multi_user.admins = ["Alice", "Carol"]
    provider = ReadingProvider()
    snapshot = ProviderSnapshot(
        provider=provider, model="test-model", context_window_tokens=128_000, signature=("test",),
    )
    front = MessageBus()

    async def factory(user_id: str) -> UserStack:
        return await _build_user_stack(
            config, user_id, front_bus=front,
            provider_snapshot=snapshot, provider_snapshot_loader=lambda *_a, **_k: snapshot,
        )

    pool = UserStackPool(factory, max_active=8, idle_ttl_s=3600)
    router = UserRouter(front, pool)
    catalog = Catalog([CatalogEntry.model_validate({
        "name": "notes", "description": "Team notes", "config": {"command": "notes-mcp"},
        "paramSchema": {"token": {"secret": True}},
    })])
    user_mcp = UserMCPAccess(
        catalog, MCPProvider({}, ToolRegistry(), catalog=catalog), Fernet.generate_key(),
        tmp_path / "workspaces",
        lambda user_id: stack.agent.tools if (stack := pool.peek(user_id)) is not None else None,
    )
    ws_config = WebSocketConfig.model_validate({
        "enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 0, "path": "/ws",
        "token": "static-token",
    })
    global_workspace = Path(config.agents.defaults.workspace)
    global_workspace.mkdir(parents=True)
    gateway = build_gateway_services(
        config=ws_config, bus=front, session_manager=None, static_dist_path=None,
        workspace_path=global_workspace, default_restrict_to_workspace=False,
        runtime_model_name=None, runtime_surface="browser", runtime_capabilities_overrides=None,
        config_path=tmp_path / "config.json",
        accounts=accounts, admins=config.multi_user.admins,
        account_runtimes=WebUIAccountRuntimes(pool, config, user_mcp),
    )
    channel = WebSocketChannel(ws_config, front, gateway=gateway)

    async def deliver_outbound() -> None:
        while True:
            msg = await front.consume_outbound()
            await ChannelManager._send_once(channel, msg)  # pyright: ignore[reportPrivateUsage]

    tasks = [
        asyncio.create_task(channel.start()),
        asyncio.create_task(router.run()),
        asyncio.create_task(deliver_outbound()),
    ]
    async with asyncio.timeout(5):
        while channel._server is None or not channel._server.sockets:  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0.01)
    port = channel._server.sockets[0].getsockname()[1]  # pyright: ignore[reportPrivateUsage]
    try:
        yield Deployment(f"127.0.0.1:{port}", config, pool, provider, tmp_path / "workspaces", tmp_path / "data")
    finally:
        provider.release.set()
        await router.aclose()
        await asyncio.wait_for(channel.stop(), timeout=5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.close_all()


async def _login(address: str, user_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"http://{address}/auth/login", json={"user_id": user_id, "password": "pw"},
        )
    assert response.status_code == 200
    return response.json()["token"]


class Client:
    def __init__(self, ws: ClientConnection, ready: dict[str, Any], token: str) -> None:
        self.ws = ws
        self.ready = ready
        self.token = token
        self.frames: list[dict[str, Any]] = []

    async def send(self, **frame: Any) -> None:
        await self.ws.send(json.dumps(frame))

    async def next(self, predicate, timeout: float = 15.0) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                frame = json.loads(await self.ws.recv())
                self.frames.append(frame)
                if predicate(frame):
                    return frame

    async def new_chat(self) -> str:
        await self.send(type="new_chat")
        return (await self.next(lambda f: f.get("event") == "attached"))["chat_id"]

    async def turn(self, chat_id: str, content: str, **extra: Any) -> str:
        """Send one message and return the assistant text of the completed turn."""
        start = len(self.frames)
        await self.send(type="message", chat_id=chat_id, content=content, webui=True,
                        turn_id=f"t-{len(self.frames)}", **extra)
        await self.next(lambda f: f.get("event") == "turn_end" and f.get("chat_id") == chat_id)
        return "".join(
            str(f.get("text") or "") for f in self.frames[start:]
            if f.get("chat_id") == chat_id and f.get("event") in {"message", "delta"}
        )

    async def command(self, chat_id: str, content: str) -> str:
        """Send a slash command and return its reply (commands do not run an agent turn)."""
        await self.send(type="message", chat_id=chat_id, content=content, webui=True,
                        turn_id=f"t-{len(self.frames)}")
        reply = await self.next(lambda f: f.get("event") == "message" and f.get("chat_id") == chat_id)
        return str(reply.get("text") or "")

    async def drain(self, seconds: float = 0.5) -> None:
        try:
            async with asyncio.timeout(seconds):
                while True:
                    self.frames.append(json.loads(await self.ws.recv()))
        except TimeoutError:
            pass


@pytest.fixture
async def connect(deployment: Deployment):
    opened: list[Any] = []

    async def _connect(user_id: str) -> Client:
        token = await _login(deployment.address, user_id)
        async with httpx.AsyncClient(timeout=10) as http:
            issued = await http.post(
                f"http://{deployment.address}/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"},
            )
        ws = await websockets.connect(f"ws://{deployment.address}/ws?ticket={issued.json()['ticket']}")
        opened.append(ws)
        ready = json.loads(await ws.recv())
        assert ready["event"] == "ready"
        return Client(ws, ready, token)

    yield _connect
    for ws in opened:
        await ws.close()


async def _get(address: str, path: str, token: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.get(f"http://{address}{path}", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_each_account_chats_in_its_own_runtime(deployment: Deployment, connect) -> None:
    alice, bob = await connect("alice"), await connect("bob")
    alice_chat, bob_chat = await alice.new_chat(), await bob.new_chat()

    assert "done" in await alice.turn(alice_chat, "hello")
    assert "done" in await bob.turn(bob_chat, "hello")

    alice_sessions = (await deployment.pool.get("alice")).sessions
    bob_sessions = (await deployment.pool.get("bob")).sessions
    assert await alice_sessions.state.read_metadata(f"websocket:{alice_chat}") is not None
    assert await alice_sessions.state.read_metadata(f"websocket:{bob_chat}") is None
    assert await bob_sessions.state.read_metadata(f"websocket:{bob_chat}") is not None


@pytest.mark.asyncio
async def test_foreign_chat_cannot_be_attached_written_or_observed(deployment: Deployment, connect) -> None:
    alice, bob = await connect("alice"), await connect("bob")
    alice_chat = await alice.new_chat()
    assert "done" in await alice.turn(alice_chat, "hello")

    await bob.send(type="attach", chat_id=alice_chat)
    refused = await bob.next(lambda f: f.get("event") in {"error", "attached"})
    assert refused == {"event": "error", "detail": "chat_not_found", "chat_id": alice_chat}

    await bob.send(type="message", chat_id=alice_chat, content="intrude", webui=True, turn_id="x1")
    refused = await bob.next(lambda f: f.get("event") in {"error", "message_accepted"})
    assert refused["event"] == "error" and refused["detail"] == "chat_not_found"

    assert "done" in await alice.turn(alice_chat, "again")
    await bob.drain()
    assert not [f for f in bob.frames if f.get("chat_id") == alice_chat and f.get("event") != "error"]
    alice_sessions = (await deployment.pool.get("alice")).sessions
    history = json.dumps((await alice_sessions.state.read(f"websocket:{alice_chat}")).messages)
    assert "intrude" not in history


@pytest.mark.asyncio
async def test_uploads_land_in_the_account_media_dir_and_stay_private(
    deployment: Deployment, connect,
) -> None:
    alice, bob = await connect("alice"), await connect("bob")
    upload = alice.ready["upload"]
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"http://{deployment.address}{upload['path']}", content=b"alice secret 42",
            headers={"Authorization": f"Bearer {upload['token']}", "Content-Type": "text/plain",
                     "X-Attachment-Name": "note.txt"},
        )
    assert response.status_code == 201
    alice_chat = await alice.new_chat()
    await alice.turn(alice_chat, "see attached", media=[{"reference": response.json()["reference"]}])

    alice_media = deployment.workspaces_root / "alice" / "media"
    stored = [p for p in alice_media.rglob("*note.txt")]
    assert len(stored) == 1
    assert not list((deployment.workspaces_root.parent / "data").rglob("*note.txt"))
    accepted = next(f for f in alice.frames if f.get("event") == "message_accepted" and f.get("media_urls"))
    async with httpx.AsyncClient(timeout=10) as client:
        served = await client.get(f"http://{deployment.address}{accepted['media_urls'][0]['url']}")
    assert served.content == b"alice secret 42"

    assert "alice secret 42" in await alice.turn(alice_chat, f"read {stored[0]}")
    bob_chat = await bob.new_chat()
    bob_reply = await bob.turn(bob_chat, f"read {stored[0]}")
    assert "alice secret 42" not in bob_reply


@pytest.mark.asyncio
async def test_process_commands_are_admin_only(
    deployment: Deployment, connect, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarts: list[str] = []
    monkeypatch.setattr(
        "nanobot.command.builtin.set_restart_notice_to_env",
        lambda **kwargs: restarts.append(kwargs["chat_id"]),
    )

    async def _no_restart(_mode: str) -> None:
        return None

    monkeypatch.setattr("nanobot.command.builtin._restart_process", _no_restart)
    bob = await connect("bob")
    chat = await bob.new_chat()
    for command in ("/restart", "/pairing list"):
        reply = await bob.command(chat, command)
        assert "administrator" in reply.lower()
    assert restarts == []

    alice = await connect("alice")
    alice_chat = await alice.new_chat()
    assert "restarting" in (await alice.command(alice_chat, "/restart")).lower()
    assert restarts == [alice_chat]


@pytest.mark.asyncio
async def test_attachment_inspect_ignores_a_client_scope(deployment: Deployment, connect, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("outside", encoding="utf-8")
    alice = await connect("alice")
    alice_chat = await alice.new_chat()
    assert "done" in await alice.turn(alice_chat, "hello")
    own = deployment.workspaces_root / "alice" / "workspace" / "mine.txt"
    own.write_text("mine", encoding="utf-8")

    async def inspect(request_id: str, path: Path) -> dict[str, Any]:
        await alice.send(type="webui_request", request_id=request_id, action="attachment.inspect", payload={
            "chat_id": alice_chat, "path": str(path),
            "workspace_scope": {"project_path": str(elsewhere), "access_mode": "full"},
        })
        return await alice.next(lambda f: f.get("event") == "webui_response" and f["request_id"] == request_id)

    refused = await inspect("r1", elsewhere / "secret.txt")
    assert refused["ok"] is False
    admitted = await inspect("r2", own)
    assert admitted["ok"] is True
    assert admitted["result"]["path"] == str(own.resolve())


@pytest.mark.asyncio
async def test_http_reads_are_scoped_to_the_account(deployment: Deployment, connect) -> None:
    alice, bob = await connect("alice"), await connect("bob")
    alice_chat, bob_chat = await alice.new_chat(), await bob.new_chat()
    await alice.turn(alice_chat, "hello")
    await bob.turn(bob_chat, "hello")

    listed = await _get(deployment.address, "/api/sessions", bob.token)
    assert listed.status_code == 200
    keys = {item["key"] for item in listed.json()["sessions"]}
    assert f"websocket:{bob_chat}" in keys
    assert f"websocket:{alice_chat}" not in keys

    thread = await _get(deployment.address, f"/api/sessions/websocket:{alice_chat}/webui-thread", bob.token)
    assert thread.status_code == 404
    own_thread = await _get(deployment.address, f"/api/sessions/websocket:{alice_chat}/webui-thread", alice.token)
    assert own_thread.status_code == 200

    assert (await _get(deployment.address, "/api/settings", bob.token)).status_code == 403
    assert (await _get(deployment.address, "/api/settings", alice.token)).status_code == 200


async def test_deleting_a_chat_cancels_its_running_turn(deployment: Deployment, connect) -> None:
    alice = await connect("alice")
    chat = await alice.new_chat()
    deployment.provider.release.clear()
    await alice.send(type="message", chat_id=chat, content="wait", webui=True, turn_id="t-wait")
    async with asyncio.timeout(10):
        while not (stack := deployment.pool.peek("alice")) or not stack.agent.has_pending_work():
            await asyncio.sleep(0.01)

    await alice.send(type="webui_request", request_id="del", action="session.delete",
                     payload={"key": f"websocket:{chat}"})
    response = await alice.next(lambda f: f.get("event") == "webui_response")
    assert response["result"]["deleted"] is True
    async with asyncio.timeout(10):
        while stack.agent.has_pending_work():
            await asyncio.sleep(0.01)


async def _upload(deployment: Deployment, client: Client, body: bytes, name: str) -> str:
    upload = client.ready["upload"]
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.post(
            f"http://{deployment.address}{upload['path']}", content=body,
            headers={"Authorization": f"Bearer {upload['token']}", "Content-Type": "text/plain",
                     "X-Attachment-Name": name},
        )
    assert response.status_code == 201
    return response.json()["reference"]


@pytest.mark.asyncio
async def test_shared_media_dir_is_not_a_read_root_for_accounts(deployment: Deployment, connect) -> None:
    shared = deployment.data_dir / "media" / "websocket" / "operator.txt"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("operator upload", encoding="utf-8")
    own = deployment.workspaces_root / "bob" / "media" / "own.txt"
    bob = await connect("bob")
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("bob media", encoding="utf-8")
    chat = await bob.new_chat()
    assert "done" in await bob.turn(chat, "hello")

    await bob.send(type="message", chat_id=chat, content="look", webui=True, turn_id="m1",
                   media=[{"path": str(shared)}])
    refused = await bob.next(lambda f: f.get("event") in {"error", "message_accepted"})
    assert refused["event"] == "error" and refused["detail"] == "attachment_rejected"

    preview = f"/api/sessions/websocket:{chat}/file-preview?path="
    assert (await _get(deployment.address, preview + str(shared), bob.token)).status_code == 403
    own_preview = await _get(deployment.address, preview + str(own), bob.token)
    assert own_preview.status_code == 200 and "bob media" in own_preview.text

    await bob.send(type="message", chat_id=chat, content="mine", webui=True, turn_id="m2",
                   media=[{"path": str(own)}])
    accepted = await bob.next(lambda f: f.get("event") in {"error", "message_accepted"})
    assert accepted["event"] == "message_accepted"


@pytest.mark.asyncio
async def test_signed_media_names_its_account(deployment: Deployment, connect) -> None:
    import base64

    alice = await connect("alice")
    chat = await alice.new_chat()
    reference = await _upload(deployment, alice, b"alice picture", "pic.txt")
    await alice.turn(chat, "see attached", media=[{"reference": reference}])
    accepted = next(f for f in alice.frames if f.get("event") == "message_accepted" and f.get("media_urls"))
    url = accepted["media_urls"][0]["url"]

    # An idle-evicted account's media still resolves without reopening its runtime.
    await deployment.pool.evict("alice")
    async with httpx.AsyncClient(timeout=10) as client:
        served = await client.get(f"http://{deployment.address}{url}")
        assert served.content == b"alice picture"
        assert deployment.pool.peek("alice") is None

        _, _, sig, payload = url.split("/")[1:5] if url.startswith("/api/") else ("", "", "", "")
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        forged = raw.replace('"alice"', '"bob"')
        assert forged != raw
        forged_payload = base64.urlsafe_b64encode(forged.encode()).rstrip(b"=").decode()
        tampered = await client.get(f"http://{deployment.address}/api/media/{sig}/{forged_payload}")
    assert tampered.status_code == 401


@pytest.mark.asyncio
async def test_request_ids_do_not_share_results_across_accounts(deployment: Deployment, connect) -> None:
    alice, carol = await connect("alice"), await connect("carol")
    chat = await alice.new_chat()
    assert "done" in await alice.turn(chat, "hello")

    async def delete(client: Client) -> dict[str, Any]:
        await client.send(type="webui_request", request_id="same-id", action="session.delete",
                          payload={"key": f"websocket:{chat}"})
        return await client.next(lambda f: f.get("event") == "webui_response")

    assert (await delete(alice))["result"]["deleted"] is True
    # Carol's identical request runs against her own (empty) store instead of replaying alice's.
    assert (await delete(carol))["result"]["deleted"] is False


async def _request(client: Client, request_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    await client.send(type="webui_request", request_id=request_id, action=action, payload=payload)
    return await client.next(
        lambda f: f.get("event") == "webui_response" and f.get("request_id") == request_id,
    )


@pytest.mark.asyncio
async def test_non_admins_manage_their_own_chats_but_not_global_settings(deployment: Deployment, connect) -> None:
    alice, bob = await connect("alice"), await connect("bob")
    alice_chat, bob_chat = await alice.new_chat(), await bob.new_chat()
    assert "done" in await alice.turn(alice_chat, "hello")
    assert "done" in await bob.turn(bob_chat, "hello")

    renamed = await _request(bob, "rename", "sidebar.update", {"state": {"title_overrides": {f"websocket:{bob_chat}": "mine"}}})
    assert renamed["result"]["title_overrides"] == {f"websocket:{bob_chat}": "mine"}

    refused = await _request(bob, "settings", "settings.agent.update", {})
    assert refused["error"] == {"status": 403, "message": "access_denied"}

    foreign = await _request(bob, "foreign", "session.delete", {"key": f"websocket:{alice_chat}"})
    assert foreign["result"]["deleted"] is False
    listed = await _get(deployment.address, "/api/sessions", alice.token)
    assert f"websocket:{alice_chat}" in [row["key"] for row in listed.json()["sessions"]]

    own = await _request(bob, "own", "session.delete", {"key": f"websocket:{bob_chat}"})
    assert own["result"]["deleted"] is True


@pytest.mark.asyncio
async def test_sidebar_preferences_and_broadcasts_stay_with_the_account(deployment: Deployment, connect) -> None:
    alice, alice_tab, carol = await connect("alice"), await connect("alice"), await connect("carol")
    state = {"title_overrides": {"websocket:x": "alice private title"}}
    await alice.send(type="set_sidebar_state", state=state)

    updated = await alice_tab.next(lambda f: f.get("event") == "sidebar_state_updated")
    assert updated["state"]["title_overrides"] == state["title_overrides"]
    await carol.drain()
    assert not [f for f in carol.frames if f.get("event") == "sidebar_state_updated"]

    assert (deployment.workspaces_root / "alice" / "webui" / "sidebar-state.json").is_file()
    assert not (deployment.data_dir / "webui" / "sidebar-state.json").exists()
    carol_view = await _get(deployment.address, "/api/webui/sidebar-state", carol.token)
    assert carol_view.status_code == 200
    assert carol_view.json()["title_overrides"] == {}


async def _post(address: str, path: str, token: str, body: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(
            f"http://{address}{path}", json=body, headers={"Authorization": f"Bearer {token}"},
        )


async def test_users_switch_catalog_entries_and_fill_parameters_but_cannot_add_servers(
    deployment: Deployment, connect,
) -> None:
    bob, carol = await connect("bob"), await connect("carol")
    addr = deployment.address

    enable, params = "/api/mcp/catalog/enable", "/api/mcp/catalog/params"
    assert (await _post(addr, enable, bob.token, {"name": "notes", "enabled": True})).status_code == 200
    assert (await _post(addr, params, bob.token, {"name": "notes", "params": {"token": "tok-9f3a"}})).status_code == 200
    for path, body in (
        (enable, {"name": "my-server", "enabled": True}),
        (params, {"name": "my-server", "params": {"command": "sh"}}),
        (params, {"name": "notes", "params": {"url": "http://x"}}),
    ):
        assert (await _post(addr, path, bob.token, body)).status_code == 400
    assert (await _post(addr, enable, "bad-token", {"name": "notes", "enabled": True})).status_code == 401

    bob_view = (await _get(deployment.address, "/api/mcp/catalog", bob.token)).json()["entries"]
    carol_view = (await _get(deployment.address, "/api/mcp/catalog", carol.token)).json()["entries"]
    assert [(e["name"], e["enabled"], e["params"][0]["set"]) for e in bob_view] == [("notes", True, True)]
    assert [(e["name"], e["enabled"], e["params"][0]["set"]) for e in carol_view] == [("notes", False, False)]
    assert "tok-9f3a" not in json.dumps(bob_view)
    assert (await _get(deployment.address, "/api/settings/mcp-presets", bob.token)).status_code == 403


def test_account_media_gateway_stages_only_account_files(tmp_path: Path) -> None:
    from loguru import logger

    from nanobot.webui.media_gateway import WebUIMediaGateway

    workspace, media = tmp_path / "alice" / "workspace", tmp_path / "alice" / "media"
    workspace.mkdir(parents=True)
    media.mkdir()
    gateway = WebUIMediaGateway(
        workspace_path=workspace, logger=logger,
        media_dir=lambda channel: media / channel if channel else media,
        account="alice", source_roots=(workspace, media),
    )
    (workspace / "chart.png").write_bytes(b"png")
    foreign = tmp_path / "bob" / "workspace" / "secret.png"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"secret")

    assert gateway.sign_or_stage_media_path(workspace / "chart.png") is not None
    assert list((media / "websocket").glob("*chart.png"))
    assert gateway.sign_or_stage_media_path(foreign) is None
    assert not list((media / "websocket").glob("*secret.png"))


@pytest.mark.asyncio
async def test_account_transcription_stages_audio_in_the_account_media_dir(
    deployment: Deployment, connect, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from nanobot.config.loader import save_config

    config = Config()
    config.transcription.provider = "groq"
    config.providers.groq.api_key = "gsk-test"
    save_config(config, tmp_path / "config.json")
    staged: list[Path] = []

    async def fake_transcribe(path, _config) -> str:
        staged.append(Path(path))
        assert Path(path).is_file()
        return "hello there"

    monkeypatch.setattr("nanobot.audio.transcription.transcribe_audio_file", fake_transcribe)
    bob = await connect("bob")
    await bob.send(type="transcribe_audio", request_id="voice-1",
                   data_url="data:audio/webm;base64," + base64.b64encode(b"voice").decode())
    result = await bob.next(lambda f: f.get("event") in {"transcription_result", "transcription_error"})

    assert result == {"event": "transcription_result", "request_id": "voice-1", "text": "hello there"}
    assert staged[0].parent == deployment.workspaces_root / "bob" / "media" / "webui-transcription"


def test_account_scope_without_media_root_never_reads_the_shared_media_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from nanobot.security.workspace_access import build_workspace_scope
    from nanobot.webui.file_preview import WebUIFilePreviewError, resolve_file_path

    shared = tmp_path / "shared-media"
    shared.mkdir()
    (shared / "upload.txt").write_text("shared", encoding="utf-8")
    monkeypatch.setattr("nanobot.webui.file_preview.get_media_dir", lambda: shared)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    single_user = build_workspace_scope(workspace, "restricted")
    assert resolve_file_path(str(shared / "upload.txt"), scope=single_user) == (shared / "upload.txt").resolve()

    with pytest.raises(WebUIFilePreviewError) as refused:
        resolve_file_path(str(shared / "upload.txt"), scope=replace(single_user, account="alice"))
    assert refused.value.status == 403


@pytest.mark.asyncio
async def test_accounts_read_model_choices_without_provider_configuration(deployment: Deployment, connect, monkeypatch) -> None:
    from nanobot.agent.model_presets import configured_chat_model_presets
    from nanobot.config.loader import save_config
    from nanobot.config.schema import ModelPresetConfig

    config = deployment.config
    config.providers.custom.api_key = "private-provider-key"
    config.providers.custom.api_base = "https://private-provider.example/v1"
    config.model_presets["team-model"] = ModelPresetConfig(
        model="team-model-id", provider="custom", extra_headers={"X-Private": "private-header"},
    )
    config.model_presets["disabled-model"] = ModelPresetConfig(model="disabled-id", provider="custom")
    config.agents.defaults.model_preset = "team-model"
    save_config(config, deployment.data_dir.parent / "config.json")
    monkeypatch.setattr("nanobot.agent.model_presets.load_chat_model_preset_catalog", lambda: configured_chat_model_presets(config))
    bob = await connect("bob")

    response = await _get(deployment.address, "/api/chat-models", bob.token)
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["agent"]["model_preset"] == "team-model"
    assert any(row["name"] == "team-model" and row["model"] == "team-model-id" for row in catalog["model_presets"])
    assert {row["name"] for row in catalog["model_presets"]} == {"team-model"}
    assert "private-provider-key" not in response.text
    assert "private-provider.example" not in response.text
    assert "private-header" not in response.text
    assert (await _get(deployment.address, "/api/chat-models", "invalid-token")).status_code == 401
    assert (await _get(deployment.address, "/api/settings", bob.token)).status_code == 403
    refused = await _request(bob, "provider", "settings.provider.update", {"name": "custom", "api_key": "replacement"})
    assert refused["error"]["status"] == 403
    chat = await bob.new_chat()
    assert "Could not switch" in await bob.command(chat, "/model disabled-model")
    assert "current session" in await bob.command(chat, "/model team-model")
    assert "team-model" in await bob.command(chat, "/model")
    await bob.command(chat, "/model-follow")
    stack = deployment.pool.peek("bob")
    assert stack is not None
    header = await stack.sessions.state.get(f"websocket:{chat}")
    assert "_nanobot_model_preset" not in header.metadata
    assert "current session" in await bob.command(chat, "/model team-model")
    await bob.next(lambda frame: frame.get("event") == "turn_end" and frame.get("chat_id") == chat)

    del config.model_presets["team-model"]
    config.agents.defaults.model_preset = None
    save_config(config, deployment.data_dir.parent / "config.json")
    stack = deployment.pool.peek("bob")
    assert stack is not None
    stack.agent.refresh_runtime_config()
    updated = await _get(deployment.address, "/api/chat-models", bob.token)
    assert {row["name"] for row in updated.json()["model_presets"]} == {"default"}
    assert "done" in await bob.turn(chat, "hello after deletion")
    status = await bob.command(chat, "/model")
    assert "Current preset: `default`" in status
    assert "Could not switch" in await bob.command(chat, "/model team-model")

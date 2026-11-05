"""Container acceptance for multi-user mode: the real gateway image, driven over its HTTP/WebSocket contract.

Optional: skipped unless Docker is reachable and ``NANOBOT_E2E_DOCKER=1``. It builds the image, starts the gateway
through ``docker-compose.yml`` + ``docker-compose.multiuser.yml`` and uses a scripted model endpoint on the host
(see ``fake_llm.py``), so no API key is needed. The gateway publishes port 8765 on the host; stop other gateways first.
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
from fake_llm import start_fake_llm
from websockets.asyncio.client import ClientConnection

from nanobot.multiuser.accounts import hash_password

REPO = Path(__file__).resolve().parents[3]
PROJECT = "nanobot-mu-e2e"
PORT = 8765
PASSWORD = "e2e-initial-pw"
USERS = ("alice", "bob", "root")
ADMIN = "root"
DATA = "/home/nanobot/.nanobot"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


pytestmark = pytest.mark.skipif(
    os.environ.get("NANOBOT_E2E_DOCKER") != "1" or not _docker_ready(),
    reason="set NANOBOT_E2E_DOCKER=1 and start Docker to run the container acceptance test",
)


@dataclass
class Deployment:
    base: str
    data: Path
    compose: list[str]

    def exec(
        self, *argv: str, user: str | None = None, stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        flags = ["-u", user] if user else []
        return subprocess.run(
            [*self.compose, "exec", "-T", *flags, "nanobot-gateway", *argv],
            capture_output=True, text=True, timeout=60, input=stdin,
        )


def _write_config(data: Path, llm_port: int) -> None:
    (data / "users.txt").write_text("# e2e allowlist\n" + "\n".join(USERS) + "\n")
    (data / "echo_server.py").write_text(
        "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP('echo')\n\n\n@mcp.tool()\n"
        "def ping(text: str) -> str:\n    return text\n\n\nmcp.run()\n"
    )
    (data / "catalog.json").write_text(json.dumps({"entries": [{
        "name": "echo",
        "kind": "mcp",
        "description": "Echo test server",
        "config": {"command": "python", "args": [f"{DATA}/echo_server.py"]},
    }]}))
    config = {
        "agents": {"defaults": {"model": "fake-model", "provider": "custom"}},
        "providers": {"custom": {"apiKey": "x", "apiBase": f"http://host.docker.internal:{llm_port}/v1"}},
        "channels": {"websocket": {
            "enabled": True, "host": "0.0.0.0", "tokenIssueSecret": secrets.token_hex(16),
            "websocketRequiresToken": True,
        }},
        "gateway": {"host": "0.0.0.0"},
        "multiUser": {
            "enabled": True,
            "usersFile": f"{DATA}/users.txt",
            "admins": [ADMIN],
            "catalogFile": f"{DATA}/catalog.json",
            "workspacesRoot": f"{DATA}/workspaces",
            "maxActiveUsers": 8,
        },
    }
    (data / "config.json").write_text(json.dumps(config, indent=2))


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    root = tmp_path_factory.mktemp("mu-e2e")
    data, work = root / "data", root / "work"
    data.mkdir()
    work.mkdir()
    server, llm_port = start_fake_llm()
    _write_config(data, llm_port)
    (work / ".env").write_text(f"NANOBOT_INITIAL_PASSWORD_HASH='{hash_password(PASSWORD)}'\n")
    (work / "override.yml").write_text(
        "services:\n  nanobot-gateway:\n    volumes:\n"
        f"      - {data}:{DATA}\n    extra_hosts:\n      - host.docker.internal:host-gateway\n"
    )
    compose = [
        "docker", "compose", "-p", PROJECT, "--env-file", str(work / ".env"),
        "-f", str(REPO / "docker-compose.yml"), "-f", str(REPO / "docker-compose.multiuser.yml"),
        "-f", str(work / "override.yml"),
    ]
    up = subprocess.run([*compose, "up", "-d", "--build", "nanobot-gateway"], capture_output=True, text=True)
    deployment = Deployment(f"http://127.0.0.1:{PORT}", data, compose)
    try:
        assert up.returncode == 0, up.stderr[-2000:]
        deadline = time.monotonic() + 120
        while True:
            try:
                if httpx.post(f"{deployment.base}/auth/login", json={}, timeout=3).status_code in (400, 401):
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                logs = subprocess.run([*compose, "logs", "--tail", "60"], capture_output=True, text=True).stdout
                pytest.fail(f"gateway did not come up:\n{logs}")
            time.sleep(1)
        yield deployment
    finally:
        # The entrypoint chowns the data dir to uid 1000; hand it back so pytest can clean up.
        deployment.exec("chown", "-R", f"{os.getuid()}:{os.getgid()}", DATA, user="root")
        subprocess.run([*compose, "down", "-v", "--timeout", "10"], capture_output=True)
        server.shutdown()


class Client:
    """One logged-in account: bearer token for HTTP, a ticketed WebSocket for chats."""

    def __init__(self, base: str, user_id: str, token: str) -> None:
        self.base, self.user_id, self.token = base, user_id, token
        self.ws: ClientConnection | None = None
        self.frames: list[dict[str, Any]] = []

    def http(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        return httpx.request(method, f"{self.base}{path}", headers=headers, timeout=20, **kwargs)

    async def connect(self) -> None:
        ticket = self.http("POST", "/auth/ws-ticket", json={})
        assert ticket.status_code == 200, ticket.text
        url = self.base.replace("http", "ws", 1) + f"/?ticket={ticket.json()['ticket']}"
        self.ws = await websockets.connect(url)
        assert json.loads(await self.ws.recv())["event"] == "ready"

    async def next(self, predicate, timeout: float = 60.0) -> dict[str, Any]:
        assert self.ws is not None
        async with asyncio.timeout(timeout):
            while True:
                frame = json.loads(await self.ws.recv())
                self.frames.append(frame)
                if predicate(frame):
                    return frame

    async def new_chat(self) -> str:
        assert self.ws is not None
        await self.ws.send(json.dumps({"type": "new_chat"}))
        return (await self.next(lambda f: f.get("event") == "attached"))["chat_id"]

    async def turn(self, chat_id: str, content: str) -> str:
        """Send one message and return the assistant text of the completed turn."""
        assert self.ws is not None
        start = len(self.frames)
        frame = {"type": "message", "chat_id": chat_id, "content": content, "webui": True,
                 "turn_id": f"t-{len(self.frames)}"}
        await self.ws.send(json.dumps(frame))
        await self.next(lambda f: f.get("event") == "turn_end" and f.get("chat_id") == chat_id)
        return "".join(
            str(f.get("text") or "") for f in self.frames[start:]
            if f.get("chat_id") == chat_id and f.get("event") in {"message", "delta"}
        )

    async def shell(self, chat_id: str, command: str) -> str:
        return await self.turn(chat_id, f"run: {command}")


@pytest.fixture
async def users(deployment: Deployment) -> AsyncIterator[dict[str, Client]]:
    clients: dict[str, Client] = {}
    for user_id in USERS:
        response = httpx.post(
            f"{deployment.base}/auth/login", json={"user_id": user_id, "password": PASSWORD}, timeout=30,
        )
        assert response.status_code == 200, response.text
        clients[user_id] = Client(deployment.base, user_id, response.json()["token"])
        await clients[user_id].connect()
    try:
        yield clients
    finally:
        for client in clients.values():
            if client.ws is not None:
                await client.ws.close()


async def test_sessions_and_memory_are_not_shared(deployment: Deployment, users: dict[str, Client]) -> None:
    alice, bob = users["alice"], users["bob"]
    a_chat, b_chat = await alice.new_chat(), await bob.new_chat()
    assert "done" in await alice.turn(a_chat, "hello from alice")
    assert "tool said" in await bob.shell(b_chat, "mkdir -p memory && echo BOB-MEMORY-9f3 >> memory/MEMORY.md")

    assert a_chat in alice.http("GET", "/api/sessions").text
    assert a_chat not in bob.http("GET", "/api/sessions").text
    assert b_chat not in alice.http("GET", "/api/sessions").text
    foreign = alice.http("GET", f"/api/sessions/websocket%3A{b_chat}/webui-thread")
    assert foreign.status_code != 200 and "BOB-MEMORY" not in foreign.text

    await alice.ws.send(json.dumps({"type": "attach", "chat_id": b_chat}))  # type: ignore[union-attr]
    refused = await alice.next(lambda f: f.get("event") in {"error", "attached"})
    assert refused["event"] == "error"

    # Bob's own history carries the marker; Alice's prompt never does.
    assert "probe=True" in await bob.turn(b_chat, "probe BOB-MEMORY-9f3")
    assert "probe=False" in await alice.turn(a_chat, "probe BOB-MEMORY-9f3")
    bob_memory = deployment.exec("cat", f"{DATA}/workspaces/bob/workspace/memory/MEMORY.md")
    assert "BOB-MEMORY-9f3" in bob_memory.stdout
    assert "BOB-MEMORY-9f3" not in deployment.exec("grep", "-r", "BOB-MEMORY", f"{DATA}/workspaces/alice").stdout


async def test_alice_shell_is_confined(deployment: Deployment, users: dict[str, Client]) -> None:
    alice, bob = users["alice"], users["bob"]
    a_chat, b_chat = await alice.new_chat(), await bob.new_chat()
    await bob.shell(b_chat, "echo BOB-FILE-7c1 > bob_note.txt")
    assert (deployment.data / "workspaces/bob/workspace/bob_note.txt").exists()

    own = await alice.shell(a_chat, "echo alice-ok > a.txt && cat a.txt")
    assert "alice-ok" in own

    # The textual command guard stops the obvious spelling above. That is not the boundary under test, so the
    # probes below are a script file the guard cannot read, which only the namespace sandbox can confine.
    deployment.exec(
        "tee", f"{DATA}/workspaces/alice/workspace/probe.py", user="nanobot", stdin=PROBE_SCRIPT,
    )
    report = await alice.shell(a_chat, "python3 probe.py")
    assert "BOB-FILE-7c1" not in report, report
    assert "bob_read=DENIED" in report, report
    assert "bob_listed=False" in report, report
    assert "gateway_procs=0" in report, report
    for host in ("169.254.169.254", "host.docker.internal", "10.0.0.1", "172.17.0.1"):
        assert f"{host}=NOROUTE" in report, report
    assert "REACHED" not in report, report


PROBE_SCRIPT = """\
import os
import socket

root = os.sep.join(["", "home", "nanobot", ".nanobot"])
note = os.sep.join([root, "workspaces", "bob", "workspace", "bob_note.txt"])
try:
    open(note).read()
    print("bob_read=READ")
except OSError:
    print("bob_read=DENIED")
print("bob_listed=%s" % (os.path.isdir(os.path.join(root, "workspaces", "bob")),))
count = 0
for pid in os.listdir(os.sep + "proc"):
    if pid.isdigit():
        try:
            count += b"gateway" in open(os.sep.join(["", "proc", pid, "cmdline"]), "rb").read()
        except OSError:
            pass
print("gateway_procs=%d" % count)
for host in ("169.254.169.254", "host.docker.internal", "10.0.0.1", "172.17.0.1"):
    sock = socket.socket()
    sock.settimeout(4)
    try:
        sock.connect((host, 80))
        print(host + "=REACHED")
    except OSError:
        print(host + "=NOROUTE")
    finally:
        sock.close()
"""


async def test_mcp_enabled_by_alice_is_invisible_to_bob(users: dict[str, Client]) -> None:
    alice, bob = users["alice"], users["bob"]

    def state(client: Client) -> bool:
        entries = client.http("GET", "/api/mcp/catalog").json()["entries"]
        return next(e for e in entries if e["name"] == "echo").get("enabled", False)

    assert not state(alice) and not state(bob)
    enabled = alice.http("POST", "/api/mcp/catalog/enable", json={"name": "echo", "enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert state(alice) and not state(bob)

    a_tools = await alice.turn(await alice.new_chat(), "tools")
    b_tools = await bob.turn(await bob.new_chat(), "tools")
    assert "echo" in a_tools, a_tools
    assert "echo" not in b_tools, b_tools


async def test_admin_usage_lists_both_users(deployment: Deployment, users: dict[str, Client]) -> None:
    for name in ("alice", "bob"):
        assert "done" in await users[name].turn(await users[name].new_chat(), "hello")
    admin_view = users[ADMIN].http("GET", "/api/admin/usage")
    assert admin_view.status_code == 200, admin_view.text
    recorded = {row.get("user_id") for row in admin_view.json()["users_30d"]}
    assert {"alice", "bob"} <= recorded
    assert users["alice"].http("GET", "/api/admin/usage").status_code == 403


async def test_removing_alice_from_the_allowlist_disconnects_her(
    deployment: Deployment, users: dict[str, Client],
) -> None:
    alice = users["alice"]
    removed = deployment.exec(
        "sh", "-c", f"grep -vx alice {DATA}/users.txt > {DATA}/users.tmp && mv {DATA}/users.tmp {DATA}/users.txt",
    )
    assert removed.returncode == 0, removed.stderr
    assert alice.ws is not None
    with pytest.raises(websockets.ConnectionClosed):
        async with asyncio.timeout(30):
            while True:
                await alice.ws.recv()
    relogin = httpx.post(
        f"{deployment.base}/auth/login", json={"user_id": "alice", "password": PASSWORD}, timeout=30,
    )
    assert relogin.status_code == 401
    assert alice.http("GET", "/api/sessions").status_code == 401
    assert users["bob"].http("GET", "/api/sessions").status_code == 200

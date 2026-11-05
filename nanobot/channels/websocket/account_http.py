"""Multi-user account routes: password login and password change.

Both run scrypt, so they execute in a worker thread to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from nanobot.multiuser.accounts import AccountStore
from nanobot.multiuser.user_settings import UserSettingsError

if TYPE_CHECKING:
    from nanobot.webui.gateway_tokens import GatewayTokenStore
    from nanobot.webui.tenants import WebUITenants

LOGIN_PATH = "/auth/login"
PASSWORD_PATH = "/auth/password"
WS_TICKET_PATH = "/auth/ws-ticket"
WS_TICKET_TTL_S = 30
MCP_ENABLE_PATH = "/api/mcp/catalog/enable"
MCP_PARAMS_PATH = "/api/mcp/catalog/params"
ACCOUNT_PATHS = frozenset({LOGIN_PATH, PASSWORD_PATH, WS_TICKET_PATH, MCP_ENABLE_PATH, MCP_PARAMS_PATH})
_MAX_BODY_BYTES = 4096


class _BadRequestError(Exception):
    pass


async def _read_json(request: web.BaseRequest) -> dict[str, Any]:
    # Requiring a JSON content type keeps cross-site form posts out without CORS preflight.
    if request.content_type != "application/json":
        raise _BadRequestError("expected application/json")
    body = bytearray()
    async for chunk in request.content.iter_chunked(_MAX_BODY_BYTES):
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise _BadRequestError("request body too large")
    try:
        data = cast(object, json.loads(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BadRequestError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise _BadRequestError("expected a JSON object")
    return cast(dict[str, Any], data)


async def _read_fields(request: web.BaseRequest, *names: str) -> tuple[str, ...]:
    payload = await _read_json(request)
    values = tuple(payload.get(name) for name in names)
    if not all(isinstance(value, str) and value for value in values):
        raise _BadRequestError(f"expected non-empty string fields: {', '.join(names)}")
    return cast(tuple[str, ...], values)


class AccountHTTP:
    def __init__(
        self,
        accounts: AccountStore,
        tenants: WebUITenants | None = None,
        tokens: GatewayTokenStore | None = None,
    ) -> None:
        self._accounts = accounts
        self._tokens = tokens
        self._tenants = tenants

    async def handle(self, request: web.BaseRequest) -> web.StreamResponse:
        if request.method != "POST":
            response = web.json_response({"error": "method_not_allowed"}, status=405)
        else:
            try:
                if request.path == LOGIN_PATH:
                    response = await self._login(request)
                elif request.path == WS_TICKET_PATH:
                    response = self._ws_ticket(request)
                elif request.path in (MCP_ENABLE_PATH, MCP_PARAMS_PATH):
                    response = await self._mcp_catalog(request)
                else:
                    response = await self._change_password(request)
            except _BadRequestError as exc:
                response = web.json_response({"error": str(exc)}, status=400)
        response.headers["Cache-Control"] = "no-store"
        response.force_close()
        return response

    async def _login(self, request: web.BaseRequest) -> web.StreamResponse:
        user_id, password = await _read_fields(request, "user_id", "password")
        token = await asyncio.to_thread(self._accounts.login, user_id, password)
        if token is None:
            return web.json_response({"error": "invalid_credentials"}, status=401)
        return web.json_response({"token": token})

    def _ws_ticket(self, request: web.BaseRequest) -> web.StreamResponse:
        """Hand the caller a single-use ticket for opening the WebSocket (never the account token)."""
        authorization = request.headers.get("Authorization", "")
        user_id = (
            self._accounts.authenticate(authorization.removeprefix("Bearer "))
            if authorization.startswith("Bearer ")
            else None
        )
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        ticket = self._tokens.issue_ws_ticket(user_id, WS_TICKET_TTL_S) if self._tokens is not None else None
        if ticket is None:
            return web.json_response({"error": "too_many_tickets"}, status=429)
        return web.json_response({"ticket": ticket, "expires_in": WS_TICKET_TTL_S})

    async def _mcp_catalog(self, request: web.BaseRequest) -> web.StreamResponse:
        """Let the caller switch a catalog entry or fill in their own parameters for it.

        The account comes from the bearer token only. Names and parameters are checked against the
        administrator's catalog, so nothing here can add a server or change a command or address.
        """
        authorization = request.headers.get("Authorization", "")
        user_id = (
            self._accounts.authenticate(authorization.removeprefix("Bearer "))
            if authorization.startswith("Bearer ")
            else None
        )
        access = self._tenants.user_mcp if self._tenants is not None else None
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if access is None:
            return web.json_response({"error": "not_found"}, status=404)
        payload = await _read_json(request)
        name = payload.get("name")
        if not isinstance(name, str):
            raise _BadRequestError("expected a string field: name")
        try:
            if request.path == MCP_ENABLE_PATH:
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise _BadRequestError("enabled must be a boolean")
                await access.set_enabled(user_id, name, enabled)
            else:
                params = payload.get("params")
                if not isinstance(params, dict):
                    raise _BadRequestError("params must be an object")
                await access.set_params(user_id, name, cast(dict[str, Any], params))
        except UserSettingsError as exc:
            raise _BadRequestError(str(exc)) from exc
        return web.json_response({"entries": access.view(user_id)})

    async def _change_password(self, request: web.BaseRequest) -> web.StreamResponse:
        authorization = request.headers.get("Authorization", "")
        user_id = (
            self._accounts.authenticate(authorization.removeprefix("Bearer "))
            if authorization.startswith("Bearer ")
            else None
        )
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        old, new = await _read_fields(request, "old", "new")
        changed = await asyncio.to_thread(self._accounts.change_password, user_id, old, new)
        if not changed:
            return web.json_response({"error": "invalid_credentials"}, status=403)
        # A password change revokes every session of the user; keep this caller signed in.
        token = await asyncio.to_thread(self._accounts.login, user_id, new)
        if token is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"token": token})

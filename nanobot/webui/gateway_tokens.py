from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from websockets.http11 import Request as WsRequest

from nanobot.webui.http_utils import bearer_token, parse_query, query_first

IssuedTokenAudience = Literal["client", "webui"]


@dataclass
class GatewayTokenStore:
    """Own short-lived WebSocket and WebUI API tokens for one gateway process."""

    max_tokens: int = 10_000
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()), init=False)
    issued_tokens: dict[str, float] = field(default_factory=dict)
    issued_token_audiences: dict[str, IssuedTokenAudience] = field(default_factory=dict)
    api_tokens: dict[str, float] = field(default_factory=dict)
    # Multi-user mode: single-use WebSocket handshake tickets, ticket -> (expiry, user_id).
    ws_tickets: dict[str, tuple[float, str]] = field(default_factory=dict)

    def check_api_token(self, request: WsRequest) -> bool:
        self._purge_expired_api_tokens()
        token = bearer_token(request.headers) or query_first(
            parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self.api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self.api_tokens.pop(token, None)
            return False
        return True

    def can_issue(self, *, include_api_token: bool = False) -> bool:
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if len(self.issued_tokens) >= self.max_tokens:
            return False
        if include_api_token and len(self.api_tokens) >= self.max_tokens:
            return False
        return True

    def issue_token(
        self,
        ttl_s: int | float,
        *,
        audience: IssuedTokenAudience = "client",
    ) -> str:
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.issued_tokens[token_value] = expiry
        self.issued_token_audiences[token_value] = audience
        return token_value

    def issue_api_token(self, ttl_s: int | float) -> str:
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.api_tokens[token_value] = expiry
        return token_value

    def take_issued_token_audience(
        self,
        token_value: str | None,
    ) -> IssuedTokenAudience | None:
        if not token_value:
            return None
        self._purge_expired_issued_tokens()
        expiry = self.issued_tokens.pop(token_value, None)
        if expiry is None:
            self.issued_token_audiences.pop(token_value, None)
            return None
        audience = self.issued_token_audiences.pop(token_value, "client")
        if time.monotonic() > expiry:
            return None
        return audience

    def issue_ws_ticket(self, user_id: str, ttl_s: int | float) -> str | None:
        """Mint a one-shot handshake ticket for ``user_id``; ``None`` when too many are outstanding."""
        now = time.monotonic()
        for ticket, (expiry, _) in list(self.ws_tickets.items()):
            if now > expiry:
                self.ws_tickets.pop(ticket, None)
        if len(self.ws_tickets) >= self.max_tokens:
            return None
        ticket = f"nbwk_{secrets.token_urlsafe(32)}"
        self.ws_tickets[ticket] = (now + float(ttl_s), user_id)
        return ticket

    def take_ws_ticket(self, ticket: str | None) -> str | None:
        """Consume a handshake ticket and return the account it was issued to."""
        entry = self.ws_tickets.pop(ticket, None) if ticket else None
        if entry is None or time.monotonic() > entry[0]:
            return None
        return entry[1]

    def clear(self) -> None:
        self.ws_tickets.clear()
        self.issued_tokens.clear()
        self.issued_token_audiences.clear()
        self.api_tokens.clear()

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.api_tokens.items()):
            if now > expiry:
                self.api_tokens.pop(token_key, None)

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.issued_tokens.items()):
            if now > expiry:
                self.issued_tokens.pop(token_key, None)
                self.issued_token_audiences.pop(token_key, None)


def token_response_payload(token: str, expires_in: Any) -> dict[str, Any]:
    return {"token": token, "expires_in": expires_in}

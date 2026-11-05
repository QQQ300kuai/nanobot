"""Administrator-defined MCP server and plugin catalog (multi-user mode).

The catalog is a JSON file maintained by the administrator; there is no UI and users cannot change it.
Users may only switch entries on or off and fill in the parameters an entry declares.

```json
{"entries": [{
  "name": "notes",
  "kind": "mcp",
  "description": "Team notes",
  "config": {"command": "notes-mcp", "env": {"NOTES_TOKEN": "{{token}}"}},
  "perUserInstance": false,
  "paramSchema": {"token": {"secret": true, "description": "Your personal token", "required": true}}
}]}
```

``{{param}}`` placeholders in ``config`` strings are replaced with the caller's own values.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationError, field_validator, model_validator

from nanobot.config.schema import MCPServerConfig
from nanobot.config_base import Base

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]{0,63})\}\}")


class CatalogError(ValueError):
    """The catalog file is missing or invalid."""


class ParamSpec(Base):
    """One parameter the user may fill in; ``secret`` values are encrypted at rest."""

    secret: bool = False
    description: str = ""
    required: bool = False


class CatalogEntry(Base):
    name: str
    kind: Literal["mcp", "plugin"] = "mcp"
    description: str = ""
    config: MCPServerConfig | None = None
    per_user_instance: bool = False
    param_schema: dict[str, ParamSpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"invalid catalog entry name {value!r}")
        return value

    @field_validator("param_schema")
    @classmethod
    def _valid_params(cls, value: dict[str, ParamSpec]) -> dict[str, ParamSpec]:
        for key in value:
            if not _PARAM_RE.fullmatch(key):
                raise ValueError(f"invalid parameter name {key!r}")
        return value

    @model_validator(mode="after")
    def _check_config(self) -> CatalogEntry:
        cfg = self.config
        if cfg is None:
            if self.kind == "mcp":
                raise ValueError(f"mcp entry {self.name!r} needs a config")
            return self
        if not (cfg.command or cfg.url):
            raise ValueError(f"entry {self.name!r} config needs a command or a url")
        if "{{" in cfg.url or "{{" in cfg.command or "{{" in cfg.cwd:
            raise ValueError(f"entry {self.name!r}: parameters may not appear in the command, cwd or url")
        if cfg.auth == "oauth":
            raise ValueError(f"entry {self.name!r}: oauth servers are not supported in the catalog")
        return self

    @property
    def instance_per_user(self) -> bool:
        """Whether each user gets a separate connection.

        HTTP/SSE servers carry the caller in a request header, and any entry holding personal
        parameters carries the user's own credentials; neither may share a connection.
        """
        cfg = self.config
        remote = cfg is not None and not cfg.command
        return self.per_user_instance or bool(self.param_schema) or remote


class Catalog:
    def __init__(self, entries: list[CatalogEntry] | None = None) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        for entry in entries or []:
            if entry.name in self._entries:
                raise CatalogError(f"duplicate catalog entry {entry.name!r}")
            self._entries[entry.name] = entry

    @classmethod
    def load(cls, path: Path) -> Catalog:
        try:
            raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            raise CatalogError(f"cannot read MCP catalog {path}: {exc}") from exc
        except ValueError as exc:
            raise CatalogError(f"MCP catalog {path} is not valid JSON: {exc}") from exc
        items = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise CatalogError(f"MCP catalog {path} must be an object with an 'entries' list")
        entries: list[CatalogEntry] = []
        for item in items:
            try:
                entries.append(CatalogEntry.model_validate(item))
            except ValidationError as exc:
                label = item.get("name") if isinstance(item, dict) else item
                raise CatalogError(f"MCP catalog {path}: invalid entry {label!r}: {exc}") from exc
        return cls(entries)

    @property
    def entries(self) -> list[CatalogEntry]:
        return list(self._entries.values())

    def get(self, name: str) -> CatalogEntry | None:
        return self._entries.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def server_urls(self) -> list[str]:
        return [e.config.url for e in self._entries.values() if e.config is not None and e.config.url]


def open_catalog(catalog_file: Path | None) -> Catalog:
    """The configured catalog; an unset path means no MCP server or plugin for anyone."""
    return Catalog.load(catalog_file) if catalog_file is not None else Catalog()


def catalog_networks(catalog: Catalog) -> list[str]:
    """Host addresses of the administrator's catalog URLs, for the SSRF allow set.

    Derived only from catalog entries; nothing a user supplies ever reaches this.
    """
    found: set[str] = set()
    for url in catalog.server_urls():
        host = urlparse(url).hostname
        if not host:
            continue
        try:
            found.add(str(ipaddress.ip_address(host)))
            continue
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError:
            continue
        found.update(info[4][0] for info in infos)
    return sorted(found)

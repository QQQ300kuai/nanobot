"""One user's MCP/plugin choices: which catalog entries are on, and their own parameters.

Stored under the account directory, outside the agent workspace. Secret parameters are encrypted
with the gateway's Fernet key, so the file never holds a token in plaintext.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from nanobot.multiuser.catalog import Catalog, CatalogEntry
from nanobot.multiuser.identity import UserPaths

KEY_FILE_NAME = "multiuser.key"
_MAX_PARAM_CHARS = 4096


class UserSettingsError(ValueError):
    """The request names something the administrator's catalog does not offer, or a bad value."""


def load_or_create_key(data_dir: Path) -> bytes:
    """The gateway's Fernet key at ``<data_dir>/multiuser.key`` (0600), generated on first start."""
    path = Path(data_dir) / KEY_FILE_NAME
    try:
        key = path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        return key
    Fernet(key)  # fail loudly on a corrupt key rather than silently losing every secret
    return key


class UserSettings:
    def __init__(self, paths: UserPaths, fernet_key: bytes, catalog: Catalog) -> None:
        self._file = paths.mcp_settings_file
        self._fernet = Fernet(fernet_key)
        self._catalog = catalog

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._file.parent, prefix=".mcp-settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _entry(self, name: str) -> CatalogEntry:
        entry = self._catalog.get(name)
        if entry is None:
            raise UserSettingsError(f"{name!r} is not in the catalog")
        return entry

    def enabled(self) -> set[str]:
        """Enabled names that the catalog still offers."""
        raw = self._read().get("enabled")
        names = raw if isinstance(raw, list) else []
        return {n for n in names if isinstance(n, str) and n in self._catalog}

    def set_enabled(self, name: str, on: bool) -> None:
        self._entry(name)
        data = self._read()
        names = {n for n in data.get("enabled", []) if isinstance(n, str)}
        names.add(name) if on else names.discard(name)
        data["enabled"] = sorted(names)
        self._write(data)

    def set_params(self, name: str, params: dict[str, Any]) -> None:
        """Replace the user's parameters for ``name``; only keys the entry declares are accepted."""
        entry = self._entry(name)
        unknown = sorted(set(params) - set(entry.param_schema))
        if unknown:
            raise UserSettingsError(f"unknown parameters for {name!r}: {', '.join(unknown)}")
        stored: dict[str, Any] = {}
        for key, value in params.items():
            if not isinstance(value, str) or len(value) > _MAX_PARAM_CHARS or any(
                ord(ch) < 32 or ord(ch) == 127 for ch in value
            ):
                raise UserSettingsError(f"parameter {key!r} must be a short single-line string")
            if entry.param_schema[key].secret:
                stored[key] = {"secret": True, "value": self._fernet.encrypt(value.encode()).decode()}
            else:
                stored[key] = {"secret": False, "value": value}
        data = self._read()
        data.setdefault("params", {})[name] = stored
        self._write(data)

    def get_params(self, name: str) -> dict[str, str]:
        """The user's decrypted parameters for ``name`` (empty for names outside the catalog)."""
        entry = self._catalog.get(name)
        stored = self._read().get("params", {}).get(name)
        if entry is None or not isinstance(stored, dict):
            return {}
        out: dict[str, str] = {}
        for key, item in stored.items():
            spec = entry.param_schema.get(key)
            if spec is None or not isinstance(item, dict) or not isinstance(item.get("value"), str):
                continue
            if spec.secret:
                try:
                    out[key] = self._fernet.decrypt(item["value"].encode()).decode()
                except InvalidToken:
                    continue
            else:
                out[key] = item["value"]
        return out

    def view(self) -> list[dict[str, Any]]:
        """What the user may see: each entry, its switch, and which parameters are set (never secret values)."""
        enabled = self.enabled()
        result: list[dict[str, Any]] = []
        for entry in self._catalog.entries:
            values = self.get_params(entry.name)
            params = []
            for key, spec in entry.param_schema.items():
                item: dict[str, Any] = {
                    "name": key, "secret": spec.secret,
                    "description": spec.description, "required": spec.required,
                    "set": key in values,
                }
                if not spec.secret and key in values:
                    item["value"] = values[key]
                params.append(item)
            result.append({
                "name": entry.name, "kind": entry.kind, "description": entry.description,
                "enabled": entry.name in enabled, "params": params,
            })
        return result

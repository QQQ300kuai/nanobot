"""User id validation and per-user filesystem layout."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from loguru import logger

from nanobot.security.workspace_policy import is_path_within

_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InvalidUserId(ValueError):
    """Raised when a user id is not safe to use as an identity or directory name."""


def validate_user_id(raw: str) -> str:
    """Return the normalized (lowercase) user id or raise InvalidUserId."""
    if not isinstance(raw, str) or not _USER_ID_RE.fullmatch(raw) or ".." in raw:
        raise InvalidUserId(f"invalid user id: {raw!r}")
    return raw.lower()


class UserPaths:
    """Filesystem locations owned by one user, all under ``root/<user_id>/``."""

    def __init__(self, root: Path, user_id: str) -> None:
        self.user_id = validate_user_id(user_id)
        self.root = Path(root)
        self.base = self.root / self.user_id
        if not is_path_within(self.base, self.root) or self.base.resolve() == self.root.resolve():
            raise InvalidUserId(f"user directory escapes root: {user_id!r}")
        self.workspace = self.base / "workspace"
        self.config_file = self.base / "config.json"
        self.secrets_file = self.base / "secrets.json"
        # The user's MCP/plugin switches and parameters; outside the workspace, secrets encrypted.
        self.mcp_settings_file = self.base / "mcp-settings.json"
        self.media_dir = self.base / "media"
        # Outside the agent workspace: the agent's own file tools cannot edit its schedule.
        self.cron_store = self.base / "cron" / "jobs.json"


def normalize_admins(admins: Iterable[str]) -> frozenset[str]:
    """Return the valid, normalized ``multi_user.admins`` entries; invalid ones are logged and ignored."""
    normalized: set[str] = set()
    for raw in admins:
        try:
            normalized.add(validate_user_id(raw))
        except InvalidUserId:
            logger.warning("Ignoring invalid multiUser.admins entry {!r}", raw)
    return frozenset(normalized)

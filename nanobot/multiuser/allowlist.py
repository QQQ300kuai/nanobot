"""Hot-reloaded user allowlist backed by a one-id-per-line text file."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from nanobot.multiuser.identity import InvalidUserId, validate_user_id
from nanobot.utils.atomic_file import read_atomic_bytes


class UserAllowlist:
    """Set of allowed user ids; removals are announced to subscribers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._ids: frozenset[str] = frozenset()
        self._removed_callbacks: list[Callable[[str], None]] = []
        self._added_callbacks: list[Callable[[str], None]] = []
        self._signature: tuple[int, int] | None = None
        self._loaded = False
        self.reload()

    def contains(self, user_id: str) -> bool:
        try:
            return validate_user_id(user_id) in self._ids
        except InvalidUserId:
            return False

    def subscribe_removed(self, cb: Callable[[str], None]) -> None:
        self._removed_callbacks.append(cb)

    def subscribe_added(self, cb: Callable[[str], None]) -> None:
        """Announce ids that appear after the first load (a removed user returning included)."""
        self._added_callbacks.append(cb)

    def reload(self) -> None:
        self._signature = self._stat_signature()
        try:
            text = read_atomic_bytes(self.path).decode("utf-8")
        except FileNotFoundError:
            if self._loaded:
                # A vanished file (editor save, deploy swap) must not revoke everyone.
                logger.warning("Allowlist file {} is missing; keeping previous entries", self.path)
                return
            text = ""
        self._loaded = True
        ids: set[str] = set()
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ids.add(validate_user_id(line))
            except InvalidUserId:
                logger.warning("Ignoring invalid user id at {}:{}", self.path, lineno)
        removed = sorted(self._ids - ids)
        added = sorted(ids - self._ids)
        self._ids = frozenset(ids)
        for user_id in removed:
            self._announce(self._removed_callbacks, user_id)
        for user_id in added:
            self._announce(self._added_callbacks, user_id)

    @staticmethod
    def _announce(callbacks: list[Callable[[str], None]], user_id: str) -> None:
        for cb in list(callbacks):
            try:
                cb(user_id)
            except Exception:
                logger.exception("Allowlist subscriber failed for {}", user_id)

    async def watch(self, interval_s: float = 2.0) -> None:
        """Poll the file and reload on change until cancelled."""
        while True:
            await asyncio.sleep(interval_s)
            self._poll()

    def _poll(self) -> None:
        signature = self._stat_signature()
        if signature == self._signature:
            return
        try:
            self.reload()
        except Exception:
            logger.exception("Failed to reload allowlist {}", self.path)

    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

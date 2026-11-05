"""Read and write workspace files without following links that leave the workspace.

A multi-user account's sandboxed shell can plant a symlink in its workspace pointing at any
host path. Gateway code runs outside that sandbox, so it must not follow such a link. Each
function takes ``root``: ``None`` keeps plain ``pathlib`` behavior (single-user workspaces may
symlink their own files); a root confines the access to files under it.

A confined access resolves the path, requires the result under the resolved root, and then
re-walks it one component at a time with ``O_NOFOLLOW``, so a link swapped in after the check
is refused too. Writes go to a temporary sibling renamed over the target, which replaces a
planted link or hard link instead of writing through it. A refused path raises
``PathEscapeError``, a ``FileNotFoundError``, so callers treat it as missing.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from loguru import logger

from nanobot.utils.atomic_file import atomic_write_lines, atomic_write_text

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Windows has no dir_fd; multi-user mode refuses to start there.
_DIR_FD = os.open in os.supports_dir_fd


# Written by the gateway beside a multi-user account's workspace, never inside it, so the
# account's sandbox (which binds only the workspace) can neither create nor remove it.
ACCOUNT_WORKSPACE_MARKER = ".account-workspace"


def mark_account_workspace(workspace: Path) -> None:
    """Record that ``workspace`` belongs to a multi-user account; its files are confined."""
    marker = workspace.parent / ACCOUNT_WORKSPACE_MARKER
    if not marker.is_file():
        marker.write_text("", encoding="utf-8")


def confinement_root(workspace: Path) -> Path | None:
    """The root to confine a workspace's files to: itself for an account workspace, else ``None``.

    Callers compute this once per workspace and pass it as ``root``.
    """
    return workspace if (workspace.parent / ACCOUNT_WORKSPACE_MARKER).is_file() else None


class PathEscapeError(FileNotFoundError):
    """A confined path leaves its root or is not a regular file; treated as missing."""


def _refuse(path: Path, reason: str) -> PathEscapeError:
    logger.warning("Refusing workspace file {}: {}", path, reason)
    return PathEscapeError(f"{path}: {reason}")


def _parts_under(path: Path, root: Path) -> tuple[Path, list[str]]:
    real_root = root.expanduser().resolve(strict=False)
    resolved = path.expanduser().resolve(strict=False)
    try:
        parts = list(resolved.relative_to(real_root).parts)
    except ValueError:
        raise _refuse(path, f"resolves outside {root}") from None
    if not parts:
        raise _refuse(path, "is the workspace root")
    return real_root, parts


@contextmanager
def _parent_fd(path: Path, root: Path, *, create: bool) -> Iterator[tuple[int, str]]:
    """Yield an fd of the path's parent, walked from the root without following links."""
    real_root, parts = _parts_under(path, root)
    fd = os.open(real_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except NotADirectoryError:
                raise _refuse(path, f"component {part!r} is not a directory") from None
            except OSError as exc:
                if exc.errno == getattr(os, "ELOOP", None):
                    raise _refuse(path, f"component {part!r} became a link") from None
                raise
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _open_regular(path: Path, root: Path, flags: int, mode: int = 0o666) -> int:
    with _parent_fd(path, root, create=bool(flags & os.O_CREAT)) as (dir_fd, name):
        try:
            fd = os.open(name, flags | _NOFOLLOW | _NONBLOCK, mode, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == getattr(os, "ELOOP", None):
                raise _refuse(path, "is a link") from None
            raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (flags & os.O_WRONLY and info.st_nlink > 1):
        os.close(fd)
        raise _refuse(path, "is not a regular single-link file")
    return fd


def read_text(path: Path, *, root: Path | None, encoding: str = "utf-8", errors: str | None = None) -> str:
    """``path.read_text`` that refuses, when confined, a file outside ``root``."""
    if root is None or not _DIR_FD:
        return path.read_text(encoding=encoding, errors=errors)
    with os.fdopen(_open_regular(path, root, os.O_RDONLY), encoding=encoding, errors=errors) as handle:
        return handle.read()


def read_bytes(path: Path, *, root: Path | None) -> bytes:
    """``path.read_bytes`` that refuses, when confined, a file outside ``root``."""
    if root is None or not _DIR_FD:
        return path.read_bytes()
    with os.fdopen(_open_regular(path, root, os.O_RDONLY), "rb") as handle:
        return handle.read()


def open_read(path: Path, *, root: Path | None, binary: bool = False, encoding: str = "utf-8") -> IO[str] | IO[bytes]:
    """``open(path, "r"/"rb")`` that refuses, when confined, a file outside ``root``."""
    if root is None or not _DIR_FD:
        return open(path, "rb") if binary else open(path, encoding=encoding)
    fd = _open_regular(path, root, os.O_RDONLY)
    return os.fdopen(fd, "rb") if binary else os.fdopen(fd, encoding=encoding)


def append_text(path: Path, content: str, *, root: Path | None, encoding: str = "utf-8") -> None:
    """Append to (or create) a file; when confined, never through a link or hard link."""
    if root is None or not _DIR_FD:
        with open(path, "a", encoding=encoding) as handle:
            handle.write(content)
        return
    fd = _open_regular(path, root, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(fd, "a", encoding=encoding) as handle:
        handle.write(content)


def write_text(
    path: Path, content: str, *, root: Path | None, encoding: str = "utf-8", atomic: bool = False,
) -> None:
    """Replace a file's text.

    Unconfined, this is ``path.write_text`` (``atomic_write_text`` with ``atomic``). Confined,
    it always writes a temporary sibling and renames it over the target, creating missing
    parent directories under ``root`` without following links.
    """
    if root is None or not _DIR_FD:
        if atomic:
            atomic_write_text(path, content)
        else:
            path.write_text(content, encoding=encoding)
        return
    with _parent_fd(path, root, create=True) as (dir_fd, name):
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise


def replace_lines(path: Path, lines: Iterable[str], *, root: Path | None) -> None:
    """``atomic_write_lines`` that, when confined, writes only under ``root``."""
    if root is None or not _DIR_FD:
        atomic_write_lines(path, lines)
        return
    write_text(path, "".join(line if line.endswith("\n") else f"{line}\n" for line in lines), root=root)


def is_file(path: Path, *, root: Path | None) -> bool:
    """``path.is_file`` that, when confined, is false for a file outside ``root``."""
    if root is None or not _DIR_FD:
        return path.is_file()
    try:
        os.close(_open_regular(path, root, os.O_RDONLY))
    except OSError:
        return False
    return True

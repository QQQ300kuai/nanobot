"""Startup guard: multi-user mode must not run without a working sandbox."""

import shutil
import subprocess
import sys
from collections.abc import Callable

from nanobot.multiuser.config import MultiUserConfig


class MultiUserStartupError(RuntimeError):
    """Multi-user mode cannot start safely on this host."""


def probe_bwrap() -> bool:
    """Run a real bwrap invocation to verify user namespaces can be created.

    The whole root is bound read-only: on merged-/usr images (Debian) ``true`` needs ``/lib`` for its loader, so
    binding only ``/usr`` makes the probe fail on a host that can in fact sandbox.
    """
    try:
        result = subprocess.run(
            ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--proc", "/proc", "true"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_workspaces_root(cfg: MultiUserConfig) -> None:
    """Raise MultiUserStartupError unless multi-user mode knows where account workspaces live."""
    if cfg.enabled and cfg.workspaces_root is None:
        raise MultiUserStartupError(
            "Multi-user mode requires multiUser.workspacesRoot, the directory holding one workspace per account."
        )


def ensure_sandbox_ready(
    cfg: MultiUserConfig,
    *,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[], bool] = probe_bwrap,
) -> None:
    """Raise MultiUserStartupError unless a usable sandbox exists for multi-user mode."""
    if not cfg.enabled:
        return
    if platform.startswith("linux"):
        if which("bwrap") is None:
            raise MultiUserStartupError(
                "Multi-user mode requires bubblewrap (bwrap) on Linux; install it and retry."
            )
        if not probe():
            raise MultiUserStartupError(
                "bubblewrap is installed but cannot create a user namespace; "
                "enable unprivileged user namespaces for this host or container."
            )
        return
    if platform == "darwin":
        if not cfg.allow_dev_seatbelt:
            raise MultiUserStartupError(
                "Multi-user mode on macOS is development-only; "
                "set multiUser.allowDevSeatbelt to true to use the seatbelt sandbox."
            )
        return
    raise MultiUserStartupError(f"Multi-user mode is not supported on platform {platform!r}.")


def refuse_multi_user_entry(cfg: MultiUserConfig, entry: str) -> None:
    """Raise unless ``entry`` (a host-side runner outside the gateway) is safe: it has no account sandbox."""
    if cfg.enabled:
        raise MultiUserStartupError(
            f"{entry} cannot run with multi_user.enabled: it executes tools on the host without "
            "per-account isolation. Use the gateway WebUI, or use a config without multi_user."
        )

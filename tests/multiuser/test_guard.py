import pytest

from nanobot.config.schema import Config
from nanobot.multiuser.config import MultiUserConfig
from nanobot.multiuser.guard import (
    MultiUserStartupError,
    ensure_sandbox_ready,
    ensure_workspaces_root,
)


def _which(found: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in found else None


def _check(cfg, *, platform, found=(), probe=True):
    ensure_sandbox_ready(cfg, platform=platform, which=_which(set(found)), probe=lambda: probe)


def test_config_defaults_and_aliases():
    cfg = Config.model_validate({"multiUser": {"enabled": True, "allowDevSeatbelt": True, "sandboxNet": "share"}})
    assert cfg.multi_user.enabled and cfg.multi_user.allow_dev_seatbelt
    assert cfg.multi_user.sandbox_net == "share"
    assert Config().multi_user == MultiUserConfig()
    assert MultiUserConfig().sandbox_net == "none"


def test_disabled_skips_all_checks():
    _check(MultiUserConfig(), platform="win32")


def test_linux_requires_bwrap():
    with pytest.raises(MultiUserStartupError):
        _check(MultiUserConfig(enabled=True), platform="linux")


def test_linux_requires_working_user_namespace():
    with pytest.raises(MultiUserStartupError):
        _check(MultiUserConfig(enabled=True), platform="linux", found={"bwrap"}, probe=False)


def test_linux_ready():
    _check(MultiUserConfig(enabled=True), platform="linux", found={"bwrap"})


def test_macos_needs_dev_seatbelt_opt_in():
    with pytest.raises(MultiUserStartupError):
        _check(MultiUserConfig(enabled=True), platform="darwin")
    _check(MultiUserConfig(enabled=True, allow_dev_seatbelt=True), platform="darwin")


def test_windows_always_refused():
    with pytest.raises(MultiUserStartupError):
        _check(MultiUserConfig(enabled=True, allow_dev_seatbelt=True), platform="win32")


def test_multi_user_requires_workspaces_root(tmp_path):
    with pytest.raises(MultiUserStartupError, match="workspacesRoot"):
        ensure_workspaces_root(MultiUserConfig(enabled=True))
    ensure_workspaces_root(MultiUserConfig(enabled=True, workspaces_root=tmp_path))
    ensure_workspaces_root(MultiUserConfig())

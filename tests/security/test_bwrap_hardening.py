import shlex
from pathlib import Path

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.sandbox import _bwrap, wrap_command
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Config
from nanobot.multiuser.config import MultiUserConfig


def _tokens(ws: Path, **kwargs) -> list[str]:
    return shlex.split(_bwrap("true", str(ws), str(ws), **kwargs))


def test_bwrap_isolates_pid_ipc_and_uts(tmp_path):
    tokens = _tokens(tmp_path / "ws")
    assert {"--unshare-pid", "--unshare-ipc", "--unshare-uts"} <= set(tokens)
    assert "--unshare-net" not in tokens


def test_bwrap_drops_all_capabilities(tmp_path):
    tokens = _tokens(tmp_path / "ws")
    assert tokens[tokens.index("--cap-drop") + 1] == "ALL"


def test_bwrap_net_none_unshares_network(tmp_path):
    assert "--unshare-net" in _tokens(tmp_path / "ws", net="none")


def test_bwrap_media_dir_replaces_global_media(tmp_path):
    media = tmp_path / "users" / "alice" / "media"
    tokens = _tokens(tmp_path / "ws", media_dir=media)
    binds = [tokens[i + 1] for i, t in enumerate(tokens) if t == "--ro-bind-try"]
    assert str(media.resolve()) in binds
    assert str(get_media_dir().resolve()) not in binds


def test_wrap_command_forwards_net_and_media_dir(tmp_path):
    ws = str(tmp_path / "ws")
    wrapped = wrap_command("bwrap", "true", ws, ws, net="none", media_dir=tmp_path / "m")
    tokens = shlex.split(wrapped)
    assert "--unshare-net" in tokens
    assert str((tmp_path / "m").resolve()) in tokens


def test_multi_user_exec_tool_forces_bwrap_without_network(tmp_path):
    cfg = Config()
    cfg.multi_user = MultiUserConfig(enabled=True, sandbox_net="none")
    media = tmp_path / "media"
    ctx = ToolContext(
        config=cfg.tools, workspace=str(tmp_path), multi_user=cfg.multi_user, media_dir=media,
    )
    tool = ExecTool.create(ctx)
    assert tool.sandbox == "bwrap"
    assert tool.sandbox_net == "none"
    assert tool.sandbox_media_dir == media


def test_multi_user_exec_tool_requires_per_user_media_dir(tmp_path):
    cfg = Config()
    cfg.multi_user = MultiUserConfig(enabled=True)
    ctx = ToolContext(config=cfg.tools, workspace=str(tmp_path), multi_user=cfg.multi_user)
    with pytest.raises(ValueError, match="media_dir"):
        ExecTool.create(ctx)


def test_single_user_exec_tool_keeps_configured_sandbox(tmp_path):
    cfg = Config()
    ctx = ToolContext(config=cfg.tools, workspace=str(tmp_path))
    tool = ExecTool.create(ctx)
    assert tool.sandbox == cfg.tools.exec.sandbox
    assert tool.sandbox_net == "share"

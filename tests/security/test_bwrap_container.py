"""Real bubblewrap isolation checks; run in the deployed container mode via scripts/verify_bwrap_container.sh."""

import os
import shutil
import subprocess

import pytest

from nanobot.agent.tools.sandbox import wrap_command

pytestmark = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")


def _run(workspace, command, **kwargs):
    media = workspace.parent / "media"
    wrapped = wrap_command("bwrap", command, str(workspace), str(workspace), media_dir=media, **kwargs)
    return subprocess.run(wrapped, shell=True, capture_output=True, text=True, timeout=30)


@pytest.fixture
def users(tmp_path):
    root = tmp_path / "users"
    a, b = root / "a" / "workspace", root / "b" / "workspace"
    for ws in (a, b):
        ws.mkdir(parents=True)
    (b / "secret").write_text("b-secret")
    (root / "config.json").write_text("{}")
    return a, b, root


def test_other_users_workspace_is_hidden(users):
    a, b, _ = users
    assert _run(a, f"cat {b}/secret").returncode != 0


def test_gateway_config_is_hidden(users):
    a, _, root = users
    assert _run(a, f"cat {root}/config.json").returncode != 0


def test_host_processes_are_not_visible(users):
    a, _, _ = users
    result = _run(a, "ls /proc | grep -E '^[0-9]+$'")
    assert result.returncode == 0, result.stderr
    assert os.getpid() not in {int(pid) for pid in result.stdout.split()}


def test_sandbox_has_no_capabilities(users):
    a, _, _ = users
    result = _run(a, "grep CapEff /proc/self/status")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[1] == "0000000000000000"


def test_net_none_removes_container_interfaces(users):
    a, _, _ = users
    result = _run(a, "tail -n +3 /proc/net/dev | cut -d: -f1", net="none")
    assert result.returncode == 0, result.stderr
    interfaces = result.stdout.split()
    assert "lo" in interfaces
    assert "eth0" not in interfaces

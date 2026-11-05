"""An account's sandbox can plant links in its workspace; the gateway must not follow them out."""

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils import confined_files
from nanobot.utils.gitstore import GitStore
from nanobot.utils.helpers import sync_workspace_templates

SECRET = "other-account-secret-7f3a"
SKILL = "---\nname: evil\ndescription: planted\n---\n" + SECRET + "\n"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "host" / "secrets.json"
    path.parent.mkdir()
    path.write_text(SECRET, encoding="utf-8")
    return path


def _workspace(tmp_path: Path, *, account: bool) -> Path:
    workspace = tmp_path / "alice" / "workspace"
    workspace.mkdir(parents=True)
    if account:
        confined_files.mark_account_workspace(workspace)
    return workspace


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def test_bootstrap_files_do_not_read_through_escaping_links(tmp_path: Path, outside: Path) -> None:
    workspace = _workspace(tmp_path, account=True)
    for name in ("AGENTS.md", "SOUL.md", "USER.md"):
        _link(workspace / name, outside)

    prompt = ContextBuilder(workspace).build_system_prompt()

    assert SECRET not in prompt


def test_memory_writes_do_not_write_through_escaping_links(tmp_path: Path, outside: Path) -> None:
    workspace = _workspace(tmp_path, account=True)
    planted = tmp_path / "host" / "planted.json"
    store = MemoryStore(workspace)
    for name in ("MEMORY.md", "history.jsonl", ".cursor", ".dream_cursor"):
        _link(store.memory_dir / name, outside)
    _link(workspace / "SOUL.md", outside)
    _link(workspace / "USER.md", planted)  # dangling: must not be created

    assert store.read_memory() == ""
    store.write_memory("memory")
    store.write_soul("soul")
    store.write_user("user")
    store.append_history("turn")
    store.set_last_dream_cursor(5)

    assert outside.read_text(encoding="utf-8") == SECRET
    assert not planted.exists()
    assert store.read_soul() == ""
    assert store.get_last_dream_cursor() == 0


def test_skills_do_not_read_through_escaping_links(tmp_path: Path, outside: Path) -> None:
    workspace = _workspace(tmp_path, account=True)
    outside.write_text(SKILL, encoding="utf-8")
    _link(workspace / "skills" / "evil" / "SKILL.md", outside)
    host_skill_dir = tmp_path / "host" / "skill"
    host_skill_dir.mkdir()
    (host_skill_dir / "SKILL.md").write_text(SKILL.replace("evil", "evildir"), encoding="utf-8")
    _link(workspace / "skills" / "evildir", host_skill_dir)

    loader = SkillsLoader(workspace)

    assert loader.load_skill("evil") is None
    assert loader.load_skill("evildir") is None
    assert SECRET not in loader.load_skills_for_context(["evil", "evildir"])
    assert SECRET not in ContextBuilder(workspace).build_system_prompt()


def test_template_sync_does_not_create_dangling_link_targets(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, account=True)
    planted = tmp_path / "host" / "users.json"
    _link(workspace / "AGENTS.md", planted)

    sync_workspace_templates(workspace, silent=True)

    assert not planted.exists()
    assert (workspace / "SOUL.md").is_file()


def test_account_workspace_runs_no_git_store(tmp_path: Path) -> None:
    # dulwich would run the sandbox-writable .git/hooks and filter drivers in the gateway.
    workspace = _workspace(tmp_path, account=True)

    sync_workspace_templates(workspace, silent=True)

    assert not (workspace / ".git").exists()
    assert not GitStore(workspace, tracked_files=["SOUL.md"]).is_initialized()


def test_account_workspace_regular_files_work(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, account=True)
    (workspace / "SOUL.md").write_text("soul text", encoding="utf-8")
    (workspace / "skills" / "mine").mkdir(parents=True)
    (workspace / "skills" / "mine" / "SKILL.md").write_text(
        "---\nname: mine\ndescription: own\n---\nown body\n", encoding="utf-8",
    )
    store = MemoryStore(workspace)

    store.write_memory("remembered")
    cursor = store.append_history("first")

    assert store.read_memory() == "remembered"
    assert store.get_latest_cursor() == cursor
    assert "soul text" in ContextBuilder(workspace).build_system_prompt()
    assert "own body" in (SkillsLoader(workspace).load_skill("mine") or "")


def test_single_user_workspace_still_follows_links(tmp_path: Path, outside: Path) -> None:
    workspace = _workspace(tmp_path, account=False)
    _link(workspace / "SOUL.md", outside)
    store = MemoryStore(workspace)
    shared_memory = tmp_path / "host" / "MEMORY.md"
    shared_memory.write_text("", encoding="utf-8")
    store.memory_file.symlink_to(shared_memory)

    store.write_memory("shared")

    assert SECRET in ContextBuilder(workspace).build_system_prompt()
    assert shared_memory.read_text(encoding="utf-8") == "shared"

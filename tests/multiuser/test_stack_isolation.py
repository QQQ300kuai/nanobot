from pathlib import Path

from nanobot.agent.tools.shell import ExecTool


async def test_two_accounts_keep_sessions_memory_skills_and_media_apart(harness) -> None:
    root = Path(harness.config.multi_user.workspaces_root)
    await harness.front.publish_inbound(harness.message("alice", "remember alice-secret"))
    await harness.front.publish_inbound(harness.message("bob", "remember bob-secret"))
    await harness.reply("alice-chat")
    await harness.reply("bob-chat")
    alice = await harness.pool.get("alice")
    bob = await harness.pool.get("bob")

    alice_memory = root / "alice" / "workspace" / "memory" / "MEMORY.md"
    assert alice_memory.read_text(encoding="utf-8") == "alice-secret"
    assert (root / "bob" / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "bob-secret"
    assert alice.agent.context.memory.memory_file == alice_memory
    assert alice.agent.context.skills.workspace_skills == root / "alice" / "workspace" / "skills"
    assert not (Path(harness.config.workspace_path) / "memory" / "MEMORY.md").exists()

    assert [item["key"] for item in await alice.sessions.state.list_sessions()] == ["websocket:alice-chat"]
    assert [item["key"] for item in await bob.sessions.state.list_sessions()] == ["websocket:bob-chat"]
    assert await bob.sessions.state.read("websocket:alice-chat") is None

    for stack in (alice, bob):
        media = root / stack.user_id / "media"
        for registry in (stack.agent.tools, stack.agent.subagents._build_tools()):
            exec_tool = registry.get("exec")
            assert isinstance(exec_tool, ExecTool)
            assert exec_tool.sandbox == "bwrap"
            assert exec_tool.sandbox_media_dir == media


async def test_account_turn_ignores_message_supplied_workspace_scope(harness) -> None:
    root = Path(harness.config.multi_user.workspaces_root)
    harness.config.workspace_path.mkdir(parents=True)
    msg = harness.message("alice", "remember scoped")
    msg.metadata["workspace_scope"] = {"project_path": str(harness.config.workspace_path), "access_mode": "full"}

    await harness.front.publish_inbound(msg)
    await harness.reply("alice-chat")

    assert (root / "alice" / "workspace" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "scoped"
    assert not (Path(harness.config.workspace_path) / "memory" / "MEMORY.md").exists()

import asyncio

from nanobot.multiuser.allowlist import UserAllowlist


def test_load_ignores_comments_blank_and_invalid_lines(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("# team\nAlice\n\nbob\n../evil\n  carol  \n", encoding="utf-8")
    allow = UserAllowlist(f)
    assert allow.contains("alice")
    assert allow.contains("Bob")
    assert allow.contains("carol")
    assert not allow.contains("evil")
    assert not allow.contains("dave")


def test_reload_notifies_every_subscriber_once_per_removed_id(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("alice\nbob\n", encoding="utf-8")
    allow = UserAllowlist(f)
    first, second = [], []
    allow.subscribe_removed(first.append)
    allow.subscribe_removed(second.append)

    f.write_text("alice\ncarol\n", encoding="utf-8")
    allow.reload()
    allow.reload()

    assert allow.contains("carol")
    assert not allow.contains("bob")
    assert first == ["bob"]
    assert second == ["bob"]


def test_missing_file_is_empty_allowlist(tmp_path):
    allow = UserAllowlist(tmp_path / "none.txt")
    assert not allow.contains("alice")


async def test_watch_reloads_on_change_and_cancels_cleanly(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("alice\nbob\n", encoding="utf-8")
    allow = UserAllowlist(f)
    removed = []
    allow.subscribe_removed(removed.append)

    task = asyncio.create_task(allow.watch(interval_s=0.01))
    await asyncio.sleep(0.03)
    f.write_text("alice\n", encoding="utf-8")
    for _ in range(100):
        if removed:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert removed == ["bob"]


def test_watch_keeps_entries_while_file_is_missing(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    allow = UserAllowlist(f)
    removed = []
    allow.subscribe_removed(removed.append)

    f.unlink()
    allow._poll()
    assert allow.contains("a")
    assert removed == []

    f.write_text("a\n", encoding="utf-8")
    allow._poll()
    assert allow.contains("a")
    assert not allow.contains("b")
    assert removed == ["b"]


def test_reload_keeps_entries_when_file_is_missing(tmp_path):
    f = tmp_path / "allow.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    allow = UserAllowlist(f)
    removed = []
    allow.subscribe_removed(removed.append)

    f.unlink()
    allow.reload()

    assert allow.contains("a")
    assert allow.contains("b")
    assert removed == []

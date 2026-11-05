from pathlib import Path

import pytest

from nanobot.multiuser.identity import InvalidUserId, UserPaths, validate_user_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Alice", "alice"), ("a", "a"), ("Bob.Smith_1-x", "bob.smith_1-x"), ("a" * 64, "a" * 64)],
)
def test_validate_user_id_accepts_and_lowercases(raw, expected):
    assert validate_user_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["../a", "a/b", "", "a" * 65, ".hidden", "a\x00b", "a..b", "-a", "a b"],
)
def test_validate_user_id_rejects(raw):
    with pytest.raises(InvalidUserId):
        validate_user_id(raw)


def test_user_paths_layout(tmp_path: Path):
    paths = UserPaths(tmp_path, "Alice")
    base = tmp_path / "alice"
    assert paths.workspace == base / "workspace"
    assert paths.config_file == base / "config.json"
    assert paths.secrets_file == base / "secrets.json"
    assert paths.media_dir == base / "media"


def test_user_paths_rejects_invalid_id(tmp_path: Path):
    with pytest.raises(InvalidUserId):
        UserPaths(tmp_path, "../evil")


def test_user_paths_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "users"
    root.mkdir()
    (root / "alice").symlink_to(tmp_path)
    with pytest.raises(InvalidUserId):
        UserPaths(root, "alice")

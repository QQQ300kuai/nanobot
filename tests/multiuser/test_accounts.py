import sqlite3

import pytest

from nanobot.multiuser.accounts import AccountStore, hash_password
from nanobot.multiuser.allowlist import UserAllowlist


@pytest.fixture
def env(tmp_path):
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("alice\nbob\n", encoding="utf-8")
    allow = UserAllowlist(allow_file)
    now = [1000.0]
    store = AccountStore(
        tmp_path / "accounts.db", allow, hash_password("initial-pw"), clock=lambda: now[0]
    )
    return store, allow, allow_file, now


def test_allowlisted_user_logs_in_with_initial_password(env):
    store, *_ = env
    token = store.login("Alice", "initial-pw")
    assert token
    assert store.authenticate(token) == "alice"
    assert store.login("alice", "wrong") is None


def test_unknown_or_invalid_user_cannot_login_and_leaves_no_trace(env, tmp_path):
    store, *_ = env
    assert store.login("mallory", "initial-pw") is None
    assert store.login("../evil", "initial-pw") is None
    assert store.login("", "initial-pw") is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["accounts.db", "allow.txt"]
    with sqlite3.connect(tmp_path / "accounts.db") as db:
        assert db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0


def test_change_password_replaces_old(env):
    store, *_ = env
    assert store.change_password("alice", "initial-pw", "new-pw") is True
    assert store.login("alice", "initial-pw") is None
    assert store.login("alice", "new-pw")
    assert store.login("bob", "initial-pw")


def test_change_password_rejects_wrong_old_password(env):
    store, *_ = env
    assert store.change_password("alice", "nope", "new-pw") is False
    assert store.login("alice", "initial-pw")


def test_revoke_user_invalidates_tokens(env):
    store, *_ = env
    t1 = store.login("alice", "initial-pw")
    t2 = store.login("bob", "initial-pw")
    store.revoke_user("alice")
    assert store.authenticate(t1) is None
    assert store.authenticate(t2) == "bob"


def test_removed_from_allowlist_revokes_immediately(env):
    store, allow, allow_file, _ = env
    token = store.login("bob", "initial-pw")
    allow_file.write_text("alice\n", encoding="utf-8")
    allow.reload()
    assert store.authenticate(token) is None


def test_token_expires_after_ttl(env):
    store, _, _, now = env
    token = store.login("alice", "initial-pw")
    now[0] += 12 * 3600 - 1
    assert store.authenticate(token) == "alice"
    now[0] += 2
    assert store.authenticate(token) is None


def test_authenticate_rejects_garbage(env):
    store, *_ = env
    assert store.authenticate("") is None
    assert store.authenticate("not-a-token") is None


def test_unknown_user_still_pays_for_a_password_verification(env, monkeypatch):
    from nanobot.multiuser import accounts

    store, *_ = env
    calls = []
    real = accounts._verify_password
    monkeypatch.setattr(accounts, "_verify_password", lambda *a: calls.append(a) or real(*a))
    assert store.login("mallory", "initial-pw") is None
    assert store.login("../evil", "initial-pw") is None
    assert store.change_password("mallory", "initial-pw", "x") is False
    assert len(calls) == 3


def test_change_password_revokes_all_existing_tokens(env):
    store, *_ = env
    t1 = store.login("alice", "initial-pw")
    t2 = store.login("alice", "initial-pw")
    assert store.change_password("alice", "initial-pw", "new-pw") is True
    assert store.authenticate(t1) is None
    assert store.authenticate(t2) is None
    assert store.login("alice", "new-pw")


def test_malformed_initial_hash_is_rejected_at_startup(tmp_path):
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("alice\n", encoding="utf-8")
    with pytest.raises(ValueError, match="initial_password_hash"):
        AccountStore(tmp_path / "a.db", UserAllowlist(allow_file), "plaintext")


def _multi_user_config(tmp_path):
    from nanobot.multiuser.config import MultiUserConfig

    users = tmp_path / "users.txt"
    users.write_text("alice\n", encoding="utf-8")
    return MultiUserConfig(enabled=True, users_file=users)


def test_open_account_store_uses_env_hash_and_a_private_db(tmp_path):
    from nanobot.multiuser.accounts import INITIAL_PASSWORD_HASH_ENV, open_account_store

    data_dir = tmp_path / "data"
    store = open_account_store(
        _multi_user_config(tmp_path),
        data_dir=data_dir,
        environ={INITIAL_PASSWORD_HASH_ENV: hash_password("initial-pw")},
    )
    assert store.login("alice", "initial-pw")
    assert (data_dir / "accounts.db").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("environ", [{}, {"NANOBOT_INITIAL_PASSWORD_HASH": "plaintext"}])
def test_open_account_store_refuses_a_missing_or_malformed_env_hash(tmp_path, environ):
    from nanobot.multiuser.accounts import open_account_store
    from nanobot.multiuser.guard import MultiUserStartupError

    with pytest.raises(MultiUserStartupError, match="NANOBOT_INITIAL_PASSWORD_HASH"):
        open_account_store(_multi_user_config(tmp_path), data_dir=tmp_path / "data", environ=environ)


def test_open_account_store_requires_a_users_file(tmp_path):
    from nanobot.multiuser.accounts import INITIAL_PASSWORD_HASH_ENV, open_account_store
    from nanobot.multiuser.config import MultiUserConfig
    from nanobot.multiuser.guard import MultiUserStartupError

    with pytest.raises(MultiUserStartupError, match="usersFile"):
        open_account_store(
            MultiUserConfig(enabled=True),
            data_dir=tmp_path / "data",
            environ={INITIAL_PASSWORD_HASH_ENV: hash_password("initial-pw")},
        )

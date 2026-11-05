"""Password accounts and server-side session tokens for allowlisted users."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from nanobot.multiuser.allowlist import UserAllowlist
from nanobot.multiuser.config import MultiUserConfig
from nanobot.multiuser.guard import MultiUserStartupError
from nanobot.multiuser.identity import InvalidUserId, validate_user_id

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16
DEFAULT_TOKEN_TTL_S = 12 * 3600
INITIAL_PASSWORD_HASH_ENV = "NANOBOT_INITIAL_PASSWORD_HASH"
ACCOUNTS_DB_NAME = "accounts.db"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """Return ``scrypt$n$r$p$salt$key`` (base64 fields) for a password."""
    salt = secrets.token_bytes(_SALT_LEN)
    key = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(
        password.encode("utf-8")
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        key = base64.b64decode(key_b64, validate=True)
        kdf = Scrypt(salt=salt, length=len(key), n=int(n), r=int(r), p=int(p))
        kdf.verify(password.encode("utf-8"), key)  # constant-time comparison
        return True
    except (ValueError, InvalidKey):
        return False


def _is_well_formed(stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        base64.b64decode(salt_b64, validate=True)
        base64.b64decode(key_b64, validate=True)
        return scheme == "scrypt" and min(int(n), int(r), int(p)) > 0
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountStore:
    """Verifies passwords and issues, checks and revokes session tokens."""

    def __init__(
        self,
        db_path: Path,
        allowlist: UserAllowlist,
        initial_password_hash: str,
        *,
        token_ttl_s: float = DEFAULT_TOKEN_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _is_well_formed(initial_password_hash):
            raise ValueError("initial_password_hash is not a valid hash_password() value")
        self._allowlist = allowlist
        self._initial_hash = initial_password_hash
        self._ttl = token_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Password hashes and token hashes stay owner-only; SQLite journals inherit this mode.
        os.close(os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(db_path, 0o600)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS passwords (user_id TEXT PRIMARY KEY, hash TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS tokens ("
                "token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
        allowlist.subscribe_removed(self.revoke_user)

    @property
    def allowlist(self) -> UserAllowlist:
        return self._allowlist

    def _current_hash(self, user_id: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT hash FROM passwords WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row else self._initial_hash

    def _verified_user(self, user_id: str, password: str) -> str | None:
        try:
            uid = validate_user_id(user_id)
        except InvalidUserId:
            uid = None
        if uid is None or not self._allowlist.contains(uid):
            # Spend the same scrypt time so response timing does not reveal allowlist membership.
            _verify_password(password, self._initial_hash)
            return None
        if not _verify_password(password, self._current_hash(uid)):
            return None
        return uid

    def login(self, user_id: str, password: str) -> str | None:
        uid = self._verified_user(user_id, password)
        if uid is None:
            return None
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock, self._db:
            self._db.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))
            self._db.execute(
                "INSERT INTO tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), uid, now + self._ttl),
            )
        return token

    def change_password(self, user_id: str, old: str, new: str) -> bool:
        uid = self._verified_user(user_id, old)
        if uid is None:
            return False
        new_hash = hash_password(new)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO passwords (user_id, hash) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET hash = excluded.hash",
                (uid, new_hash),
            )
            self._db.execute("DELETE FROM tokens WHERE user_id = ?", (uid,))
        return True

    def authenticate(self, token: str) -> str | None:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT user_id, expires_at FROM tokens WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        user_id, expires_at = row
        if expires_at <= self._clock() or not self._allowlist.contains(user_id):
            return None
        return user_id

    def is_member(self, user_id: str) -> bool:
        """Whether ``user_id`` is a valid id currently on the allowlist."""
        return self._allowlist.contains(user_id)

    def revoke_user(self, user_id: str) -> None:
        try:
            uid = validate_user_id(user_id)
        except InvalidUserId:
            return
        with self._lock, self._db:
            self._db.execute("DELETE FROM tokens WHERE user_id = ?", (uid,))


def open_account_store(
    cfg: MultiUserConfig,
    *,
    data_dir: Path,
    environ: Mapping[str, str] = os.environ,
) -> AccountStore:
    """Open the gateway account store, refusing to start without a valid initial password hash."""
    if cfg.users_file is None:
        raise MultiUserStartupError("Multi-user mode requires multiUser.usersFile (the user allowlist).")
    initial_hash = environ.get(INITIAL_PASSWORD_HASH_ENV, "").strip()
    if not _is_well_formed(initial_hash):
        raise MultiUserStartupError(
            f"Multi-user mode requires {INITIAL_PASSWORD_HASH_ENV} to hold a scrypt hash "
            "produced by nanobot.multiuser.accounts.hash_password()."
        )
    allowlist = UserAllowlist(Path(cfg.users_file).expanduser())
    return AccountStore(Path(data_dir) / ACCOUNTS_DB_NAME, allowlist, initial_hash)

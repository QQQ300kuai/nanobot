from pathlib import Path
from typing import Literal

from pydantic import Field

from nanobot.config_base import Base


class MultiUserConfig(Base):
    """Multi-user mode configuration.

    The initial password hash is read from ``NANOBOT_INITIAL_PASSWORD_HASH`` and never stored here.
    """
    enabled: bool = False
    users_file: Path | None = None
    admins: list[str] = Field(default_factory=list)
    allow_dev_seatbelt: bool = False
    catalog_file: Path | None = None
    workspaces_root: Path | None = None
    sandbox_net: Literal["share", "none"] = "none"
    # Account runtimes kept open at once (soft cap: busy runtimes are never closed) and their idle lifetime.
    max_active_users: int = Field(default=32, ge=1)
    user_idle_ttl_s: float = Field(default=1800.0, gt=0)

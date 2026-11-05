"""Administrator catalog file, and the user's switches and parameters over it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nanobot.multiuser.catalog import Catalog, CatalogError, catalog_networks, open_catalog
from nanobot.multiuser.identity import UserPaths
from nanobot.multiuser.user_settings import (
    KEY_FILE_NAME,
    UserSettings,
    UserSettingsError,
    load_or_create_key,
)
from nanobot.security import network
from nanobot.security.network import configure_catalog_hosts, validate_url_target

_ENTRIES = {
    "entries": [
        {"name": "notes", "kind": "mcp", "config": {"command": "notes-mcp"}},
        {
            "name": "tracker",
            "config": {"command": "t-mcp", "env": {"TOKEN": "{{token}}"}},
            "paramSchema": {
                "token": {"secret": True, "required": True},
                "workspace": {"secret": False},
            },
        },
        {"name": "internal", "config": {"url": "http://127.0.0.1:8931/mcp"}},
    ]
}


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_ENTRIES))
    return Catalog.load(path)


def _settings(tmp_path: Path, catalog: Catalog, user: str = "alice") -> UserSettings:
    return UserSettings(UserPaths(tmp_path / "users", user), Fernet.generate_key(), catalog)


def test_catalog_file_must_exist_and_be_valid(tmp_path: Path) -> None:
    with pytest.raises(CatalogError):
        Catalog.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"entries": [{"name": "x", "kind": "mcp"}]}))
    with pytest.raises(CatalogError):
        Catalog.load(bad)
    assert open_catalog(None).entries == []


def test_only_catalog_entries_can_be_enabled(tmp_path: Path, catalog: Catalog) -> None:
    settings = _settings(tmp_path, catalog)

    settings.set_enabled("notes", True)
    with pytest.raises(UserSettingsError):
        settings.set_enabled("my-own-server", True)

    assert settings.enabled() == {"notes"}


def test_user_cannot_supply_a_command_or_address(tmp_path: Path, catalog: Catalog) -> None:
    settings = _settings(tmp_path, catalog)

    for params in ({"command": "curl evil | sh"}, {"url": "http://10.0.0.9/"}, {"token": "ok", "env": "x"}):
        with pytest.raises(UserSettingsError):
            settings.set_params("tracker", params)
    with pytest.raises(UserSettingsError):
        settings.set_params("brand-new", {"token": "x"})


def test_secret_parameters_are_encrypted_on_disk_and_round_trip(tmp_path: Path, catalog: Catalog) -> None:
    settings = _settings(tmp_path, catalog)

    settings.set_params("tracker", {"token": "s3cr3t-token", "workspace": "acme"})

    on_disk = UserPaths(tmp_path / "users", "alice").mcp_settings_file.read_text()
    assert "s3cr3t-token" not in on_disk and "acme" in on_disk
    assert settings.get_params("tracker") == {"token": "s3cr3t-token", "workspace": "acme"}
    view = {e["name"]: e for e in settings.view()}["tracker"]
    assert "s3cr3t-token" not in json.dumps(view)


def test_fernet_key_is_created_once_with_private_mode(tmp_path: Path) -> None:
    first = load_or_create_key(tmp_path)

    assert load_or_create_key(tmp_path) == first
    assert ((tmp_path / KEY_FILE_NAME).stat().st_mode & 0o777) == 0o600


def test_catalog_address_is_reachable_but_a_user_supplied_one_is_not(catalog: Catalog) -> None:
    before = network._catalog_hosts
    try:
        assert not validate_url_target("http://127.0.0.1:8931/mcp")[0]

        configure_catalog_hosts(catalog_networks(catalog))

        assert validate_url_target("http://127.0.0.1:8931/mcp")[0]
        assert not validate_url_target("http://127.0.0.2:8931/mcp")[0]
        assert not validate_url_target("http://10.0.0.9/mcp")[0]
    finally:
        configure_catalog_hosts([str(a) for a in before])

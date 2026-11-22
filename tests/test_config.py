"""Ticket #62: the `[storage].path` config file."""

from pathlib import Path

import pytest

from src.mud.config import ConfigError, load_config


def test_defaults_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.storage.path == Path("data/mud.db")


def test_mud_toml_is_used_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mud.toml").write_text(
        '[storage]\npath = "var/mud.db"\n', encoding="utf-8"
    )

    config = load_config()

    assert config.storage.path == Path("var/mud.db")


def test_explicit_config_path_is_read(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text('[storage]\npath = "srv/mud.db"\n', encoding="utf-8")

    config = load_config(path)

    assert config.storage.path == Path("srv/mud.db")


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_storage_must_be_a_table(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('storage = "not a table"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="storage"):
        load_config(path)


def test_storage_path_must_be_a_non_empty_string(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[storage]\npath = 3\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="path"):
        load_config(path)

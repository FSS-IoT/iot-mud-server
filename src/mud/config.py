"""Runtime configuration loaded from TOML.

The default config file is `mud.toml` in the working directory. A missing
default file is fine; a missing explicitly requested file is an error.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("mud.toml")
DEFAULT_DB = Path("data/mud.db")
# The product world is YAML (SPEC v2.1.0 criterion 5). The path is relative to
# the repository root; `load_demo_village` resolves it there, because that
# loader owns world discovery and a relative path alone would depend on cwd.
# A future config ticket can expose `[world].path` without moving the default.
DEFAULT_WORLD = Path("data/worlds/starting_village.yaml")


class ConfigError(ValueError):
    """A config file is missing or malformed."""


@dataclass(frozen=True)
class StorageConfig:
    path: Path = DEFAULT_DB


@dataclass(frozen=True)
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Load `[storage].path`, or the built-in default when no file exists."""
    if path is None:
        candidate = DEFAULT_CONFIG
        if not candidate.exists():
            return Config()
        path = candidate

    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    storage = data.get("storage", {})
    if not isinstance(storage, dict):
        raise ConfigError(f"{path}: [storage] must be a table")

    raw_path = storage.get("path", str(DEFAULT_DB))
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"{path}: [storage].path must be a non-empty string")

    return Config(storage=StorageConfig(Path(raw_path)))

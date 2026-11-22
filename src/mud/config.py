"""Runtime configuration loaded from TOML.

The default config file is `mud.toml` in the working directory. A missing
default file is fine; a missing explicitly requested file is an error.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("mud.toml")
DEFAULT_DB = Path("data/mud.db")
DEFAULT_AUTH_MAX_ATTEMPTS = 20
DEFAULT_AUTH_WINDOW_SECONDS = 60.0
# Command limits are separate from the auth-failure budget (#70). The bucket
# starts full, so a client may spend the burst before the refill rate matters.
DEFAULT_COMMANDS_PER_SECOND = 10.0
DEFAULT_COMMAND_BURST = 20
# The owner's open-question default: an authenticated client with no input for
# 15 minutes is put to sleep in place (#72).
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0
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
class AuthConfig:
    """The auth-attempt limiter's window and failure budget.

    The budget is per connection and per window, never per character name
    (SPEC v2.1.1 criterion 3). Defaults allow a legitimate client a dozen
    fumbled keys while making online guessing over scrypt-cost hashes slow.
    """

    max_attempts: int = DEFAULT_AUTH_MAX_ATTEMPTS
    window_seconds: float = DEFAULT_AUTH_WINDOW_SECONDS


@dataclass(frozen=True)
class LimitsConfig:
    """Runtime limits for commands and idle sessions (#85).

    `commands_per_second` and `command_burst` describe one token bucket per
    connection: the bucket starts with `command_burst` tokens, each command
    spends one, and `commands_per_second` tokens refill per second. The auth
    limiter in `AuthConfig` is separate and unchanged.

    `idle_timeout_seconds` is measured from the last client message. An idle
    authenticated character sleeps in place (#72) and its socket closes with
    `4004`; a guest session is not subject to the sleep path.
    """

    commands_per_second: float = DEFAULT_COMMANDS_PER_SECOND
    command_burst: int = DEFAULT_COMMAND_BURST
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def _positive_number(path: Path, section: dict, section_name: str,
                     key: str, default: float) -> float:
    raw = section.get(key, default)
    if (isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(raw)
            or raw <= 0):
        raise ConfigError(
            f"{path}: [{section_name}].{key} must be a positive number")
    return float(raw)


def load_config(path: str | Path | None = None) -> Config:
    """Load `[storage]`, `[auth]`, and `[limits]`, or the built-in defaults."""
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

    auth = data.get("auth", {})
    if not isinstance(auth, dict):
        raise ConfigError(f"{path}: [auth] must be a table")

    raw_attempts = auth.get("max_attempts", DEFAULT_AUTH_MAX_ATTEMPTS)
    if (isinstance(raw_attempts, bool)
            or not isinstance(raw_attempts, int)
            or raw_attempts < 1):
        raise ConfigError(
            f"{path}: [auth].max_attempts must be a positive integer")

    raw_window = auth.get("window_seconds", DEFAULT_AUTH_WINDOW_SECONDS)
    if (isinstance(raw_window, bool)
            or not isinstance(raw_window, (int, float))
            or not math.isfinite(raw_window)
            or raw_window <= 0):
        raise ConfigError(
            f"{path}: [auth].window_seconds must be a positive number")

    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError(f"{path}: [limits] must be a table")

    raw_burst = limits.get("command_burst", DEFAULT_COMMAND_BURST)
    if (isinstance(raw_burst, bool)
            or not isinstance(raw_burst, int)
            or raw_burst < 1):
        raise ConfigError(
            f"{path}: [limits].command_burst must be a positive integer")

    return Config(
        storage=StorageConfig(Path(raw_path)),
        auth=AuthConfig(
            max_attempts=raw_attempts,
            window_seconds=float(raw_window),
        ),
        limits=LimitsConfig(
            commands_per_second=_positive_number(
                path, limits, "limits", "commands_per_second",
                DEFAULT_COMMANDS_PER_SECOND),
            command_burst=raw_burst,
            idle_timeout_seconds=_positive_number(
                path, limits, "limits", "idle_timeout_seconds",
                DEFAULT_IDLE_TIMEOUT_SECONDS),
        ),
    )

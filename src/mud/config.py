"""Runtime configuration loaded from TOML.

The default config file is `mud.toml` in the working directory. A missing
default file is fine; a missing explicitly requested file is an error.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("mud.toml")
DEFAULT_DB = Path("data/mud.db")
DEFAULT_ADMIN_HOST = "127.0.0.1"
DEFAULT_ADMIN_PORT = 8766
ADMIN_TOKEN_ENV = "MUD_ADMIN_TOKEN"
DEFAULT_AUTH_MAX_ATTEMPTS = 20
DEFAULT_AUTH_WINDOW_SECONDS = 60.0
# Command limits are separate from the auth-failure budget (#70). The bucket
# starts full, so a client may spend the burst before the refill rate matters.
DEFAULT_COMMANDS_PER_SECOND = 10.0
DEFAULT_COMMAND_BURST = 20
# The owner's open-question default: an authenticated client with no input for
# 15 minutes is put to sleep in place (#72).
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0
# The product world is a YAML world directory: `world.yaml` metadata plus one
# area file per region under `areas/` (#80). The path is relative to the
# repository root; `load_demo_village` resolves it there, because that loader
# owns world discovery and a relative path alone would depend on cwd. The
# frozen five-room conformance fixture stays at
# `data/worlds/starting_village.yaml` for the legacy loader tests.
# A future config ticket can expose `[world].path` without moving the default.
DEFAULT_WORLD = Path("data/worlds/club_world")


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
class AdminConfig:
    """The experimental browser admin console and world editor.

    Disabled by default. The default bind is loopback; the console is not a
    public service and must never be exposed to the internet. `token` is
    deliberately hidden from `repr` so a config dump cannot leak it. The
    `MUD_ADMIN_TOKEN` environment variable overrides the file value.
    """

    enabled: bool = False
    host: str = DEFAULT_ADMIN_HOST
    port: int = DEFAULT_ADMIN_PORT
    token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FeaturesConfig:
    """Optional v2.1 behavior switches (#84).

    `effects` is the client-delivery switch for authored effects. Effects are
    always published on the engine bus and appended to `event_log`; with
    `False` (the default until the effect-consuming clients and devices
    exist), no client receives an `effect` event and gameplay is identical to
    a run with the block absent. It never changes engine state.
    """

    effects: bool = False


@dataclass(frozen=True)
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)


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
    """Load `[storage]`, `[auth]`, `[limits]`, `[features]`, `[admin]`.

    A missing default file returns built-in defaults (the admin console is
    off). `MUD_ADMIN_TOKEN` always wins over `[admin].token`.
    """
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

    features = data.get("features", {})
    if not isinstance(features, dict):
        raise ConfigError(f"{path}: [features] must be a table")

    raw_effects = features.get("effects", False)
    if not isinstance(raw_effects, bool):
        raise ConfigError(
            f"{path}: [features].effects must be true or false")

    admin = data.get("admin", {})
    if not isinstance(admin, dict):
        raise ConfigError(f"{path}: [admin] must be a table")

    raw_enabled = admin.get("enabled", False)
    if not isinstance(raw_enabled, bool):
        raise ConfigError(f"{path}: [admin].enabled must be true or false")

    raw_host = admin.get("host", DEFAULT_ADMIN_HOST)
    if not isinstance(raw_host, str) or not raw_host.strip():
        raise ConfigError(
            f"{path}: [admin].host must be a non-empty string")

    raw_admin_port = admin.get("port", DEFAULT_ADMIN_PORT)
    if (isinstance(raw_admin_port, bool)
            or not isinstance(raw_admin_port, int)
            or not 1 <= raw_admin_port <= 65535):
        raise ConfigError(
            f"{path}: [admin].port must be an integer between 1 and 65535")

    raw_token = admin.get("token")
    if raw_token is not None and not isinstance(raw_token, str):
        raise ConfigError(f"{path}: [admin].token must be a string")
    token = raw_token.strip() if isinstance(raw_token, str) else None
    # The environment is the strong override: a deployment can keep the
    # secret out of mud.toml entirely. An empty value counts as unset.
    env_token = os.environ.get(ADMIN_TOKEN_ENV)
    if env_token is not None:
        token = env_token.strip() or None

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
        features=FeaturesConfig(effects=raw_effects),
        admin=AdminConfig(
            enabled=raw_enabled,
            host=raw_host.strip(),
            port=raw_admin_port,
            token=token,
        ),
    )

"""Shared protocol-v2 types for the server and its adapter."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeGuard

PROTOCOL_VERSION = 2

# The changelog label for the current additive release inside protocol 2.
PROTOCOL_RELEASE = "2.1"

# `MAJOR.MINOR.PATCH`: MAJOR is the protocol, MINOR is the shared
# client-facing feature level, PATCH is the server-only counter. See
# docs/PROTOCOL.md and tests/test_protocol_hello.py.
SERVER_VERSION = "2.1.6"
FEATURE_LEVEL = 1

# Additive names a client may use. Unknown names are ignored. A capability
# means the server implements it, not that a given world uses it.
CAPABILITIES = (
    "auth.key",
    "inventory.capacity",
    "limits.commands",
    "idle.sleep",
    "npc.dialogue",
    "npc.wander",
    "locks",
)

# `hello.data.server_commit` when no commit can be resolved.
UNKNOWN_SERVER_COMMIT = "unknown"

_SERVER_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def is_server_commit(value: object) -> TypeGuard[str]:
    """True when `value` is a 40-character lowercase hex git commit."""
    return (
        isinstance(value, str)
        and _SERVER_COMMIT_RE.fullmatch(value) is not None
    )


# Event names the v2 server may emit. Kept here so tests and the server share
# one list instead of spelling strings in several places.
SESSION_EVENTS = {
    "hello", "session.ok", "auth.ok", "auth.error", "error", "notice",
    "shutdown",
}
ROOM_EVENTS = {"room.state", "room.enter", "room.leave"}
SOCIAL_EVENTS = {"player.said", "player.emoted", "player.told", "who.list"}
OBJECT_EVENTS = {
    "object.taken", "object.dropped", "object.opened", "object.closed",
    "object.put", "object.got", "object.read", "object.locked",
    "object.unlocked", "player.inventory", "exit.locked", "exit.unlocked",
}

ERROR_CODES = {
    "unknown_command", "bad_args", "ambiguous", "ambiguous_target",
    "not_found", "not_here", "not_portable", "container_closed",
    "container_cycle", "no_exit", "locked", "already_open",
    "already_closed", "too_heavy", "malformed_message", "unknown_message",
    "not_joined", "already_joined", "name_taken", "invalid_name",
    "invalid_key", "not_authenticated", "not_awake", "rate_limited",
    "internal", "unsupported_version",
}

# The auth methods `hello.data.auth_modes` may advertise.
AUTH_MODES = ("key",)


@dataclass
class Outbound:
    """One event the server should send.

    `target` is "actor" (the command's sender), "room" (everyone else in the
    room), or "player" (one named character, for private social traffic).
    `room_name` chooses the room for a broadcast; None means the actor's
    current room. `player_name` chooses the character for a "player" push and
    is ignored by the other targets. `direct` controls whether the request id
    is echoed; a private push to someone else never is.
    """

    type: str
    data: dict[str, Any]
    target: Literal["actor", "room", "player"] = "actor"
    room_name: str | None = None
    direct: bool = True
    player_name: str | None = None


@dataclass
class CommandResult:
    """The adapter's answer to one input: zero or more outbound events."""

    events: list[Outbound] = field(default_factory=list)


@dataclass
class SemanticEvent:
    """One wire-agnostic thing a command did, for an adapter to shape.

    The engine decides *what happened* (a coin was taken, a move succeeded, an
    exit was locked). It records that as a `SemanticEvent`; a protocol adapter
    adds only transport concerns — the actor/room split, the `self` flag, a
    room broadcast (#130).

    `scope` says who should see it:

    * ``actor`` — only the command's sender.
    * ``room`` — the sender and everyone else in the room.
    * ``room_others`` — everyone in the room except the sender (a leave/enter).
    * ``player`` — the sender and the one character named in ``data["player"]``.

    ``data`` is the game content: object/actor references, the presentation
    text for each perspective, and any room the event belongs to. It stays
    free of wire fields such as ``self`` or a target name.
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    scope: Literal["actor", "room", "room_others", "player"] = "actor"
    # False for an unsolicited push that is not a reply to the request id (the
    # inventory relist after a put/get). The adapter passes this to `Outbound`.
    direct: bool = True


@dataclass
class CommandOutcome:
    """The engine's typed answer to one command (#130).

    ``process_command`` is a string shim over this that still requires an
    actor (a `Player` or a registered name). ``Engine.execute`` returns the
    typed result: the presentation reply, a typed success-or-error status with
    the error code, and the semantic events the command produced. An adapter
    maps it to wire events without parsing the command again or diffing world
    state.

    ``error_data`` carries the extra fields a typed refusal includes on the
    wire — `ambiguous` candidates, the `too_heavy` capacity/used numbers, the
    `container_cycle` object/container. The adapter merges it into the error
    payload.
    """

    reply: str = ""
    status: Literal["success", "error"] = "success"
    error_code: str | None = None
    error_data: dict[str, Any] = field(default_factory=dict)
    events: list[SemanticEvent] = field(default_factory=list)


def utc_now() -> str:
    """ISO 8601 UTC timestamp with a trailing Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def envelope(
    type_: str,
    data: dict[str, Any],
    *,
    seq: int,
    event_id: str | None = None,
    msg_id: Any = None,
) -> dict[str, Any]:
    """Build one server-to-client v2 envelope."""
    message = {
        "v": PROTOCOL_VERSION,
        "type": type_,
        "event_id": event_id or f"{type_}-{uuid.uuid4().hex}",
        "seq": seq,
        "ts": utc_now(),
        "data": data,
    }
    if msg_id is not None:
        message["id"] = msg_id
    return message

"""Shared protocol-v2 types for the server and its adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

PROTOCOL_VERSION = 2

# Event names the v2.0 server may emit. Kept here so tests and the server share
# one list instead of spelling strings in several places.
SESSION_EVENTS = {"hello", "session.ok", "error", "notice", "shutdown"}
ROOM_EVENTS = {"room.state", "room.enter", "room.leave"}
SOCIAL_EVENTS = {"player.said", "player.emoted", "player.told", "who.list"}
OBJECT_EVENTS = {
    "object.taken", "object.dropped", "object.opened", "object.closed",
    "object.put", "object.got", "object.read", "object.locked",
    "object.unlocked", "player.inventory", "exit.locked", "exit.unlocked",
}

ERROR_CODES = {
    "unknown_command", "bad_args", "ambiguous_target", "not_found",
    "not_here", "not_portable", "container_closed", "no_exit", "locked",
    "already_open", "already_closed", "malformed_message", "unknown_message",
    "not_joined", "already_joined", "name_taken", "not_authenticated",
    "rate_limited", "internal", "unsupported_version",
}


@dataclass
class Outbound:
    """One event the server should send.

    `target` is "actor" (the command's sender) or "room" (everyone else in the
    room). `room_name` chooses the room for a broadcast; None means the actor's
    current room. `direct` controls whether the request id is echoed.
    """

    type: str
    data: dict[str, Any]
    target: Literal["actor", "room"] = "actor"
    room_name: str | None = None
    direct: bool = True


@dataclass
class CommandResult:
    """The adapter's answer to one input: zero or more outbound events."""

    events: list[Outbound] = field(default_factory=list)


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

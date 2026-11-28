"""Player feedback: validation, rate limiting, context, and storage (#177).

A feedback `text` is the first free-form user input the server stores. Treat it
as untrusted everywhere:

- validate it on arrival (str, stripped, bounded, no control characters);
- bind it as a SQL value, never interpolate it;
- never log the raw text, only the id, length, and category;
- return `feedback.saved {id}` only, never the text or the captured context.

The module also owns the per-character in-memory context captured around each
command (bounded to the last 10), and the write path that stores one row.
"""

from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from typing import Any

from src.mud.storage.keys import object_key

MAX_FEEDBACK_LENGTH = 2000
FEEDBACK_CATEGORIES = ("bug", "idea", "other")
DEFAULT_FEEDBACK_CATEGORY = "other"

# How many commands the in-memory context keeps, and how long each command
# text may be before it is truncated.
MAX_CONTEXT_COMMANDS = 10
MAX_COMMAND_TEXT = 200

# The per-character feedback rate limit: one per minute, five per hour.
FEEDBACK_MIN_INTERVAL = 60.0
FEEDBACK_HOURLY_MAX = 5
FEEDBACK_HOUR = 3600.0


class FeedbackValidationError(ValueError):
    """A feedback field is missing, mistyped, or out of bounds.

    `code` is the typed error a client sees: `bad_args` or `too_long`.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_feedback_text(value: object) -> str:
    """Return the stripped, validated text, or raise `FeedbackValidationError`.

    The text must be a string; leading and trailing whitespace is stripped;
    empty is refused; over `MAX_FEEDBACK_LENGTH` code points is `too_long`.
    `\\n` and `\\t` are allowed; every other control character (NUL, ESC, DEL,
    and the C1 block) is `bad_args`.
    """
    if not isinstance(value, str):
        raise FeedbackValidationError(
            "bad_args", "Feedback text must be a string.")
    text = value.strip()
    if not text:
        raise FeedbackValidationError(
            "bad_args", "Feedback text must not be empty.")
    if len(text) > MAX_FEEDBACK_LENGTH:
        raise FeedbackValidationError(
            "too_long",
            f"Feedback text must be {MAX_FEEDBACK_LENGTH} characters or fewer.")
    for ch in text:
        if ch in "\n\t":
            continue
        if unicodedata.category(ch) == "Cc":
            raise FeedbackValidationError(
                "bad_args",
                "Feedback text must not contain control characters.")
    return text


def validate_feedback_category(value: object) -> str:
    """Return the category, defaulting to `other`, or raise for an unknown one."""
    if value is None:
        return DEFAULT_FEEDBACK_CATEGORY
    if not isinstance(value, str) or value not in FEEDBACK_CATEGORIES:
        raise FeedbackValidationError(
            "bad_args",
            f"Feedback category must be one of {', '.join(FEEDBACK_CATEGORIES)}.")
    return value


class FeedbackLimiter:
    """A per-character, in-memory feedback rate limit (#177).

    The limit is one send per `FEEDBACK_MIN_INTERVAL` and
    `FEEDBACK_HOURLY_MAX` sends per hour, measured on an injected clock so
    tests advance time instead of sleeping. `allowed` prunes and decides;
    `record` counts a successful save.
    """

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock if clock is not None else time.monotonic
        self._last: dict[str, float] = {}
        self._hourly: dict[str, list[float]] = {}

    def allowed(self, name: str) -> bool:
        now = self._clock()
        last = self._last.get(name)
        if last is not None and now - last < FEEDBACK_MIN_INTERVAL:
            return False
        recent = [
            at for at in self._hourly.get(name, [])
            if now - at < FEEDBACK_HOUR
        ]
        self._hourly[name] = recent
        return len(recent) < FEEDBACK_HOURLY_MAX

    def record(self, name: str) -> None:
        now = self._clock()
        self._last[name] = now
        recent = [
            at for at in self._hourly.get(name, [])
            if now - at < FEEDBACK_HOUR
        ]
        recent.append(now)
        self._hourly[name] = recent


def redact_command(text: str) -> str:
    """The context copy of one command: bounded, with `tell` bodies redacted.

    A `tell <player> <body>` becomes `tell <player> [redacted]`; nothing else
    reads the body. Other commands keep their text, truncated to
    `MAX_COMMAND_TEXT` code points.
    """
    stripped = text.strip()
    words = stripped.split()
    if words and words[0].lower() == "tell":
        target = words[1] if len(words) > 1 else None
        return f"tell {target} [redacted]" if target else "tell [redacted]"
    return stripped[:MAX_COMMAND_TEXT]


def record_command(
    session: Any,
    *,
    text: str,
    room_id: str | None,
    event_types: list[str],
    now: Callable[[], str],
) -> None:
    """Append one command to the session's bounded in-memory context."""
    entry = {
        "ts": now(),
        "text": redact_command(text),
        "room_id": room_id,
        "events": sorted(set(event_types)),
    }
    session.command_history.append(entry)
    del session.command_history[:-MAX_CONTEXT_COMMANDS]
    session.command_count += 1


def _inventory_snapshot(engine: Any, player: Any) -> dict[str, Any]:
    if player is None:
        return {"items": [], "capacity": 0.0, "used": 0.0}
    items = []
    for item in player.contents:
        items.append({
            "id": object_key(item),
            "name": item.name,
            "weight": round(float(getattr(item, "weight", 0.0) or 0.0), 3),
            "lit": bool(getattr(item, "lit", False)),
        })
    return {
        "items": items,
        "capacity": round(engine.carry_capacity(player), 3),
        "used": round(engine.carry_used(player), 3),
    }


def build_context(engine: Any, session: Any) -> dict[str, Any]:
    """The context captured with one report: commands, inventory, session stats."""
    player = engine.get_player(session.player_name)
    return {
        "commands": list(session.command_history),
        "inventory": _inventory_snapshot(engine, player),
        "session": {
            "age_seconds": round(session.clock() - session.joined_at, 3),
            "command_count": session.command_count,
            "asleep": bool(player.asleep) if player is not None else False,
        },
    }


def record_feedback(
    conn: sqlite3.Connection,
    *,
    player_name: str,
    text: str,
    category: str,
    room_id: str | None,
    room_name: str | None,
    world_epoch: str | None,
    server_commit: str | None,
    context: dict[str, Any],
    now: str,
) -> int:
    """Insert one feedback row with bound parameters only; return its id.

    The text and the context JSON are values, never identifiers or SQL. The
    id is the only thing a caller may echo or log.
    """
    body = json.dumps(context, sort_keys=True, ensure_ascii=False, allow_nan=False)
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback(player_name, text, category, room_id, "
            "room_name, world_epoch, server_commit, context, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (player_name, text, category, room_id, room_name, world_epoch,
             server_commit, body, now),
        )
        return int(cur.lastrowid)


def escape_controls(text: str) -> str:
    """Return `text` with every control character shown as a visible escape.

    Used by `admin feedback show` (and the list preview) so a stored NUL or ESC
    cannot rewrite the operator's terminal.
    """
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch) == "Cc":
            out.append(repr(ch)[1:-1])
        else:
            out.append(ch)
    return "".join(out)

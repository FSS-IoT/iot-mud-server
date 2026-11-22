"""Stable ids shared by the database and the wire.

A reload has to put an object back where the client saw it, so both sides must
derive an id the same way. Rooms use the authored `item_id`, else a slug of the
display name. Objects prefer their per-instance id (the loader sets it from the
world file), then the authored id, then the slug.
"""

from __future__ import annotations

import re
from typing import Any


def slug(value: str) -> str:
    """A lowercase, underscore-joined form of a display name."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "object"


def room_key(room: Any) -> str:
    """The id a room is stored under."""
    return getattr(room, "item_id", None) or slug(room.name)


def object_key(item: Any) -> str:
    """The id an object is stored under."""
    return (
        getattr(item, "instance_id", None)
        or getattr(item, "item_id", None)
        or slug(item.name)
    )

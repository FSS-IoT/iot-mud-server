"""The effect trigger seam (#84).

Effects are authored in the YAML world and validated by `models.py` and
`validator.py`: a trigger from `EFFECT_TRIGGERS`, a dotted effect type, and
JSON-safe params. This module is not a second schema. It turns one *triggered*
effect into the stable payload the engine bus publishes and `event_log`
stores, so the engine, the log, and a client read exactly the same shape.

The payload is deliberately free of timestamps, random ids, and object
identities: the same trigger on the same content produces the same dict.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from src.mud.content.models import Effect


def effect_entries(owner: Any, trigger: str) -> list[Any]:
    """The authored effects `owner` attaches to `trigger`, in file order.

    The loaded model stores `dict[trigger, list[Effect]]`. A hand-built entity
    in a test may store plain dicts; both are accepted here. Anything else is
    empty rather than a crash, because a runtime trigger must never turn a
    content typo (already rejected by `content check`) into a dropped session.
    """
    effects = getattr(owner, "effects", None)
    if not isinstance(effects, Mapping):
        return []
    entries = effects.get(trigger)
    if not isinstance(entries, (list, tuple)):
        return []
    return list(entries)


def effect_payload(
    effect: Effect | Mapping[str, Any],
    *,
    trigger: str,
    source: Mapping[str, str],
    actor: str | None,
    room: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    """One effect as the stable, JSON-safe bus/log payload.

    `source` names the room, object, or NPC that owns the effect; `room` is the
    room the effect happens in (the same room for a room-owned effect). The
    authored params are deep-copied so a subscriber cannot mutate content.
    Returns None for an entry that carries no usable `type`.
    """
    if isinstance(effect, Effect):
        effect_type = effect.type
        params: dict[str, Any] = copy.deepcopy(effect.params)
    elif isinstance(effect, Mapping):
        effect_type = effect.get("type")
        raw_params = effect.get("params")
        if raw_params is None:
            params = {}
        elif isinstance(raw_params, Mapping):
            params = copy.deepcopy(dict(raw_params))
        else:
            return None
    else:
        return None

    if not isinstance(effect_type, str) or not effect_type:
        return None

    return {
        "effect": {"type": effect_type, "params": params},
        "trigger": trigger,
        "source": dict(source),
        "room": dict(room) if room is not None else None,
        "actor": {"name": actor} if actor is not None else None,
    }

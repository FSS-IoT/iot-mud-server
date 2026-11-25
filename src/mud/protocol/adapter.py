"""Translate one engine command into protocol-v2 events.

The engine is authoritative: `Engine.execute` parses the command once, mutates
state, and returns a typed `CommandOutcome` with a reply, a typed status, and
the semantic events the command produced (#130). This module is now only the
v2 shaping layer: it turns each semantic event into the wire `Outbound`(s),
adding the transport concerns the engine deliberately does not know — the
actor/room split, the `self` flag, and a room broadcast.

It no longer parses verbs, classifies prose, or snapshots world state.
"""

from __future__ import annotations

from typing import Any

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine
from src.mud.protocol.v2 import CommandResult, Outbound
from src.mud.storage.keys import room_key, slug


def _room_id(room: Room) -> str:
    return room_key(room)


def _object_data(entity: Entity, collection: list[Entity]) -> dict[str, Any]:
    """Stable instance id plus the prototype kind.

    The world's item id is the prototype. When a collection holds more than one
    of the same prototype, the instance id gets a numeric suffix so a client can
    tell two coins apart.
    """
    base = getattr(entity, "item_id", None) or slug(entity.name)
    ident = getattr(entity, "instance_id", None)
    if not ident:
        siblings = [
            candidate for candidate in collection
            if (getattr(candidate, "item_id", None)
                or slug(candidate.name)) == base
        ]
        if len(siblings) > 1:
            try:
                index = siblings.index(entity) + 1
            except ValueError:
                index = 1
            ident = f"{base}-{index}"
        else:
            ident = base
        entity.instance_id = ident
    return {"id": ident, "kind": base, "name": entity.name}


def build_room_state(engine: Engine, player: Player) -> dict[str, Any]:
    """The actor's view of its room, structured for v2."""
    room = player.location
    if room is None:
        return {
            "room": {
                "id": "void", "name": "The Void",
                "description": "You feel lost in a void.",
                "dark": False, "exits": [], "objects": [],
                "players": [], "npcs": [],
            },
            "text": "You feel lost in a void.",
        }

    view = engine.visible_room_state(player)
    if view.get("dark"):
        return {
            "room": {
                "id": _room_id(room),
                "name": room.name,
                "description": room.description,
                "dark": True,
                "exits": [],
                "objects": [],
                "players": [],
                "npcs": [],
            },
            "text": "It is too dark to see anything.",
        }

    objects = []
    for entity in room.contents:
        if isinstance(entity, Player):
            continue
        data = _object_data(entity, room.contents)
        data["portable"] = bool(getattr(entity, "portable", True))
        if getattr(entity, "readable", False):
            data["readable"] = True
        if getattr(entity, "light_source", False):
            data["lit"] = bool(getattr(entity, "lit", False))
        if getattr(entity, "locked", False):
            data["locked"] = True
        if (getattr(entity, "container", False)
                or getattr(entity, "contents", None)
                or getattr(entity, "locked", False)):
            data["container"] = True
            # Explicit lid state (#76); additive, so v2.0 clients ignore it.
            data["open"] = bool(getattr(entity, "open", False))
        objects.append(data)
    objects.sort(key=lambda obj: obj["name"].lower())

    players = []
    for other in engine.players_in_room(room):
        if other.name == player.name:
            continue
        person = {"name": other.name}
        if getattr(other, "asleep", False):
            # Sleep is the stone state (#72): present but invincible. The
            # fields are additive, so an awake entry keeps the v2.0 shape.
            person["asleep"] = True
            person["stone"] = True
        players.append(person)
    players.sort(key=lambda person: person["name"].lower())
    npcs = [
        {
            "id": getattr(npc, "item_id", None) or slug(npc.name),
            "name": npc.name,
        }
        for npc in sorted(room.npcs, key=lambda npc: npc.name.lower())
    ]
    exits = []
    for direction, target in sorted(room.exits.items()):
        lock = room.exit_locks.get(direction)
        entry: dict[str, Any] = {
            "direction": direction,
            "to": _room_id(target),
            "locked": room.exit_is_locked(direction),
        }
        # The author's door name, when there is one. Additive (#77).
        if lock is not None and lock.label:
            entry["label"] = lock.label
        exits.append(entry)

    text = f"{room.name}\n{room.description}"
    if objects:
        text += "\n\nHere you see: " + ", ".join(
            obj["name"] for obj in objects) + "."
    if players:
        names = [
            f"{person['name']} (asleep)" if person.get("asleep")
            else person["name"]
            for person in players
        ]
        text += "\n\nOthers in the room: " + ", ".join(names) + "."
    if npcs:
        text += "\n\nAlso here: " + ", ".join(
            npc["name"] for npc in npcs) + "."

    payload = {
        "id": _room_id(room),
        "name": room.name,
        "description": room.description,
        "dark": False,
        "exits": exits,
        "objects": objects,
        "players": players,
        "npcs": npcs,
    }
    # Additive: only rooms with authored geometry carry the field, and the
    # dark branch above omits it so a map is never leaked to an observer who
    # cannot see the room.
    geometry = getattr(room, "geometry", None)
    if geometry is not None:
        payload["geometry"] = geometry

    return {"room": payload, "text": text}


def _weight(value: float) -> float:
    """Round a sum of authored weights to a stable wire value (#78)."""
    return round(float(value), 3)


def _inventory_data(engine: Engine, player: Player,
                    text: str | None = None) -> dict[str, Any]:
    """The actor's pack, always with the carry-limit fields (#78).

    `capacity` and `used` are in the same unit as `Entity.weight`; `used` is
    the sum of the carried objects' own weights, not their nested contents.
    Every inventory push (the `inventory` verb and the put/get follow-ups) goes
    through here, so the numbers cannot drift.
    """
    items = []
    for item in player.contents:
        data = _object_data(item, player.contents)
        if getattr(item, "light_source", False):
            data["lit"] = bool(getattr(item, "lit", False))
        items.append(data)
    items.sort(key=lambda item: item["name"].lower())
    if text is None:
        if items:
            text = "You are carrying: " + ", ".join(
                item["name"] for item in items) + "."
        else:
            text = "You are not carrying anything."
    payload = []
    for item in items:
        entry = {"id": item["id"], "name": item["name"]}
        if "lit" in item:
            entry["lit"] = item["lit"]
        payload.append(entry)
    return {
        "items": payload,
        "capacity": _weight(engine.carry_capacity(player)),
        "used": _weight(engine.carry_used(player)),
        "text": text,
    }


def _error(code: str, message: str) -> CommandResult:
    return CommandResult([Outbound("error", {"code": code, "message": message})])


def _shape_event(engine: Engine, actor: Player, sem) -> list[Outbound]:
    """Map one semantic event to its wire outbound(s)."""
    kind, data, scope = sem.kind, sem.data, sem.scope
    if kind == "room.state":
        player = data.get("actor", actor)
        return [Outbound("room.state", build_room_state(engine, player),
                         "actor", direct=sem.direct)]
    if kind == "notice":
        return [Outbound("notice", {"text": data.get("text", ""),
                                    "level": "info"},
                         "actor", direct=sem.direct)]
    if kind == "player.inventory":
        return [Outbound("player.inventory",
                         _inventory_data(engine, data["actor"],
                                         text=data.get("text")),
                         "actor", direct=sem.direct)]
    if kind == "who.list":
        return [Outbound("who.list", {
            "players": engine.who_entries(),
            "text": data.get("text", ""),
        }, "actor", direct=sem.direct)]
    if kind == "object.read":
        return [Outbound("object.read", {
            "object": data["object"],
            "content": data.get("content", ""),
            "text": data.get("text", ""),
        }, "actor", direct=sem.direct)]
    if kind == "exit.locked":
        room = data["room"]
        target = data.get("target")
        return [Outbound("exit.locked", {
            "exit": {
                "direction": data["direction"],
                "to": _room_id(target) if target is not None else None,
            },
            "room": {"id": _room_id(room), "name": room.name},
            "by": data["actor"],
            "self": False,
            "text": (f"{data['actor']} tries the way {data['direction']}; "
                     "it is locked."),
        }, "room", room_name=room.name, direct=False)]
    if kind == "exit.unlocked":
        room = data["room"]
        base = {
            "exit": {"direction": data["direction"],
                     "to": _room_id(data["target"])},
            "room": {"id": _room_id(room), "name": room.name},
            "by": data["actor"],
        }
        return [
            Outbound(kind, {**base, "self": True,
                            "text": data.get("text_actor", "")},
                     "actor", direct=sem.direct),
            Outbound(kind, {**base, "self": False,
                            "text": data.get("text_room", "")},
                     "room", room_name=room.name, direct=False),
        ]
    if kind == "player.told":
        base = {"from": data["from"], "to": data["to"]}
        if scope == "player":
            return [Outbound(kind, {
                **base, "self": False,
                "text": data.get("text_actor", ""),
            }, "player", player_name=data.get("player"), direct=False)]
        return [Outbound(kind, {
            **base, "self": True,
            "text": data.get("text_actor", ""),
        }, "actor", direct=sem.direct)]
    if kind == "npc.say":
        base = {"npc": data["npc"]}
        if "topic" in data:
            base["topic"] = data["topic"]
        room = actor.location
        return [
            Outbound(kind, {**base, "self": True,
                            "text": data.get("text_actor", "")},
                     "actor", direct=sem.direct),
            Outbound(kind, {**base, "self": False,
                            "text": data.get("text_room", "")},
                     "room", room_name=room.name if room else None,
                     direct=False),
        ]
    if kind in ("room.leave", "room.enter"):
        room = data["room"]
        return [Outbound(kind, {
            "room": {"id": _room_id(room), "name": room.name},
            "actor": {"name": data["actor"]},
            "direction": data["direction"],
            "reason": "move",
            "text": data["text"],
        }, "room", room_name=room.name, direct=False)]
    # Object and social events carry a shared body plus one text per
    # perspective (`text_actor` / `text_room`).
    base = {key: value for key, value in data.items()
            if key not in ("text_actor", "text_room")}
    actor_event = Outbound(
        kind, {**base, "self": True, "text": data.get("text_actor", "")},
        "actor", direct=sem.direct)
    if scope == "actor":
        return [actor_event]
    room_event = Outbound(
        kind, {**base, "self": False, "text": data.get("text_room", "")},
        "room", direct=False)
    if scope == "room_others":
        return [room_event]
    return [actor_event, room_event]


def _shape_outcome(engine: Engine, actor: Player, outcome) -> CommandResult:
    """Turn a typed `CommandOutcome` into the wire events it should produce."""
    events: list[Outbound] = []
    if outcome.status == "error":
        data: dict[str, Any] = {
            "code": outcome.error_code, "message": outcome.reply,
        }
        data.update(outcome.error_data)
        events.append(Outbound("error", data))
    for sem in outcome.events:
        events.extend(_shape_event(engine, actor, sem))
    return CommandResult(events)


async def execute(engine: Engine, player_name: str,
                  command_text: str) -> CommandResult:
    """Run one command and return the v2 events it should produce."""
    player = engine.get_player(player_name)
    if player is None:
        return _error("not_joined", "Join before sending commands.")

    if command_text.strip().lower() in ("quit", "exit"):
        return CommandResult([])

    outcome = await engine.execute(player, command_text)
    return _shape_outcome(engine, player, outcome)

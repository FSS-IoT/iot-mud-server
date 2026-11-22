"""Translate one engine command into protocol-v2 events.

The engine remains authoritative: `Engine.process_command` performs the
mutation and returns the reply text. This adapter inspects the state before and
after that call to emit typed events, and keeps the reply text as the
presentation field. That keeps v2 additive over Doc's working engine instead of
rewriting every handler.

The adapter is deliberately the only place that knows both worlds, so the
websocket server stays a transport.
"""

from __future__ import annotations

import logging
from typing import Any

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine
from src.mud.protocol.v2 import CommandResult, Outbound
from src.mud.storage.keys import room_key, slug
from src.mud.storage.store import StorageWriteError

logger = logging.getLogger(__name__)

# Mirrors Engine.process_command's alias table. The engine's reply remains the
# source of truth; this only decides which typed event to emit.
_ALIASES = {
    "get": "take", "pick": "take", "grab": "take",
    "l": "look",
    "n": "go north", "north": "go north",
    "s": "go south", "south": "go south",
    "e": "go east", "east": "go east",
    "w": "go west", "west": "go west",
    "u": "go up", "up": "go up",
    "d": "go down", "down": "go down",
    "exits": "show_exits",
    "i": "inventory", "inv": "inventory",
    "h": "help",
    "exam": "examine",
}


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
        data["portable"] = True
        if getattr(entity, "readable", False):
            data["readable"] = True
        if getattr(entity, "light_source", False):
            data["lit"] = bool(getattr(entity, "lit", False))
        if getattr(entity, "locked", False):
            data["locked"] = True
        if getattr(entity, "contents", None) or getattr(entity, "locked", False):
            data["container"] = True
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
    exits = [
        {"direction": direction, "to": _room_id(target), "locked": False}
        for direction, target in sorted(room.exits.items())
    ]

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

    payload = {
        "id": _room_id(room),
        "name": room.name,
        "description": room.description,
        "dark": False,
        "exits": exits,
        "objects": objects,
        "players": players,
        "npcs": [],
    }
    # Additive: only rooms with authored geometry carry the field, and the
    # dark branch above omits it so a map is never leaked to an observer who
    # cannot see the room.
    geometry = getattr(room, "geometry", None)
    if geometry is not None:
        payload["geometry"] = geometry

    return {"room": payload, "text": text}


def _inventory_data(player: Player, text: str | None = None) -> dict[str, Any]:
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
    return {"items": payload, "text": text}


def _find(engine: Engine, collection: list[Entity], name: str | None) -> Entity | None:
    if not collection or not name:
        return None
    item, disambiguation = engine._disambiguate(collection, name)
    if disambiguation:
        return None
    return item


def _classify(reply: str) -> str:
    lowered = reply.lower()
    if "i don't understand" in lowered:
        return "unknown_command"
    if lowered.startswith("there is nothing readable"):
        return "not_found"
    if lowered.startswith(("you see no ", "you can't read")):
        return "not_found"
    if lowered.startswith(("there is no ", "there are no ")):
        return "not_found"
    if (lowered.startswith(("you aren't carrying", "you don't have"))):
        return "not_found"
    if lowered.startswith("there is nowhere"):
        return "not_here"
    if " is locked" in lowered:
        return "locked"
    if " does not fit" in lowered:
        return "locked"
    if "is already open" in lowered:
        return "already_open"
    if ("which " in lowered or "did you mean" in lowered
            or "specify" in lowered):
        return "ambiguous_target"
    if lowered.startswith("you cannot go"):
        return "no_exit"
    if ("what? try" in lowered or "use a key on something" in lowered
            or "put what where" in lowered):
        return "bad_args"
    return "internal"


def _error(code: str, message: str) -> CommandResult:
    return CommandResult([Outbound("error", {"code": code, "message": message})])


def _notice(text: str) -> CommandResult:
    return CommandResult([Outbound("notice", {"text": text, "level": "info"})])


def _failure(reply: str) -> CommandResult:
    code = _classify(reply)
    if code == "internal":
        return _notice(reply)
    return _error(code, reply)


def _actor(type_: str, data: dict[str, Any]) -> Outbound:
    return Outbound(type_, data, "actor", direct=True)


def _room(type_: str, data: dict[str, Any],
          room_name: str | None = None) -> Outbound:
    return Outbound(type_, data, "room", room_name=room_name, direct=False)


def _parse(text: str) -> tuple[str, str | None, str]:
    """Return (verb, arg, raw) with the engine's aliases applied."""
    raw = text.strip()
    lowered = raw.lower()
    if not lowered:
        return "empty", None, raw

    parts = lowered.split(maxsplit=1)
    verb = parts[0]
    arg = parts[1] if len(parts) > 1 else None

    if verb == "look" and arg is not None:
        first = arg.split(maxsplit=1)[0]
        if first == "at":
            rest = arg.split(maxsplit=1)
            return "examine", rest[1].strip() if len(rest) > 1 else None, raw
        if first in ("in", "inside"):
            rest = arg.split(maxsplit=1)
            return "look_in", rest[1].strip() if len(rest) > 1 else "", raw
        return "look", arg, raw

    input_str = f"{verb} {arg}".strip() if arg else verb
    if input_str in _ALIASES:
        target = _ALIASES[input_str]
        resolved, _, resolved_arg = target.partition(" ")
        return resolved, resolved_arg or None, raw
    if verb in _ALIASES:
        target = _ALIASES[verb]
        resolved, _, resolved_arg = target.partition(" ")
        return resolved, resolved_arg or None, raw
    return verb, arg, raw


async def execute(engine: Engine, player_name: str,
                  command_text: str) -> CommandResult:
    """Run one command and return the v2 events it should produce."""
    player = engine.get_player(player_name)
    if player is None:
        return _error("not_joined", "Join before sending commands.")

    verb, arg, raw = _parse(command_text)
    if verb in ("exit", "quit"):
        return CommandResult([])

    before_room = player.location
    before_room_contents = list(before_room.contents) if before_room else []
    before_inventory = list(player.contents)
    before_locked = {
        id(entity): bool(getattr(entity, "locked", False))
        for entity in before_room_contents
    }
    before_lit = {
        id(entity): bool(getattr(entity, "lit", False))
        for entity in before_inventory
    }

    try:
        reply = await engine.process_command(command_text, player_name)
    except StorageWriteError:
        # Commit failed, so the engine changed nothing: one typed error, no
        # event for any other client (SPEC v2.1.0 criterion 2).
        logger.exception("write-through failed on %r", command_text)
        return _error(
            "internal",
            "The world could not save that command; nothing changed.",
        )
    after_room = player.location

    if verb == "look":
        return CommandResult([_actor("room.state",
                                     build_room_state(engine, player))])

    if verb == "go":
        if not arg:
            return _error("bad_args", reply)
        if after_room is not None and after_room is not before_room:
            events = [_actor("room.state", build_room_state(engine, player))]
            if before_room is not None:
                events.append(_room("room.leave", {
                    "room": {"id": _room_id(before_room),
                             "name": before_room.name},
                    "actor": {"name": player.name},
                    "direction": arg,
                    "reason": "move",
                    "text": f"{player.name} leaves.",
                }, room_name=before_room.name))
            events.append(_room("room.enter", {
                "room": {"id": _room_id(after_room),
                         "name": after_room.name},
                "actor": {"name": player.name},
                "direction": arg,
                "reason": "move",
                "text": f"{player.name} arrives.",
            }, room_name=after_room.name))
            return CommandResult(events)
        return _failure(reply)

    if verb == "take":
        taken = [item for item in player.contents
                 if item not in before_inventory]
        if taken:
            item = taken[0]
            obj = _object_data(item, before_room_contents)
            return CommandResult([
                _actor("object.taken", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                Outbound("player.inventory", _inventory_data(player),
                         direct=False),
                _room("object.taken", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} picks up the {item.name}.",
                }),
            ])
        return _failure(reply)

    if verb == "drop":
        dropped = [
            item for item in (after_room.contents if after_room else [])
            if item not in before_room_contents and not isinstance(item, Player)
        ]
        if dropped:
            item = dropped[0]
            obj = _object_data(item, after_room.contents)
            return CommandResult([
                _actor("object.dropped", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                Outbound("player.inventory", _inventory_data(player),
                         direct=False),
                _room("object.dropped", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} drops the {item.name}.",
                }),
            ])
        return _failure(reply)

    if verb == "put":
        _item_arg, _, container_arg = (
            arg.rpartition(" in ") if arg and " in " in arg else ("", "", ""))
        moved = [item for item in before_inventory
                 if item not in player.contents]
        container = _find(engine, before_room_contents, container_arg.strip())
        if moved and container is not None:
            item = moved[0]
            obj = _object_data(item, container.contents)
            cont = _object_data(container, before_room_contents)
            return CommandResult([
                _actor("object.put", {
                    "object": obj, "container": cont,
                    "by": player.name, "self": True, "text": reply,
                }),
                Outbound("player.inventory", _inventory_data(player),
                         direct=False),
                _room("object.put", {
                    "object": obj, "container": cont,
                    "by": player.name, "self": False,
                    "text": (f"{player.name} puts the {item.name} in the "
                             f"{container.name}."),
                }),
            ])
        return _failure(reply)

    if verb == "open":
        container = _find(engine, before_room_contents, arg)
        if container is not None and getattr(container, "locked", False):
            return _error("locked", reply)
        if container is not None and (
                reply.lower().startswith("you open")
                or reply.lower().rstrip().endswith("is empty.")):
            obj = _object_data(container, before_room_contents)
            return CommandResult([
                _actor("object.opened", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                _room("object.opened", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} opens the {container.name}.",
                }),
            ])
        return _failure(reply)

    if verb == "use":
        lock_name = arg.split(" on ", 1)[1].strip() if arg and " on " in arg else None
        container = _find(engine, before_room_contents, lock_name)
        if container is None:
            return _failure(reply)
        was_locked = before_locked.get(id(container), False)
        still_locked = bool(getattr(container, "locked", False))
        if was_locked and not still_locked:
            obj = _object_data(container, before_room_contents)
            return CommandResult([
                _actor("object.unlocked", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                _room("object.unlocked", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} unlocks the {container.name}.",
                }),
            ])
        # The reply decides: "already open", "does not fit", or missing key.
        return _failure(reply)

    if verb == "light":
        target = _find(engine, before_inventory, arg)
        was_lit = before_lit.get(id(target), False) if target is not None else False
        now_lit = bool(getattr(target, "lit", False)) if target is not None else False
        if target is not None and now_lit and not was_lit:
            obj = _object_data(target, player.contents)
            obj["lit"] = True
            return CommandResult([
                _actor("object.lit", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                _room("object.lit", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} lights the {target.name}.",
                }),
            ])
        return _failure(reply)

    if verb == "read":
        if reply.lower().startswith("it is too dark"):
            return _notice(reply)
        held = _find(engine, player.contents, arg)
        target = held or _find(engine, before_room_contents, arg)
        if (target is not None and getattr(target, "readable", False)
                and reply.lower().startswith("it is")):
            collection = player.contents if held is not None else before_room_contents
            return CommandResult([_actor("object.read", {
                "object": _object_data(target, collection),
                "content": target.description,
                "text": reply,
            })])
        return _failure(reply)

    if verb == "inventory":
        return CommandResult([
            _actor("player.inventory", _inventory_data(player, text=reply)),
        ])

    if verb == "who":
        return CommandResult([
            _actor("who.list", {
                "players": engine.who_entries(),
                "text": reply,
            }),
        ])

    if verb == "say":
        spoken = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
        if not spoken:
            return _failure(reply)
        return CommandResult([
            _actor("player.said", {
                "from": player.name, "text": reply, "self": True,
            }),
            _room("player.said", {
                "from": player.name,
                "text": f'{player.name} says, "{spoken}".',
                "self": False,
            }),
        ])

    if verb == "examine":
        if not arg:
            return _error("bad_args", "Examine what? Try `examine <thing>`.")
        return _notice(reply)

    if verb in ("show_exits", "help", "hello"):
        return _notice(reply)

    if verb == "look_in":
        if not arg:
            return _error("bad_args",
                          "Look in what? Try `look in <container>`.")
        if " is locked" in reply.lower():
            return _error("locked", reply)
        return _notice(reply)

    if verb == "empty":
        return _notice(reply)

    return _error("unknown_command", reply)

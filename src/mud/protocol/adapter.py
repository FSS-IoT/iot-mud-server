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
    "me": "emote",
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
    Every inventory push (the `inventory` verb and the put/get
    follow-ups) goes through here, so the numbers cannot drift.
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


def _candidates(engine: Engine, collection: list[Entity],
                name: str | None) -> list[dict[str, Any]]:
    """The structured ambiguity candidates for `name` in `collection`.

    Empty when the name resolves uniquely or not at all; `_failure` only
    attaches the list to an `ambiguous` error. The same order as the engine's
    `<name> <n>` index: display name, then collection order.
    """
    if not collection or not name:
        return []
    matches = engine.target_matches(collection, name)
    if len(matches) < 2:
        return []
    return [_object_data(item, collection) for item in matches]


def _too_heavy(engine: Engine, player: Player, collection: list[Entity],
               name: str | None, reply: str) -> CommandResult:
    """The typed carry-limit refusal, with the numbers behind it (#78)."""
    data: dict[str, Any] = {
        "code": "too_heavy",
        "message": reply,
        "capacity": _weight(engine.carry_capacity(player)),
        "used": _weight(engine.carry_used(player)),
    }
    item = _find(engine, collection, name)
    if item is not None:
        data["object"] = _object_data(item, collection)
        data["weight"] = _weight(float(getattr(item, "weight", 0.0) or 0.0))
    return CommandResult([Outbound("error", data)])


def _container_cycle(engine: Engine, inventory: list[Entity],
                     room_contents: list[Entity], item_arg: str | None,
                     container_arg: str | None,
                     reply: str) -> CommandResult:
    """The typed refusal for a put that would close a containment cycle (#79).

    The engine refused before moving anything, so no `object.put` and no
    inventory push: one error naming the object and the container it refused.
    """
    data: dict[str, Any] = {"code": "container_cycle", "message": reply}
    item = _find(engine, inventory, item_arg)
    container = _find(engine, room_contents, container_arg)
    if item is not None:
        data["object"] = _object_data(item, inventory)
    if container is not None:
        data["container"] = _object_data(container, room_contents)
    return CommandResult([Outbound("error", data)])


def _find(engine: Engine, collection: list[Entity], name: str | None) -> Entity | None:
    if not collection or not name:
        return None
    item, disambiguation = engine._disambiguate(collection, name)
    if disambiguation:
        return None
    return item


def _unlocked_exit(room: Room, before_locks: dict[str, bool]):
    """The (direction, target) this command unlocked, or None.

    Exit locks are shared by both directions of a two-way door, so looking at
    the actor's room is enough: a change from locked to unlocked names the way
    the actor used.
    """
    for direction, lock in room.exit_locks.items():
        if before_locks.get(direction) and not lock.locked:
            target = room.exits.get(direction)
            if target is not None:
                return direction, target
    return None


def _classify(reply: str) -> str:
    lowered = reply.lower()
    if "i don't understand" in lowered:
        return "unknown_command"
    # The carry limit refuses before moving anything (#78). Match the whole
    # refusal, not the words in an object description.
    if lowered.startswith("you cannot carry ") and "too heavy" in lowered:
        return "too_heavy"
    # The container guard refuses before moving anything (#79).
    if (lowered.startswith("you cannot put the ")
            and "cannot contain itself" in lowered):
        return "container_cycle"
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
    # Container verbs (#76): the lid and contents decide the code.
    if " is already closed" in lowered:
        return "already_closed"
    if " is closed" in lowered:
        return "container_closed"
    if " is not in the " in lowered:
        return "not_here"
    if "fixed in place" in lowered:
        return "not_portable"
    if ("not something you can close" in lowered
            or "is not a container" in lowered):
        return "bad_args"
    # A name on the durable roster whose session is gone (#72) is asleep and
    # stone. `tell` refuses rather than queueing; the code is additive (#75).
    if " is asleep, turned to stone" in lowered:
        return "not_awake"
    if " is locked" in lowered:
        return "locked"
    if " does not fit" in lowered:
        return "locked"
    if "is already open" in lowered:
        return "already_open"
    if lowered.startswith("there are several ") or "did you mean" in lowered:
        # v2.1 spells this `ambiguous` and attaches `candidates[]` (#78);
        # the v2.0 `ambiguous_target` name remains in the reserved list.
        return "ambiguous"
    if lowered.startswith("you cannot go"):
        return "no_exit"
    if ("what? try" in lowered or "use a key on something" in lowered
            or "put what where" in lowered
            or lowered.startswith(("talk to whom", "ask whom about what"))):
        return "bad_args"
    return "internal"


def _error(code: str, message: str) -> CommandResult:
    return CommandResult([Outbound("error", {"code": code, "message": message})])


def _notice(text: str) -> CommandResult:
    return CommandResult([Outbound("notice", {"text": text, "level": "info"})])


def _failure(reply: str,
             candidates: list[dict[str, Any]] | None = None) -> CommandResult:
    code = _classify(reply)
    if code == "internal":
        return _notice(reply)
    if code != "ambiguous":
        return _error(code, reply)
    data: dict[str, Any] = {"code": code, "message": reply}
    # An ambiguous refusal always says what it was choosing between.
    data["candidates"] = candidates or []
    return CommandResult([Outbound("error", data)])


def _actor(type_: str, data: dict[str, Any]) -> Outbound:
    return Outbound(type_, data, "actor", direct=True)


def _room(type_: str, data: dict[str, Any],
          room_name: str | None = None) -> Outbound:
    return Outbound(type_, data, "room", room_name=room_name, direct=False)


def _player(type_: str, data: dict[str, Any], player_name: str) -> Outbound:
    """A private push to one named character, not a request reply (#75)."""
    return Outbound(type_, data, "player", player_name=player_name,
                    direct=False)


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
    if verb == "get" and arg and " from " in arg:
        # `get <item> from <container>` is its own v2.1 verb (#76); a bare
        # `get <item>` still resolves to `take` through the alias table.
        return "get", arg, raw
    if input_str in _ALIASES:
        target = _ALIASES[input_str]
        resolved, _, resolved_arg = target.partition(" ")
        return resolved, resolved_arg or None, raw
    if verb in _ALIASES:
        target = _ALIASES[verb]
        resolved, _, resolved_arg = target.partition(" ")
        return resolved, resolved_arg or None, raw
    return verb, arg, raw


# Verbs whose semantic events the engine already produces. The adapter shapes
# these from a `CommandOutcome` instead of re-parsing and diffing state (#130).
# The set grows as verbs move to the typed path; the old branches below cover
# the rest until then.
_OUTCOME_VERBS = frozenset({"take", "drop", "open", "go"})


def _shape_event(engine: Engine, actor: Player, sem) -> list[Outbound]:
    """Map one semantic event to its wire outbound(s)."""
    kind, data, scope = sem.kind, sem.data, sem.scope
    if kind == "room.state":
        player = data.get("actor", actor)
        return [Outbound("room.state", build_room_state(engine, player),
                         "actor", direct=sem.direct)]
    if kind in ("room.leave", "room.enter"):
        room = data["room"]
        return [Outbound(kind, {
            "room": {"id": _room_id(room), "name": room.name},
            "actor": {"name": data["actor"]},
            "direction": data["direction"],
            "reason": "move",
            "text": data["text"],
        }, "room", room_name=room.name, direct=False)]
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
    # Object events carry a shared body plus one text per perspective.
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

    verb, arg, raw = _parse(command_text)
    if verb in ("exit", "quit"):
        return CommandResult([])

    if verb in _OUTCOME_VERBS:
        outcome = await engine.execute(player, command_text)
        return _shape_outcome(engine, player, outcome)

    before_room = player.location
    before_room_contents = list(before_room.contents) if before_room else []
    before_inventory = list(player.contents)
    before_locked = {
        id(entity): bool(getattr(entity, "locked", False))
        for entity in before_room_contents
    }
    before_open = {
        id(entity): bool(getattr(entity, "open", False))
        for entity in before_room_contents
    }
    before_lit = {
        id(entity): bool(getattr(entity, "lit", False))
        for entity in before_inventory
    }
    before_exit_locks = (
        {
            direction: bool(lock.locked)
            for direction, lock in before_room.exit_locks.items()
        }
        if before_room is not None else {}
    )

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
        if (before_room is not None
                and before_room.exit_is_locked(arg.strip())):
            # The actor gets the typed refusal; the room sees the attempt.
            direction = arg.strip()
            target = before_room.exits.get(direction)
            return CommandResult([
                *_error("locked", reply).events,
                _room("exit.locked", {
                    "exit": {
                        "direction": direction,
                        "to": _room_id(target) if target is not None else None,
                    },
                    "room": {"id": _room_id(before_room),
                             "name": before_room.name},
                    "by": player.name,
                    "self": False,
                    "text": f"{player.name} tries the way {direction}; it is locked.",
                }, room_name=before_room.name),
            ])
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
                _room("object.taken", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} picks up the {item.name}.",
                }),
            ])
        if _classify(reply) == "too_heavy":
            return _too_heavy(
                engine, player, before_room_contents, arg, reply)
        return _failure(
            reply, _candidates(engine, before_room_contents, arg))

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
                _room("object.dropped", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} drops the {item.name}.",
                }),
            ])
        return _failure(
            reply, _candidates(engine, before_inventory, arg))

    if verb == "put":
        item_arg, _, container_arg = (
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
                Outbound("player.inventory", _inventory_data(engine, player),
                         direct=False),
                _room("object.put", {
                    "object": obj, "container": cont,
                    "by": player.name, "self": False,
                    "text": (f"{player.name} puts the {item.name} in the "
                             f"{container.name}."),
                }),
            ])
        if _classify(reply) == "container_cycle":
            return _container_cycle(
                engine, before_inventory, before_room_contents,
                item_arg.strip(), container_arg.strip(), reply)
        return _failure(
            reply,
            _candidates(engine, before_inventory, item_arg)
            or _candidates(engine, before_room_contents, container_arg))

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
        return _failure(
            reply, _candidates(engine, before_room_contents, arg))

    if verb == "close":
        container = _find(engine, before_room_contents, arg)
        was_open = before_open.get(id(container), False) if container is not None else False
        if (container is not None and was_open
                and not bool(getattr(container, "open", False))
                and reply.lower().startswith("you close")):
            obj = _object_data(container, before_room_contents)
            return CommandResult([
                _actor("object.closed", {
                    "object": obj, "by": player.name, "self": True,
                    "text": reply,
                }),
                _room("object.closed", {
                    "object": obj, "by": player.name, "self": False,
                    "text": f"{player.name} closes the {container.name}.",
                }),
            ])
        return _failure(
            reply, _candidates(engine, before_room_contents, arg))

    if verb == "get":
        item_arg, _, container_arg = (
            arg.rpartition(" from ") if arg and " from " in arg
            else ("", "", ""))
        container = _find(engine, before_room_contents, container_arg.strip())
        moved = [item for item in player.contents
                 if item not in before_inventory]
        if (moved and container is not None
                and reply.lower().startswith("you get ")):
            item = moved[0]
            obj = _object_data(item, player.contents)
            cont = _object_data(container, before_room_contents)
            return CommandResult([
                _actor("object.got", {
                    "object": obj, "container": cont,
                    "by": player.name, "self": True, "text": reply,
                }),
                Outbound("player.inventory", _inventory_data(engine, player),
                         direct=False),
                _room("object.got", {
                    "object": obj, "container": cont,
                    "by": player.name, "self": False,
                    "text": (f"{player.name} gets the {item.name} from the "
                             f"{container.name}."),
                }),
            ])
        if _classify(reply) == "too_heavy":
            return _too_heavy(
                engine, player,
                container.contents if container is not None else [],
                item_arg, reply)
        return _failure(
            reply,
            _candidates(engine, before_room_contents, container_arg)
            or _candidates(
                engine, container.contents if container is not None else [],
                item_arg))

    if verb == "use":
        if before_room is not None:
            unlocked = _unlocked_exit(before_room, before_exit_locks)
            if unlocked is not None:
                direction, target = unlocked
                payload = {
                    "exit": {"direction": direction, "to": _room_id(target)},
                    "room": {"id": _room_id(before_room),
                             "name": before_room.name},
                    "by": player.name,
                    "self": True,
                    "text": reply,
                }
                return CommandResult([
                    _actor("exit.unlocked", payload),
                    _room("exit.unlocked", {
                        **payload,
                        "self": False,
                        "text": f"{player.name} unlocks the way {direction}.",
                    }, room_name=before_room.name),
                ])
        key_name = arg.split(" on ", 1)[0].strip() if arg and " on " in arg else None
        lock_name = arg.split(" on ", 1)[1].strip() if arg and " on " in arg else None
        container = _find(engine, before_room_contents, lock_name)
        if container is None:
            return _failure(
                reply,
                _candidates(engine, before_inventory, key_name)
                or _candidates(engine, before_room_contents, lock_name))
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
        return _failure(
            reply,
            _candidates(engine, before_inventory, key_name)
            or _candidates(engine, before_room_contents, lock_name))

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
        return _failure(
            reply, _candidates(engine, before_inventory, arg))

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
        return _failure(
            reply,
            _candidates(engine, before_inventory, arg)
            or _candidates(engine, before_room_contents, arg))

    if verb in ("talk", "ask"):
        # Scripted NPC dialogue (#81). The engine resolved the room NPC and
        # authored the line; the adapter only shapes it. Success is the one
        # reply shape the handlers produce, so a miss (unknown NPC, missing
        # `about`, ambiguity) stays a typed failure and emits no speech.
        npc_arg = arg or ""
        topic_arg = None
        if verb == "ask":
            npc_name, separator, topic_arg = npc_arg.partition(" about ")
            npc_arg = npc_name if separator else ""
        npc, _ambiguity = (
            engine.find_npc(before_room, npc_arg) if npc_arg else (None, None)
        )
        if npc is None or not reply.startswith(f"{npc.name} says,"):
            return _failure(
                reply,
                _candidates(engine, before_room_contents, npc_arg)
                if npc_arg else None,
            )
        data: dict[str, Any] = {
            "npc": {
                "id": getattr(npc, "item_id", None) or slug(npc.name),
                "name": npc.name,
            },
            "self": True,
            "text": reply,
        }
        if topic_arg is not None and topic_arg.strip().lower() in npc.topics:
            # Additive: present only when the asked topic matched the script.
            data["topic"] = topic_arg.strip().lower()
        room_data = {**data, "self": False}
        return CommandResult([
            _actor("npc.say", data),
            _room("npc.say", room_data, room_name=before_room.name),
        ])

    if verb == "inventory":
        return CommandResult([
            _actor("player.inventory",
                   _inventory_data(engine, player, text=reply)),
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

    if verb == "emote":
        # Room-local only (#75): the actor and its room see the same rendered
        # line; `self` marks the actor. The engine's reply is that line.
        if not arg:
            return _failure(reply)
        return CommandResult([
            _actor("player.emoted", {
                "from": player.name, "text": reply, "self": True,
            }),
            _room("player.emoted", {
                "from": player.name, "text": reply, "self": False,
            }),
        ])

    if verb == "tell":
        # Private to the sender and an awake target, never the room (#75).
        # The engine resolved the target and refused a sleeping one; a typed
        # failure carries no target event and creates no offline queue.
        pieces = raw.split(maxsplit=2)
        if len(pieces) < 3 or not pieces[2].strip():
            return _error("bad_args", reply)
        target_name = pieces[1]
        spoken = pieces[2].strip()
        if not reply.lower().startswith("you tell "):
            return _failure(reply)
        target = engine.find_character(target_name)
        if target is None:
            # The target vanished between the engine call and this lookup.
            return _notice(reply)
        return CommandResult([
            _actor("player.told", {
                "from": player.name, "to": target.name,
                "text": reply, "self": True,
            }),
            _player("player.told", {
                "from": player.name, "to": target.name,
                "text": f'{player.name} tells you, "{spoken}".',
                "self": False,
            }, target.name),
        ])

    if verb == "examine":
        if not arg:
            return _error("bad_args", "Examine what? Try `examine <thing>`.")
        # A description is a notice; a miss or an ambiguity is typed (#78).
        return _failure(
            reply,
            _candidates(engine, before_inventory, arg)
            or _candidates(engine, before_room_contents, arg))

    if verb in ("show_exits", "help", "hello"):
        return _notice(reply)

    if verb == "look_in":
        if not arg:
            return _error("bad_args",
                          "Look in what? Try `look in <container>`.")
        if " is locked" in reply.lower():
            return _error("locked", reply)
        return _failure(
            reply, _candidates(engine, before_room_contents, arg))

    if verb == "empty":
        return _notice(reply)

    return _error("unknown_command", reply)

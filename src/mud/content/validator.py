"""Semantic validation for world content.

The model in `models.py` answers "is this YAML shaped like a world?". This
module answers "does the world hold together?": global id uniqueness, exit
targets and reciprocals, reachability from `start_room`, container cycles,
lock keys, the effect schema, and room geometry.

Every message names the file, the room, and the offending id where one exists.
`check_world` never raises for bad content; it returns the messages so the CLI
can print them without a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.mud.content.models import (
    OPPOSITE_EXITS,
    ContentModelError,
    Effect,
    Exit,
    GameObject,
    LoadedWorld,
    Room,
    load_world,
)
from src.mud.world.geometry import WorldDataError, validate_geometry


@dataclass(frozen=True)
class _Located:
    kind: str
    room_id: str
    file: Path


@dataclass(frozen=True)
class _Edge:
    source: str
    direction: str
    target: str
    one_way: bool
    file: Path


def _json_safe(value: Any) -> bool:
    """True when a value survives `json.dumps` unchanged in meaning."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _json_safe(item)
            for key, item in value.items()
        )
    return False


def container_cycles(contains: dict[str, list[str]]) -> list[list[str]]:
    """Return each containment cycle as an id path that ends where it began.

    This is the rule v2.1.2 reuses at runtime: a container may not contain
    itself directly or transitively. Unknown child ids are ignored here; the
    validator reports them separately so this function stays a pure graph
    check.
    """
    state: dict[str, int] = {}
    cycles: list[list[str]] = []
    reported: set[frozenset[str]] = set()

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for child in contains[node]:
            if child not in contains:
                continue
            if state.get(child) == 1:
                start_index = stack.index(child)
                cycle = stack[start_index:] + [child]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    cycles.append(cycle)
            elif state.get(child, 0) == 0:
                visit(child, stack)
        stack.pop()
        state[node] = 2

    for node in contains:
        if state.get(node, 0) == 0:
            visit(node, [])
    return cycles


def _check_effect_params(
    errors: list[str],
    file: Path,
    room_id: str,
    ident: str,
    effects: dict[str, list[Effect]],
) -> None:
    for trigger, entries in effects.items():
        for index, effect in enumerate(entries):
            for param, value in effect.params.items():
                if not _json_safe(value):
                    errors.append(
                        f"{file}: room {room_id!r} id {ident!r}: "
                        f"effects.{trigger}[{index}].params.{param} must be "
                        f"JSON-safe (string, number, boolean, null, list, or "
                        f"object)"
                    )


def validate_world(loaded: LoadedWorld) -> list[str]:
    """Return every semantic problem in a loaded world; empty means valid."""
    errors: list[str] = []
    world = loaded.world
    rooms = list(zip(world.rooms, loaded.room_files))
    if not rooms:
        return [f"{loaded.source}: world has no rooms"]

    # One global id namespace: rooms, objects, and NPCs may not collide.
    seen: dict[str, _Located] = {}
    room_by_id: dict[str, Room] = {}
    object_by_id: dict[str, GameObject] = {}
    object_room: dict[str, str] = {}
    object_file: dict[str, Path] = {}

    def claim(kind: str, ident: str, room: Room, file: Path) -> bool:
        previous = seen.get(ident)
        if previous is not None:
            errors.append(
                f"{file}: room {room.id!r} id {ident!r}: duplicate id "
                f"{ident!r} (already used by {previous.kind} in room "
                f"{previous.room_id!r} at {previous.file})"
            )
            return False
        seen[ident] = _Located(kind, room.id, file)
        return True

    for room, file in rooms:
        if claim("room", room.id, room, file):
            room_by_id[room.id] = room

    for room, file in rooms:
        for obj in room.objects:
            if claim("object", obj.id, room, file):
                object_by_id[obj.id] = obj
                object_room[obj.id] = room.id
                object_file[obj.id] = file
        for npc in room.npcs:
            claim("npc", npc.id, room, file)

    start = world.start_room
    if start not in room_by_id:
        errors.append(
            f"{loaded.source}: start_room {start!r} is not a declared room"
        )

    # Exits: targets, declared keys, and the explicit-edge map.
    edges: list[_Edge] = []
    for room, file in rooms:
        for direction, value in room.exits.items():
            if isinstance(value, Exit):
                target_id = value.to
                one_way = value.one_way
                if value.locked:
                    if value.key is None:
                        errors.append(
                            f"{file}: room {room.id!r} id {room.id!r}: "
                            f"locked exit {direction!r} to {target_id!r} "
                            f"needs a key; add key: <object_id> or remove "
                            f"locked: true"
                        )
                    else:
                        key = object_by_id.get(value.key)
                        if key is None:
                            errors.append(
                                f"{file}: room {room.id!r} id {room.id!r}: "
                                f"locked exit {direction!r} to {target_id!r} "
                                f"names key {value.key!r}, which is not a "
                                f"declared object"
                            )
                        elif not key.portable:
                            errors.append(
                                f"{file}: room {room.id!r} id {room.id!r}: "
                                f"locked exit {direction!r} to {target_id!r} "
                                f"key {value.key!r} must be portable"
                            )
                elif value.key is not None:
                    errors.append(
                        f"{file}: room {room.id!r} id {room.id!r}: "
                        f"exit {direction!r} names key {value.key!r} but is "
                        f"not locked"
                    )
            else:
                target_id = value
                one_way = False

            if target_id not in room_by_id:
                errors.append(
                    f"{file}: room {room.id!r} id {room.id!r}: exit "
                    f"{direction!r} points to unknown room {target_id!r}"
                )
                continue
            edges.append(
                _Edge(room.id, direction, target_id, one_way, file)
            )

    # A pair declared in both directions is the same exit written twice.
    explicit = {(edge.source, edge.direction): edge for edge in edges}
    reported_conflicts: set[frozenset[tuple[str, str, str]]] = set()
    for edge in edges:
        opposite = OPPOSITE_EXITS.get(edge.direction)
        if opposite is None:
            continue
        reverse = explicit.get((edge.target, opposite))
        if reverse is None:
            continue
        pair = frozenset(
            {
                (edge.source, edge.direction, edge.target),
                (edge.target, opposite, reverse.target),
            }
        )
        if pair in reported_conflicts:
            continue
        reported_conflicts.add(pair)

        if reverse.target == edge.source:
            errors.append(
                f"{edge.file}: room {edge.target!r} id {edge.target!r}: "
                f"exit {opposite!r} to {edge.source!r} duplicates the "
                f"reciprocal of room {edge.source!r} exit "
                f"{edge.direction!r}; declare the exit once and let the "
                f"loader create the other direction"
            )
        else:
            errors.append(
                f"{edge.file}: room {edge.target!r} id {edge.target!r}: "
                f"exit {opposite!r} to {reverse.target!r} collides with the "
                f"reciprocal of room {edge.source!r} exit "
                f"{edge.direction!r} to {edge.target!r}; a room cannot have "
                f"two {opposite!r} exits"
            )

    # Effective graph: declared edges plus the reciprocal the loader creates.
    graph: dict[str, set[str]] = {room_id: set() for room_id in room_by_id}
    effective_exits: dict[str, dict[str, str]] = {
        room_id: {} for room_id in room_by_id
    }
    for edge in edges:
        graph[edge.source].add(edge.target)
        effective_exits[edge.source][edge.direction] = edge.target
        opposite = OPPOSITE_EXITS.get(edge.direction)
        if not edge.one_way and opposite is not None:
            graph[edge.target].add(edge.source)
            effective_exits[edge.target].setdefault(opposite, edge.source)

    if start in room_by_id:
        reachable = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for target in graph.get(current, ()):
                if target not in reachable:
                    reachable.add(target)
                    stack.append(target)

        reported_unreachable: set[str] = set()
        for room, file in rooms:
            if room.id in reachable or room.id in reported_unreachable:
                continue
            reported_unreachable.add(room.id)
            errors.append(
                f"{file}: room {room.id!r} id {room.id!r}: unreachable from "
                f"start_room {start!r}"
            )

    # Containers: every contained id exists, only containers contain, and the
    # containment graph is acyclic (directly or transitively).
    for obj in object_by_id.values():
        file = object_file[obj.id]
        if obj.contains and obj.kind != "container":
            errors.append(
                f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                f"contains objects but kind is {obj.kind!r}, not 'container'"
            )
        for child in obj.contains:
            if child not in object_by_id:
                errors.append(
                    f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                    f"contains unknown object {child!r}"
                )

    cycles = container_cycles(
        {object_id: obj.contains for object_id, obj in object_by_id.items()}
    )
    for cycle in cycles:
        node = cycle[0]
        cycle_text = " -> ".join(repr(part) for part in cycle)
        errors.append(
            f"{object_file[node]}: room {object_room[node]!r} id {node!r}: "
            f"container cycle: {cycle_text}"
        )

    # Object locks use the same key rule as exit locks.
    for obj in object_by_id.values():
        file = object_file[obj.id]
        if not obj.locked:
            if obj.required_key is not None:
                errors.append(
                    f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                    f"names required_key {obj.required_key!r} but is not "
                    f"locked"
                )
            continue
        if obj.kind != "container":
            errors.append(
                f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                f"locked object must be kind 'container'"
            )
        if obj.required_key is None:
            errors.append(
                f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                f"locked container needs required_key; add "
                f"required_key: <portable_object_id>"
            )
            continue
        key = object_by_id.get(obj.required_key)
        if key is None:
            errors.append(
                f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                f"required_key {obj.required_key!r} is not a declared object"
            )
        elif not key.portable:
            errors.append(
                f"{file}: room {object_room[obj.id]!r} id {obj.id!r}: "
                f"required_key {obj.required_key!r} must be portable"
            )

    # Effects: the model checked triggers and the dotted type; the params must
    # survive a JSON round trip because the bus and event log carry them.
    for room, file in rooms:
        _check_effect_params(errors, file, room.id, room.id, room.effects)
        for obj in room.objects:
            _check_effect_params(
                errors, file, room.id, obj.id, obj.effects
            )
        for npc in room.npcs:
            _check_effect_params(
                errors, file, room.id, npc.id, npc.effects
            )

    # Geometry reuses the v2.0 loader's validator. The effective exit map
    # includes the reciprocals the YAML loader will create, so a room only
    # needs to draw the doors its own file declares.
    for room, file in rooms:
        if room.geometry is None:
            continue
        try:
            validate_geometry(
                room.id, room.geometry, effective_exits.get(room.id, {})
            )
        except WorldDataError as exc:
            errors.append(
                f"{file}: room {room.id!r} id {room.id!r}: {exc}"
            )

    return errors


def check_world(path: str | Path) -> list[str]:
    """Validate a world file or directory without touching the database."""
    try:
        loaded = load_world(path)
    except ContentModelError as exc:
        return str(exc).splitlines()
    return validate_world(loaded)

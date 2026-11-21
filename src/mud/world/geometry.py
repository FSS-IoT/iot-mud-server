"""Validation for optional room geometry.

Geometry is authoring data for clients that draw a map. It is additive to the
text contract: a room without geometry, or a client that ignores it, behaves
exactly as before.

Coordinates are zero-based `[x, y]`: `x` is the column from the left (west)
and `y` is the row from the top (north). Cell characters:

    #  wall      .  floor
    +  door      ^  stairs up      v  stairs down

A door sits on one edge of the map, and that edge names its direction. The
validator rejects a map with a clear error naming the room and the offending
cell rather than serving a map a client cannot render.
"""

from __future__ import annotations

from typing import Any, NoReturn

CELL_CHARS = frozenset({"#", ".", "+", "^", "v"})
BOUNDARY_CHARS = frozenset({"#", "+"})
# Cells a player or a prop can stand on. Stairs are walkable floor.
FLOOR_CHARS = frozenset({".", "^", "v"})
DOOR_SIDES = ("north", "south", "west", "east")
MIN_SIZE = 3


class WorldDataError(ValueError):
    """The world file is structurally valid JSON but not a valid world."""


def _fail(room_id: str, message: str) -> NoReturn:
    raise WorldDataError(f"World data error in room {room_id!r}: {message}")


def _cell(
    room_id: str,
    label: str,
    value: Any,
    rows: list[str],
) -> list[int]:
    """Validate one `[x, y]` cell and return it normalized as a list."""
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(part, bool) or not isinstance(part, int)
                   for part in value)):
        _fail(room_id, f"{label} must be an [x, y] pair of integers.")
    x, y = value
    height = len(rows)
    width = len(rows[0]) if rows else 0
    if not (0 <= x < width and 0 <= y < height):
        _fail(room_id,
              f"{label} cell ({x}, {y}) is out of bounds for a "
              f"{width}x{height} map.")
    found = rows[y][x]
    if found not in FLOOR_CHARS:
        _fail(room_id,
              f"{label} cell ({x}, {y}) is not on floor (found {found!r}).")
    return [x, y]


def _inside_door(
    room_id: str,
    side: str,
    x: int,
    y: int,
    rows: list[str],
) -> tuple[int, int]:
    """The walkable cell just inside a boundary door."""
    offsets = {
        "north": (0, 1),
        "south": (0, -1),
        "west": (1, 0),
        "east": (-1, 0),
    }
    dx, dy = offsets[side]
    inside_x, inside_y = x + dx, y + dy
    found = rows[inside_y][inside_x]
    if found != ".":
        _fail(room_id,
              f"door at ({x}, {y}) does not open onto floor: the cell inside "
              f"is {found!r} at ({inside_x}, {inside_y}).")
    return inside_x, inside_y


def validate_geometry(
    room_id: str,
    raw: Any,
    exits: dict[str, Any],
) -> dict[str, Any]:
    """Validate one room's geometry and return the normalized wire shape.

    Raises `WorldDataError` naming the room and the offending cell. The
    returned object always has `cells` and `props`; `spawn` is present only
    when the world file declares it.
    """
    if not isinstance(raw, dict):
        _fail(room_id, "geometry must be an object.")

    unknown = set(raw) - {"cells", "spawn", "props"}
    if unknown:
        names = ", ".join(repr(name) for name in sorted(unknown))
        _fail(room_id, f"geometry has unknown field(s): {names}.")

    if "cells" not in raw:
        _fail(room_id, "geometry.cells is required.")
    cells = raw["cells"]
    if not isinstance(cells, list) or not cells:
        _fail(room_id, "geometry.cells must be a non-empty list of row strings.")

    rows: list[str] = []
    width: int | None = None
    for y, row in enumerate(cells):
        if not isinstance(row, str):
            _fail(room_id, f"geometry.cells row {y} must be a string.")
        if width is None:
            width = len(row)
        elif len(row) != width:
            _fail(room_id,
                  f"geometry.cells has a ragged row {y}: {len(row)} cells, "
                  f"expected {width}.")
        rows.append(row)

    assert width is not None
    height = len(rows)
    if height < MIN_SIZE or width < MIN_SIZE:
        _fail(room_id,
              f"geometry.cells must be at least {MIN_SIZE}x{MIN_SIZE} "
              f"(got {width}x{height}).")

    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char not in CELL_CHARS:
                _fail(room_id,
                      f"geometry.cells has unknown character {char!r} at "
                      f"({x}, {y}).")

    # A closed room: the perimeter is walls and doors only. Floor or stairs on
    # an edge would be a hole in the wall with no exit to explain it.
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if x not in (0, width - 1) and y not in (0, height - 1):
                continue
            if char not in BOUNDARY_CHARS:
                _fail(room_id,
                      f"boundary cell ({x}, {y}) is {char!r}; every boundary "
                      f"cell must be a wall ('#') or a door ('+').")

    doors: dict[str, list[tuple[int, int]]] = {
        side: [] for side in DOOR_SIDES
    }
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char != "+":
                continue
            if x in (0, width - 1) and y in (0, height - 1):
                _fail(room_id,
                      f"door at ({x}, {y}) is in a corner; doors must sit on "
                      f"an edge.")
            if y == 0:
                side = "north"
            elif y == height - 1:
                side = "south"
            elif x == 0:
                side = "west"
            elif x == width - 1:
                side = "east"
            else:
                _fail(room_id,
                      f"door at ({x}, {y}) is not on the boundary; doors must "
                      f"sit on an edge.")
            doors[side].append((x, y))

    for side in DOOR_SIDES:
        has_exit = side in exits
        found = doors[side]
        if found and not has_exit:
            x, y = found[0]
            _fail(room_id,
                  f"door at ({x}, {y}) is on the {side} edge but the room has "
                  f"no {side!r} exit.")
        if has_exit and not found:
            _fail(room_id,
                  f"the room has a {side!r} exit but no door on the {side} "
                  f"edge.")
        if len(found) > 1:
            _fail(room_id,
                  f"the {side} edge has {len(found)} doors; exactly one is "
                  f"allowed.")

    default_spawns: dict[str, tuple[int, int]] = {}
    for side in DOOR_SIDES:
        for x, y in doors[side]:
            default_spawns[side] = _inside_door(room_id, side, x, y, rows)

    stairs = {
        "up": [(x, y) for y, row in enumerate(rows)
               for x, char in enumerate(row) if char == "^"],
        "down": [(x, y) for y, row in enumerate(rows)
                 for x, char in enumerate(row) if char == "v"],
    }
    for exit_name, label, symbol in (
        ("up", "up", "^"),
        ("down", "down", "v"),
    ):
        found = stairs[exit_name]
        if exit_name in exits and not found:
            _fail(room_id,
                  f"the room has an {exit_name!r} exit but no {label} stairs "
                  f"({symbol}).")
        if exit_name not in exits and found:
            x, y = found[0]
            _fail(room_id,
                  f"stairs {label} at ({x}, {y}) but the room has no "
                  f"{exit_name!r} exit.")
        if len(found) > 1:
            _fail(room_id,
                  f"the room has {len(found)} {label} stairs; one {symbol} is "
                  f"allowed per {exit_name!r} exit.")

    spawn: dict[str, list[int]] = {}
    if "spawn" in raw:
        spawn_raw = raw["spawn"]
        if not isinstance(spawn_raw, dict):
            _fail(room_id, "geometry.spawn must be an object of side -> [x, y].")
        for side, value in spawn_raw.items():
            if side not in exits:
                _fail(room_id,
                      f"geometry.spawn names {side!r}, but the room has no "
                      f"{side!r} exit.")
            spawn[side] = _cell(
                room_id, f"geometry.spawn[{side!r}]", value, rows)

    # A client falls back to the cell inside a door when that side has no
    # explicit spawn, so a prop may not occupy either kind of spawn cell.
    occupied = {
        cell for side, cell in default_spawns.items() if side not in spawn
    }
    occupied.update(tuple(cell) for cell in spawn.values())
    props_raw = raw.get("props", [])
    if not isinstance(props_raw, list):
        _fail(room_id, "geometry.props must be a list.")
    props: list[dict[str, Any]] = []
    for index, prop in enumerate(props_raw):
        if not isinstance(prop, dict):
            _fail(room_id, f"geometry.props[{index}] must be an object.")
        unknown = set(prop) - {"kind", "cell"}
        if unknown:
            names = ", ".join(repr(name) for name in sorted(unknown))
            _fail(room_id,
                  f"geometry.props[{index}] has unknown field(s): {names}.")
        kind = prop.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            _fail(room_id,
                  f"geometry.props[{index}] needs a non-empty string kind.")
        if "cell" not in prop:
            _fail(room_id, f"prop {kind!r} has no cell.")
        cell = _cell(room_id, f"prop {kind!r}", prop["cell"], rows)
        if tuple(cell) in occupied:
            _fail(room_id,
                  f"prop {kind!r} cell ({cell[0]}, {cell[1]}) is also a "
                  f"spawn cell.")
        props.append({"kind": kind, "cell": cell})

    geometry: dict[str, Any] = {"cells": rows}
    if "spawn" in raw:
        geometry["spawn"] = spawn
    geometry["props"] = props
    return geometry

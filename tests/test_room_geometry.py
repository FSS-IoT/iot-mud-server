"""Red-green: optional room geometry for map-rendering clients.

Geometry is additive authoring data on a room. The loader validates it at
startup, and the v2 adapter includes it in `room.state` only when the room has
a map and the observer can see (the state is not dark). Text remains the
contract; a client that ignores `geometry` loses nothing.
"""

import json

import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import build_room_state
from src.mud.world.loader import load_demo_village, load_world

# A 7x5 room with one north door at (3, 0), floor inside it at (3, 1).
VALID_CELLS = [
    "###+###",
    "#.....#",
    "#.....#",
    "#.....#",
    "#######",
]


def _world(geometry: dict, exits: dict | None = None) -> dict:
    """A minimal two-room world whose gate carries `geometry`."""
    return {
        "start_location": "gate",
        "rooms": [
            {
                "id": "gate",
                "name": "Village Gate",
                "description": "A heavy gate.",
                "exits": {"north": "market"} if exits is None else exits,
                "item_ids": [],
                "geometry": geometry,
            },
            {
                "id": "market",
                "name": "Marketplace",
                "description": "A market.",
                "exits": {"south": "gate"},
                "item_ids": [],
            },
        ],
        "items": [],
    }


async def _load_temp(tmp_path, geometry: dict, exits: dict | None = None):
    path = tmp_path / "world.json"
    path.write_text(json.dumps(_world(geometry, exits)))
    engine = Engine()
    await load_world(engine, path)
    return engine


def _door_cells(cells: list[str]) -> dict[tuple[int, int], str]:
    """Return {(x, y): side} for every door in the map."""
    height = len(cells)
    width = len(cells[0])
    doors: dict[tuple[int, int], str] = {}
    for y, row in enumerate(cells):
        for x, cell in enumerate(row):
            if cell != "+":
                continue
            if y == 0:
                side = "north"
            elif y == height - 1:
                side = "south"
            elif x == 0:
                side = "west"
            elif x == width - 1:
                side = "east"
            else:
                side = "interior"
            doors[(x, y)] = side
    return doors


@pytest.mark.asyncio
async def test_loader_accepts_valid_geometry(tmp_path):
    geometry = {
        "cells": VALID_CELLS,
        "spawn": {"north": [3, 1]},
        "props": [{"kind": "stall", "cell": [3, 2]}],
    }

    engine = await _load_temp(tmp_path, geometry)
    room = engine.get_room("Village Gate")

    assert room is not None
    assert room.geometry["cells"] == VALID_CELLS
    assert room.geometry["spawn"] == {"north": [3, 1]}
    assert room.geometry["props"] == [{"kind": "stall", "cell": [3, 2]}]


@pytest.mark.asyncio
async def test_loader_rejects_ragged_rows(tmp_path):
    geometry = {
        "cells": [
            "#######",
            "#.....#",
            "#.....",
            "#######",
        ],
    }

    with pytest.raises(ValueError, match="ragged"):
        await _load_temp(tmp_path, geometry)


@pytest.mark.parametrize("cell_char", [".", "^", "v"])
@pytest.mark.asyncio
async def test_loader_rejects_floor_or_stairs_on_boundary(tmp_path, cell_char):
    cells = list(VALID_CELLS)
    row = list(cells[2])
    row[6] = cell_char  # east edge has no exit
    cells[2] = "".join(row)
    geometry = {"cells": cells}

    with pytest.raises(
            ValueError,
            match=r"gate.*boundary cell \(6, 2\).*wall.*door"):
        await _load_temp(tmp_path, geometry)


@pytest.mark.asyncio
async def test_loader_rejects_door_without_exit(tmp_path):
    geometry = {
        "cells": [
            "###+###",
            "#.....#",
            "#.....#",
            "#.....#",
            "###+###",
        ],
    }

    with pytest.raises(ValueError, match=r"gate.*south.*no 'south' exit"):
        await _load_temp(tmp_path, geometry, exits={"north": "market"})


@pytest.mark.asyncio
async def test_loader_rejects_exit_without_door(tmp_path):
    geometry = {
        "cells": [
            "#######",
            "#.....#",
            "#.....#",
            "#.....#",
            "#######",
        ],
    }

    with pytest.raises(ValueError, match=r"gate.*no door"):
        await _load_temp(tmp_path, geometry, exits={"south": "market"})


@pytest.mark.asyncio
async def test_loader_rejects_corner_door(tmp_path):
    geometry = {
        "cells": [
            "#######",
            "#.....#",
            "#.....#",
            "#.....#",
            "+######",
        ],
    }

    with pytest.raises(ValueError, match=r"gate.*corner"):
        await _load_temp(tmp_path, geometry, exits={"south": "market"})


@pytest.mark.asyncio
async def test_loader_rejects_out_of_bounds_spawn(tmp_path):
    geometry = {
        "cells": VALID_CELLS,
        "spawn": {"north": [9, 1]},
    }

    with pytest.raises(ValueError, match=r"gate.*spawn.*out of bounds"):
        await _load_temp(tmp_path, geometry)


@pytest.mark.asyncio
async def test_loader_rejects_out_of_bounds_prop(tmp_path):
    geometry = {
        "cells": VALID_CELLS,
        "props": [{"kind": "stall", "cell": [3, 9]}],
    }

    with pytest.raises(ValueError, match=r"gate.*prop.*out of bounds"):
        await _load_temp(tmp_path, geometry)


@pytest.mark.asyncio
async def test_loader_accepts_stairs_matching_up_and_down_exits(tmp_path):
    geometry = {
        "cells": [
            "#######",
            "#..^..#",
            "#.....#",
            "#..v..#",
            "#######",
        ],
        "spawn": {"up": [3, 1], "down": [3, 3]},
    }

    engine = await _load_temp(
        tmp_path, geometry, exits={"up": "market", "down": "market"})
    room = engine.get_room("Village Gate")

    assert room is not None
    assert room.geometry["cells"] == geometry["cells"]
    assert set(room.exits) == {"up", "down"}


@pytest.mark.parametrize(
    ("stairs", "exit_name", "label"),
    [("^", "up", "up"), ("v", "down", "down")],
)
@pytest.mark.asyncio
async def test_loader_rejects_stairs_without_matching_exit(
        tmp_path, stairs, exit_name, label):
    geometry = {
        "cells": [
            "###+###",
            f"#.{stairs}...#",
            "#.....#",
            "#.....#",
            "#######",
        ],
    }

    with pytest.raises(ValueError, match=rf"gate.*stairs {label}.*no '{exit_name}' exit"):
        await _load_temp(tmp_path, geometry, exits={"north": "market"})


@pytest.mark.asyncio
async def test_loader_rejects_prop_on_spawn(tmp_path):
    geometry = {
        "cells": VALID_CELLS,
        "spawn": {"north": [3, 1]},
        "props": [{"kind": "stall", "cell": [3, 1]}],
    }

    with pytest.raises(ValueError, match=r"gate.*prop.*spawn"):
        await _load_temp(tmp_path, geometry)


@pytest.mark.asyncio
async def test_gate_room_state_has_north_and_south_doors():
    engine = Engine()
    await load_demo_village(engine)
    player = engine.register_player("Ada")

    state = build_room_state(engine, player)
    room = state["room"]

    assert room["id"] == "gate"
    geometry = room["geometry"]
    doors = _door_cells(geometry["cells"])
    # The product world adds the south road through the gate (#80); the
    # original north door to the marketplace is unchanged.
    assert geometry["cells"][0] == "###+###"
    assert geometry["cells"][-1] == "###+###"
    assert sorted(doors.values()) == ["north", "south"]
    assert len(geometry["cells"]) >= 3
    assert all(len(row) == len(geometry["cells"][0]) for row in geometry["cells"])


@pytest.mark.asyncio
async def test_market_geometry_matches_its_exits():
    engine = Engine()
    await load_demo_village(engine)
    player = engine.register_player("Ada")

    moved, _ = await engine.move_player("Ada", "north")
    assert moved
    state = build_room_state(engine, player)
    room = state["room"]

    assert room["id"] == "market"
    geometry = room["geometry"]
    cells = geometry["cells"]
    assert len(cells[0]) == 11
    assert len(cells) == 9

    doors = _door_cells(cells)
    assert set(doors.values()) == {"north", "south", "east", "west"}
    assert len(doors) == 4

    links = {exit_["direction"]: exit_["to"] for exit_ in room["exits"]}
    assert links == {
        "north": "forest",
        "south": "gate",
        "east": "tavern",
        "west": "shop",
    }
    assert set(links) == set(doors.values())


@pytest.mark.asyncio
async def test_starting_village_geometry_matches_every_exit():
    engine = Engine()
    await load_demo_village(engine)

    expected = {
        "gate": {"north", "south"},
        "market": {"north", "south", "east", "west"},
        "tavern": {"west"},
        "shop": {"east"},
        "forest": {"south"},
    }
    for room_id, sides in expected.items():
        room = next(r for r in engine.rooms.values() if r.item_id == room_id)
        assert room.geometry is not None, room_id
        doors = _door_cells(room.geometry["cells"])
        assert set(doors.values()) == sides, room_id
        assert set(room.exits) == sides, room_id


@pytest.mark.asyncio
async def test_dark_room_state_omits_geometry():
    engine = Engine()
    await load_demo_village(engine)
    player = engine.register_player("Ada")

    await engine.move_player("Ada", "north")
    await engine.move_player("Ada", "north")
    state = build_room_state(engine, player)
    room = state["room"]

    assert room["id"] == "forest"
    assert room["dark"] is True
    assert "geometry" not in room


@pytest.mark.asyncio
async def test_room_without_geometry_omits_the_field():
    engine = Engine()
    room = Room("Bare Room", "Nothing here.")
    room.item_id = "bare"
    engine._rooms[room.name] = room
    engine._player_location = room
    player = engine.register_player("Ada")

    state = build_room_state(engine, player)
    payload = state["room"]

    assert "geometry" not in payload
    assert "geometry" not in json.dumps(payload)

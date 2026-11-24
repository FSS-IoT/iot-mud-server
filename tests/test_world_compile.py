"""#132: compile the world once, so the engine and the seeder agree.

The engine loader and the SQLite seeder used to expand reciprocal exits, locks,
containment, and geometry independently. These tests pin the two places they
could disagree and the shared compiled value that removes the drift.
"""

from __future__ import annotations

import asyncio

from src.mud.content.models import load_world
from src.mud.content.validator import validate_world
from src.mud.content.world import compile_world
from src.mud.engine.core import Engine
from src.mud.storage.db import open_database
from src.mud.world.loader import load_world as load_engine_world
from src.mud.world.seed import seed_world

WORLD = """\
name: Locked Rooms
start_room: hall
rng_seed: 99
rooms:
  - id: hall
    name: Hall
    description: A bare hall.
    exits:
      north:
        to: vault
        locked: true
        key: brass_key
        label: iron door
  - id: vault
    name: Vault
    description: A cold vault.
    objects:
      - id: brass_key
        name: Brass Key
        description: A small brass key.
"""


def _write_world(tmp_path):
    path = tmp_path / "locked.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


def test_reciprocal_lock_label_matches_between_seed_and_loader(tmp_path):
    """Both directions of the door carry the same lock label, in DB and engine.

    Fails on the parent commit: the seed wrote `label = None` for the reciprocal
    while the loader shared the declaring side's label.
    """
    world_path = _write_world(tmp_path)

    engine = Engine()
    asyncio.run(load_engine_world(engine, world_path))
    hall = engine.get_room("Hall")
    vault = engine.get_room("Vault")
    north_lock = hall.exit_locks["north"]
    south_lock = vault.exit_locks["south"]
    assert north_lock is south_lock, "one physical door, one lock instance"
    assert north_lock.label == "iron door"

    conn = open_database(tmp_path / "mud.db")
    try:
        seed_world(conn, world_path, epoch="test-epoch")
        north = conn.execute(
            "SELECT * FROM exits WHERE room_id = 'hall' AND direction = 'north'"
        ).fetchone()
        south = conn.execute(
            "SELECT * FROM exits WHERE room_id = 'vault' "
            "AND direction = 'south'"
        ).fetchone()
    finally:
        conn.close()

    assert north["locked"] == 1 and south["locked"] == 1
    assert north["key_id"] == south["key_id"] == "brass_key"
    assert north["label"] == south["label"] == "iron door"


def test_one_effective_exit_graph(tmp_path):
    """A two-way locked door is one shared lock and one edge per side."""
    loaded = load_world(_write_world(tmp_path))
    assert validate_world(loaded) == []

    compiled = compile_world(loaded)

    door_edges = [edge for edge in compiled.exits if edge.lock_id is not None]
    assert {(edge.room_id, edge.direction) for edge in door_edges} == {
        ("hall", "north"),
        ("vault", "south"),
    }
    assert len({edge.lock_id for edge in door_edges}) == 1
    lock_id = door_edges[0].lock_id
    assert compiled.locks[lock_id].key == "brass_key"
    assert compiled.locks[lock_id].label == "iron door"

    # One effective edge per side, and no duplicate graph entries.
    assert len(compiled.exits) == len(
        {(edge.room_id, edge.direction) for edge in compiled.exits}
    )
    assert compiled.exit_map() == {
        "hall": {"north": "vault"},
        "vault": {"south": "hall"},
    }

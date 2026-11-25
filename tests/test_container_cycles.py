"""Ticket #79: a container cannot contain itself, directly or transitively.

The validator half pins the graph rule #65 already ships: `content check`
rejects an authored direct or transitive containment cycle. The runtime half
is the new guard: `put <item> in <container>` refuses to close a cycle and
leaves both sides exactly where they were, at the engine and as the adapter's
typed `container_cycle` error.

The runtime setup puts one entity in two collections on purpose. Normal play
cannot reach the guard (the container must be in the room, the item in the
inventory, and the container must already sit inside the item), so the guard
is defense-in-depth for any future path that adds contents.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.mud.content import Entity, Player, Room
from src.mud.content.validator import container_cycles
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import execute
from src.mud.protocol.v2 import ERROR_CODES

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- Validator: authored cycles fail `content check` ------------------------


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    """Run `content check` exactly as a content author would."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "src.mud.admin",
            "content",
            "check",
            "--world",
            str(path),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write(tmp_path: Path, objects: list[dict]) -> Path:
    data = {
        "name": "Cycle Test World",
        "start_room": "gate",
        "rng_seed": 7,
        "rooms": [
            {
                "id": "gate",
                "name": "Gate",
                "description": "A gate.",
                "objects": objects,
            }
        ],
    }
    path = tmp_path / "world.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _container(ident: str, contains: list[str]) -> dict:
    return {
        "id": ident,
        "name": ident,
        "kind": "container",
        "contains": contains,
    }


def test_direct_container_cycle_fails_content_check(tmp_path):
    path = _write(tmp_path, [_container("chest", ["chest"])])

    result = _check(path)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "cycle" in output.lower()
    assert "'chest'" in output


def test_transitive_container_cycle_fails_content_check(tmp_path):
    path = _write(
        tmp_path,
        [_container("a", ["b"]), _container("b", ["a"])],
    )

    result = _check(path)
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "cycle" in output.lower()
    assert "'a'" in output
    assert "'b'" in output


def test_container_cycles_is_the_shared_rule():
    """The validator and the runtime must use this one graph rule."""
    assert container_cycles({"chest": ["chest"]}) == [["chest", "chest"]]
    assert container_cycles({"a": ["b"], "b": ["a"]}) == [
        ["a", "b", "a"]
    ]
    assert container_cycles({"a": ["b"], "b": []}) == []


def test_consistent_nesting_still_passes_content_check(tmp_path):
    """The cycle rule must not reject ordinary nesting."""
    path = _write(
        tmp_path,
        [_container("chest", ["pouch"]), _container("pouch", [])],
    )

    result = _check(path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_container_cycle_is_a_shared_error_code():
    """The adapter's new code is part of the protocol contract, not just docs."""
    assert "container_cycle" in ERROR_CODES


# --- Runtime: put refuses a self or descendant insertion --------------------


def _market() -> tuple[Engine, Room, Player]:
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    engine._rooms[room.name] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    return engine, room, player


def _container_entity(ident: str, name: str) -> Entity:
    return Entity(
        name,
        f"A {name.lower()}.",
        item_id=ident,
        container=True,
        open=True,
    )


@pytest.mark.asyncio
async def test_put_a_container_in_itself_is_refused():
    engine, room, player = _market()
    chest = _container_entity("chest", "Chest")
    room.add_content(chest)
    player.add_content(chest)
    before_room = list(room.contents)
    before_inventory = list(player.contents)

    response = await engine.process_command("put chest in chest", "Ada")

    assert "cannot contain itself" in response.lower()
    assert room.contents == before_room
    assert player.contents == before_inventory
    assert chest not in chest.contents


@pytest.mark.asyncio
async def test_put_a_container_into_its_descendant_is_refused():
    engine, room, player = _market()
    outer = _container_entity("outer_chest", "Outer Chest")
    inner = _container_entity("inner_chest", "Inner Chest")
    outer.add_content(inner)
    room.add_content(inner)
    player.add_content(outer)
    before_room = list(room.contents)
    before_inventory = list(player.contents)

    response = await engine.process_command(
        "put outer chest in inner chest", "Ada"
    )

    assert "cannot contain itself" in response.lower()
    assert room.contents == before_room
    assert player.contents == before_inventory
    assert inner in outer.contents
    assert outer not in inner.contents


@pytest.mark.asyncio
async def test_put_a_container_into_a_deep_descendant_is_refused():
    engine, room, player = _market()
    outer = _container_entity("outer_chest", "Outer Chest")
    middle = _container_entity("middle_chest", "Middle Chest")
    inner = _container_entity("inner_chest", "Inner Chest")
    outer.add_content(middle)
    middle.add_content(inner)
    room.add_content(inner)
    player.add_content(outer)
    before_room = list(room.contents)
    before_inventory = list(player.contents)

    response = await engine.process_command(
        "put outer chest in inner chest", "Ada"
    )

    assert "cannot contain itself" in response.lower()
    assert room.contents == before_room
    assert player.contents == before_inventory
    assert middle in outer.contents
    assert inner in middle.contents
    assert outer not in inner.contents


@pytest.mark.asyncio
async def test_put_a_container_into_an_unrelated_container_still_works():
    engine, room, player = _market()
    chest = _container_entity("chest", "Chest")
    pouch = _container_entity("pouch", "Pouch")
    room.add_content(chest)
    player.add_content(pouch)

    response = await engine.process_command("put pouch in chest", "Ada")

    assert response == "You put the Pouch in the Chest."
    assert pouch in chest.contents
    assert pouch not in player.contents


@pytest.mark.asyncio
async def test_self_put_is_a_typed_container_cycle_error():
    engine, room, player = _market()
    chest = _container_entity("chest", "Chest")
    room.add_content(chest)
    player.add_content(chest)
    before_room = list(room.contents)
    before_inventory = list(player.contents)

    result = await execute(engine, "Ada", "put chest in chest")

    assert [event.type for event in result.events] == ["error"]
    error = result.events[0]
    assert error.data["code"] == "container_cycle"
    assert "cannot contain itself" in error.data["message"].lower()
    assert error.data["object"]["id"] == "chest"
    assert error.data["container"]["id"] == "chest"
    assert room.contents == before_room
    assert player.contents == before_inventory


@pytest.mark.asyncio
async def test_descendant_put_is_a_typed_container_cycle_error():
    engine, room, player = _market()
    outer = _container_entity("outer_chest", "Outer Chest")
    inner = _container_entity("inner_chest", "Inner Chest")
    outer.add_content(inner)
    room.add_content(inner)
    player.add_content(outer)
    before_room = list(room.contents)
    before_inventory = list(player.contents)

    result = await execute(
        engine, "Ada", "put outer chest in inner chest"
    )

    assert [event.type for event in result.events] == ["error"]
    error = result.events[0]
    assert error.data["code"] == "container_cycle"
    assert error.data["object"]["id"] == "outer_chest"
    assert error.data["container"]["id"] == "inner_chest"
    assert room.contents == before_room
    assert player.contents == before_inventory
    assert inner in outer.contents
    assert outer not in inner.contents

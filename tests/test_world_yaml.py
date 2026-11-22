"""Ticket #66: the starting village is YAML and loads the same world.

The product world moves from `starting_village.json` to
`starting_village.yaml` in the #65 format. These tests compare the *effective*
engine graph, not the files: names, descriptions, exits (including the
reciprocals the loader creates), objects and their containment, the start room,
and every room's geometry must be identical whichever file the loader reads.

They also pin the startup contract: a missing, malformed, or semantically
invalid YAML world fails with the content model or validator message instead of
booting a half-built world.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.mud.config import DEFAULT_WORLD
from src.mud.content import Player
from src.mud.content.models import OPPOSITE_EXITS, ContentModelError
from src.mud.engine.core import Engine
from src.mud.world.geometry import WorldDataError
from src.mud.world.loader import load_demo_village, load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
JSON_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.json"
YAML_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.yaml"


async def _load(path: Path) -> Engine:
    engine = Engine()
    await load_world(engine, path)
    return engine


def _entity_snapshot(entity) -> dict:
    """The engine state an object contributes, independent of its source."""
    return {
        "item_id": entity.item_id,
        "instance_id": entity.instance_id,
        "name": entity.name,
        "description": entity.description,
        "weight": entity.weight,
        "readable": entity.readable,
        "light_source": entity.light_source,
        "lit": entity.lit,
        "locked": entity.locked,
        "required_key": entity.required_key,
        "dropped_on_sleep": entity.dropped_on_sleep,
        "contents": sorted(
            (_entity_snapshot(child) for child in entity.contents),
            key=lambda child: child["item_id"] or child["name"],
        ),
    }


def _graph(engine: Engine) -> dict:
    """The effective world: what a player can reach and see."""
    rooms = {}
    for room in engine.rooms.values():
        rooms[room.item_id] = {
            "name": room.name,
            "description": room.description,
            "dark": room.dark,
            "exits": {
                direction: target.item_id
                for direction, target in sorted(room.exits.items())
            },
            "objects": sorted(
                (
                    _entity_snapshot(obj)
                    for obj in room.contents
                    if not isinstance(obj, Player)
                ),
                key=lambda obj: obj["item_id"] or obj["name"],
            ),
            "geometry": room.geometry,
        }
    return {
        "start": (
            engine.current_location.item_id
            if engine.current_location is not None
            else None
        ),
        "rooms": rooms,
    }


# ---------------------------------------------------------------------------
# Same effective world
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_world_matches_json_effective_graph():
    json_engine = await _load(JSON_WORLD)
    yaml_engine = await _load(YAML_WORLD)

    assert _graph(yaml_engine) == _graph(json_engine)


@pytest.mark.asyncio
async def test_yaml_loader_creates_the_reciprocal_exits():
    engine = await _load(YAML_WORLD)
    gate = next(r for r in engine.rooms.values() if r.item_id == "gate")
    market = next(r for r in engine.rooms.values() if r.item_id == "market")

    assert gate.exits["north"] is market
    assert market.exits["south"] is gate
    assert set(market.exits) == {"north", "south", "east", "west"}


def test_yaml_declares_each_exit_once():
    """The YAML file never hand-declares a reciprocal the loader creates."""
    data = yaml.safe_load(YAML_WORLD.read_text(encoding="utf-8"))
    declared: dict[tuple[str, str], str] = {}
    for room in data["rooms"]:
        for direction, value in (room.get("exits") or {}).items():
            target = value["to"] if isinstance(value, dict) else value
            declared[(room["id"], direction)] = target

    assert declared == {
        ("gate", "north"): "market",
        ("market", "west"): "shop",
        ("market", "north"): "forest",
        ("market", "east"): "tavern",
    }
    for (room_id, direction), target in declared.items():
        opposite = OPPOSITE_EXITS[direction]
        assert (target, opposite) not in declared, (
            f"{room_id} {direction} -> {target} is declared twice"
        )


def test_yaml_metadata_uses_the_json_start_room():
    data = yaml.safe_load(YAML_WORLD.read_text(encoding="utf-8"))
    json_data = json.loads(JSON_WORLD.read_text(encoding="utf-8"))

    assert data["start_room"] == json_data["start_location"]
    assert isinstance(data["rng_seed"], int)
    assert data["name"]


def test_default_world_is_yaml():
    assert DEFAULT_WORLD == Path("data/worlds/starting_village.yaml")
    assert DEFAULT_WORLD.suffix == ".yaml"


@pytest.mark.asyncio
async def test_load_demo_village_builds_the_same_world_as_json():
    default_engine = Engine()
    await load_demo_village(default_engine)
    json_engine = await _load(JSON_WORLD)

    assert _graph(default_engine) == _graph(json_engine)


# ---------------------------------------------------------------------------
# Startup failures use the model or validator message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_yaml_world_fails_startup(tmp_path):
    with pytest.raises(ContentModelError, match="world not found"):
        await _load(tmp_path / "missing.yaml")


@pytest.mark.asyncio
async def test_invalid_yaml_fails_startup_with_the_model_message(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")

    with pytest.raises(ContentModelError, match="invalid YAML"):
        await _load(path)


@pytest.mark.asyncio
async def test_semantically_invalid_yaml_fails_startup(tmp_path):
    path = tmp_path / "dangling.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "Dangling World",
                "start_room": "gate",
                "rng_seed": 7,
                "rooms": [
                    {
                        "id": "gate",
                        "name": "Gate",
                        "description": "A gate.",
                        "exits": {"north": "nowhere"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorldDataError, match="unknown room"):
        await _load(path)

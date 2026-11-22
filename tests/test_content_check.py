"""Ticket #65: `content check` rejects every malformed world.

Each test drives the real command:

    python -m src.mud.admin content check --world <path>

A rejected world exits 1 and the message names the file, the room, and the
offending id. The valid world ships under `data/worlds/` and exits 0.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_WORLD = REPO_ROOT / "data" / "worlds" / "example_world.yaml"


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


def _write(tmp_path: Path, data: dict, name: str = "world.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _room(room_id: str, **overrides) -> dict:
    room = {
        "id": room_id,
        "name": room_id.title(),
        "description": f"The {room_id}.",
    }
    room.update(overrides)
    return room


def _world(rooms: list[dict], **overrides) -> dict:
    data = {
        "name": "Test World",
        "start_room": "gate",
        "rng_seed": 7,
        "rooms": rooms,
    }
    data.update(overrides)
    return data


def _two_rooms(gate: dict | None = None, market: dict | None = None) -> dict:
    """A minimal reachable world: gate --north--> market."""
    gate_room = _room("gate", exits={"north": "market"})
    if gate:
        gate_room.update(gate)
    market_room = _room("market")
    if market:
        market_room.update(market)
    return _world([gate_room, market_room])


def _assert_rejected(
    result: subprocess.CompletedProcess[str],
    path: Path,
    *fragments: str,
) -> None:
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert path.name in result.stderr
    for fragment in fragments:
        assert fragment in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Valid world
# ---------------------------------------------------------------------------


def test_valid_yaml_world_exits_zero():
    result = _check(VALID_WORLD)

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_valid_world_directory_exits_zero(tmp_path):
    """One `world.yaml` plus `areas/*.yaml`, the format parallel authors use."""
    (tmp_path / "areas").mkdir()
    (tmp_path / "world.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "Directory World",
                "start_room": "gate",
                "rng_seed": 7,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "areas" / "village.yaml").write_text(
        yaml.safe_dump(
            {
                "area": "village",
                "rooms": [
                    _room("gate", exits={"north": "market"}),
                    _room("market"),
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _check(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_directory_error_names_the_area_file(tmp_path):
    (tmp_path / "areas").mkdir()
    (tmp_path / "world.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "Directory World",
                "start_room": "gate",
                "rng_seed": 7,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "areas" / "village.yaml").write_text(
        yaml.safe_dump(
            {
                "area": "village",
                "rooms": [_room("gate", exits={"north": "nowhere"})],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _check(tmp_path)

    assert result.returncode == 1
    assert "village.yaml" in result.stderr
    assert "'gate'" in result.stderr
    assert "'nowhere'" in result.stderr


def test_one_way_exit_is_valid(tmp_path):
    data = _world(
        [
            _room(
                "gate",
                exits={"north": {"to": "market", "one_way": True}},
            ),
            _room("market"),
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    assert result.returncode == 0, result.stderr


def test_invalid_yaml_is_rejected_without_a_traceback(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")

    result = _check(path)

    assert result.returncode == 1
    assert path.name in result.stderr
    assert "YAML" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# Duplicate ids
# ---------------------------------------------------------------------------


def test_duplicate_object_ids_are_rejected(tmp_path):
    data = _two_rooms(
        gate={
            "objects": [
                {"id": "lamp", "name": "a lamp", "description": "A lamp."},
            ]
        },
        market={
            "objects": [
                {"id": "lamp", "name": "another lamp", "description": "A lamp."},
            ]
        },
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "duplicate", "'lamp'", "'gate'", "'market'")


def test_duplicate_id_across_kinds_is_rejected(tmp_path):
    """Rooms, objects, and NPCs share one global id namespace."""
    data = _two_rooms(
        gate={
            "objects": [
                {"id": "market", "name": "a sign", "description": "A sign."},
            ]
        },
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "duplicate", "'market'", "room")


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def test_dangling_exit_is_rejected(tmp_path):
    data = _world([_room("gate", exits={"north": "nowhere"})])
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "exit 'north'", "'nowhere'")


def test_hand_declared_reciprocal_exit_is_rejected(tmp_path):
    data = _world(
        [
            _room("gate", exits={"north": "market"}),
            _room("market", exits={"south": "gate"}),
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(
        result, path, "'gate'", "'market'", "north", "south", "reciprocal"
    )


def test_unreachable_room_is_rejected(tmp_path):
    data = _world([_room("gate"), _room("island")])
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'island'", "unreachable", "'gate'")


def test_missing_start_room_is_rejected(tmp_path):
    data = _world([_room("gate")], start_room="nowhere")
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "start_room", "'nowhere'")


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def test_direct_container_cycle_is_rejected(tmp_path):
    data = _world(
        [
            _room(
                "gate",
                objects=[
                    {
                        "id": "chest",
                        "name": "chest",
                        "kind": "container",
                        "contains": ["chest"],
                    }
                ],
            )
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "'chest'", "cycle")


def test_transitive_container_cycle_is_rejected(tmp_path):
    data = _world(
        [
            _room(
                "gate",
                objects=[
                    {
                        "id": "a",
                        "name": "box a",
                        "kind": "container",
                        "contains": ["b"],
                    },
                    {
                        "id": "b",
                        "name": "box b",
                        "kind": "container",
                        "contains": ["c"],
                    },
                    {
                        "id": "c",
                        "name": "box c",
                        "kind": "container",
                        "contains": ["a"],
                    },
                ],
            )
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "cycle", "'a'", "'b'", "'c'")


def test_unknown_contained_object_is_rejected(tmp_path):
    data = _world(
        [
            _room(
                "gate",
                objects=[
                    {
                        "id": "chest",
                        "name": "chest",
                        "kind": "container",
                        "contains": ["missing_loot"],
                    }
                ],
            )
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(
        result, path, "'gate'", "'chest'", "'missing_loot'"
    )


def test_container_cycle_helper_is_reusable_by_the_runtime():
    """v2.1.2 imports the same graph rule instead of reimplementing it."""
    from src.mud.content.validator import container_cycles

    assert container_cycles({"a": ["b"], "b": ["c"], "c": ["a"]}) == [
        ["a", "b", "c", "a"]
    ]
    assert container_cycles({"chest": ["chest"]}) == [["chest", "chest"]]
    assert container_cycles({"a": ["b"], "b": []}) == []


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_data", "fragment"),
    [
        ({"to": "market", "locked": True}, "needs"),
        (
            {"to": "market", "locked": True, "key": "missing_key"},
            "'missing_key'",
        ),
    ],
)
def test_missing_exit_key_is_rejected(tmp_path, exit_data, fragment):
    data = _two_rooms(gate={"exits": {"north": exit_data}})
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "'market'", fragment)


def test_key_must_be_portable(tmp_path):
    data = _two_rooms(
        gate={
            "exits": {"north": {"to": "market", "locked": True, "key": "rock"}},
            "objects": [
                {
                    "id": "rock",
                    "name": "rock",
                    "description": "Too heavy to carry.",
                    "portable": False,
                }
            ],
        }
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "'rock'", "portable")


def test_locked_container_needs_a_key(tmp_path):
    data = _world(
        [
            _room(
                "gate",
                objects=[
                    {
                        "id": "chest",
                        "name": "chest",
                        "kind": "container",
                        "locked": True,
                    }
                ],
            )
        ]
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "'chest'", "required_key")


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effects", "fragment"),
    [
        (
            {"on_enter": [{"params": {"intensity": 0.3}}]},
            "type",
        ),
        (
            {"on_enter": [{"type": "Light Dim"}]},
            "type",
        ),
        (
            {"on_enter": [{"type": "light.dim", "params": [1, 2]}]},
            "params",
        ),
        (
            {"on_sneeze": [{"type": "light.dim"}]},
            "on_sneeze",
        ),
        (
            {
                "on_enter": [
                    {
                        "type": "light.dim",
                        "params": {"when": datetime.date(2026, 11, 21)},
                    }
                ]
            },
            "params",
        ),
    ],
)
def test_malformed_effect_is_rejected(tmp_path, effects, fragment):
    data = _world([_room("gate", effects=effects)])
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "effects", fragment)


# ---------------------------------------------------------------------------
# Geometry (carried into YAML by #66)
# ---------------------------------------------------------------------------


def test_geometry_that_disagrees_with_exits_is_rejected(tmp_path):
    data = _two_rooms(
        gate={
            "geometry": {
                "cells": [
                    "##+##",
                    "#...#",
                    "#...#",
                    "#...#",
                    "##+##",
                ]
            }
        }
    )
    path = _write(tmp_path, data)

    result = _check(path)

    _assert_rejected(result, path, "'gate'", "south", "door")

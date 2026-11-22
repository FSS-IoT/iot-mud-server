"""Scenario-level golden checks (#83).

The coverage test in `tests/test_golden_coverage.py` proves the union of the
runs reaches every documented event and every room. These tests pin the shape
of each individual scenario, and prove the wandering path is the same on a
second fresh server.
"""

from __future__ import annotations

import pytest

from tests.golden import scenarios
from tests.golden.harness import ALL_AREAS, ALL_ROOMS


@pytest.mark.asyncio
async def test_full_tour_visits_every_room_and_area(tmp_path):
    run = await scenarios.full_tour(tmp_path / "full_tour")

    assert run.rooms == ALL_ROOMS
    assert run.areas == ALL_AREAS
    # The tour's own paths: every delivered event family is present, not only
    # the lock and container events. The coverage test adds the rest.
    assert {
        "hello", "auth.ok", "room.state", "room.enter", "room.leave",
        "npc.say", "player.said", "player.emoted", "player.told", "who.list",
        "object.taken", "object.dropped", "object.opened", "object.closed",
        "object.put", "object.got", "object.read", "object.lit",
        "object.unlocked", "exit.locked", "exit.unlocked",
        "player.inventory", "notice", "error",
    } <= run.events


@pytest.mark.asyncio
async def test_social_covers_private_speech_and_the_sleep_path(tmp_path):
    run = await scenarios.social(tmp_path / "social")

    assert {
        "hello", "auth.ok", "auth.error", "room.state", "room.enter",
        "room.leave", "player.said", "player.emoted", "player.told",
        "who.list", "error",
    } <= run.events
    assert "gate" in run.rooms and "market" in run.rooms


@pytest.mark.asyncio
async def test_guest_session_covers_the_v20_session_events(tmp_path):
    run = await scenarios.guest_session(tmp_path / "guest_session")

    assert {
        "hello", "session.ok", "room.state", "room.enter", "room.leave",
        "error",
    } <= run.events


@pytest.mark.asyncio
async def test_npc_wander_replays_the_same_seeded_trail(tmp_path):
    first = await scenarios.npc_wander(tmp_path / "first")
    second = await scenarios.npc_wander(tmp_path / "second")

    assert first.detail["trail"] == second.detail["trail"]
    merchant = [
        (tick, source, target, direction)
        for tick, npc_id, source, target, direction in first.detail["trail"]
        if npc_id == "npc_merchant"
    ]
    assert merchant[:2] == [
        (20, "market", "forest", "north"),
        (40, "forest", "market", "south"),
    ]
    assert {"npc.enter", "npc.leave"} <= first.events

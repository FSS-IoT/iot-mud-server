"""Red-green fixes for bug 3: movement and position awareness for registered players.

Bug 3: `go` (and the other interaction commands) only moved a legacy
single-player slot. A registered, named player never actually moved — so
`look`, which reads the player's `.location`, reported a stale room. Movement
and item handling were not shared between the two slots. The engine now has one
required actor per command (#133).

This file covers engine movement: `move_player(name, direction)` moves a
registered player's location and emits a `player_moved` semantic event
(name / from / to), and `go` routes through it. The v1 server that broadcast
these events was retired in #137; the v2 server's room events cover the wire.
"""

import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine

# ---------------------------------------------------------------------------
# Pure engine: a registered player actually moves
# ---------------------------------------------------------------------------

def _two_room_engine():
    """gate --north--> market, both rooms registered, legacy start = gate."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    market = Room("Marketplace", "Lightly vendors cry out their wares.")
    gate.exits["north"] = market
    engine._rooms["Village Gate"] = gate
    engine._rooms["Marketplace"] = market
    engine.start_room = gate
    return engine


@pytest.mark.asyncio
async def test_registered_player_moves_to_destination():
    """`go north` as a registered player relocates that player's location."""
    engine = _two_room_engine()
    player = engine.register_player("Ada")
    assert player.location.name == "Village Gate"

    response = await engine.process_command("go north", "Ada")

    assert player.location.name == "Marketplace"
    assert "Marketplace" in response
    market = engine._rooms["Marketplace"]
    assert [c for c in market.contents if c is player] == [player]


@pytest.mark.asyncio
async def test_registered_player_cannot_go_without_exit():
    """No exit in a direction -> clear refusal, player stays put."""
    engine = _two_room_engine()  # gate only has a north exit
    engine.register_player("Ada")

    response = await engine.process_command("go south", "Ada")

    assert response == "You cannot go south."


@pytest.mark.asyncio
async def test_registered_move_emits_player_moved():
    """Moving emits a `player_moved` event carrying name / from / to."""
    engine = _two_room_engine()
    events = []
    await engine.subscribe(
        "player_moved", lambda et, d: events.append((et, dict(d)))
    )
    engine.register_player("Ada")

    await engine.process_command("go north", "Ada")

    assert ("player_moved", {
        "name": "Ada", "from": "Village Gate", "to": "Marketplace",
    }) in events


@pytest.mark.asyncio
async def test_legacy_move_still_works_without_player_name():
    """Moving the single actor updates that actor's location."""
    engine = _two_room_engine()
    player = engine.register_player("Adventurer")

    await engine.process_command("go north", player)

    assert player.location.name == "Marketplace"


import pytest

from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village


@pytest.mark.asyncio
async def test_demo_village_populates_world_green():
    """The product world has 30-40 rooms and the starting village inside it."""
    engine = Engine()
    
    # Action: Load the demo scene
    await load_demo_village(engine)
    
    # Assertions: the v2.1.3 world size
    assert 30 <= len(engine.rooms) <= 40, (
        f"Expected 30-40 rooms in demo, but found {len(engine.rooms)}"
    )
    
    # Assertions: Check specific known rooms/entities are present
    room_names = [r.name for r in engine.rooms.values()]
    assert "Village Gate" in room_names
    assert "Old Tavern" in room_names
    assert "Mysterious Forest" in room_names
    
    # Assertion: Items count across all rooms/player
    total_items = sum(len(r.contents) for r in engine.rooms.values())
    if engine.player:
        total_items += len(engine.player.contents)
    
    assert total_items >= 10, f"Expected at least 10 items in the world, but found {total_items}"


@pytest.mark.asyncio
async def test_every_room_is_reachable_from_start():
    """No orphan rooms: every exit graph node is reachable from the start."""
    engine = Engine()
    await load_demo_village(engine)

    start = engine._player_location
    seen = {start}
    queue = [start]
    while queue:
        room = queue.pop()
        for target in room.exits.values():
            if target not in seen:
                seen.add(target)
                queue.append(target)

    assert seen == set(engine.rooms.values())

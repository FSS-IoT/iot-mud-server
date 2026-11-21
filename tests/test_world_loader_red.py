import pytest
from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village

@pytest.mark.asyncio
async def test_demo_village_populates_world_green():
    """[TASK-014] GREEN PHASE: Verify that the loader populates exactly 5 rooms and at least 3 items."""
    engine = Engine()
    
    # Action: Load the demo scene
    await load_demo_village(engine)
    
    # Assertions: 5 Rooms
    assert len(engine.rooms) == 5, f"Expected 5 rooms in demo, but found {len(engine.rooms)}"
    
    # Assertions: Check specific known rooms/entities are present
    room_names = [r.name for r in engine.rooms.values()]
    assert "Village Gate" in room_names
    assert "Old Tavern" in room_names
    assert "Mysterious Forest" in room_names
    
    # Assertion: Items count across all rooms/player
    total_items = sum(len(r.contents) for r in engine.rooms.values())
    if engine.player:
        total_items += len(engine.player.contents)
    
    assert total_items >= 3, f"Expected at least 3 items in the world, but found {total_items}"

import pytest

from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_engine_initialization_is_empty():
    """[TASK-013] RED PHASE: Confirm a fresh engine instance is truly empty and uses public APIs."""
    engine = Engine()
    
    # Assertions against expected PUBLIC API
    assert len(engine.rooms) == 0, "New engine should start with no rooms."
    assert engine.start_room is None, "New engine should have no spawn room."
    assert engine.current_location is None, "New engine should have no location."

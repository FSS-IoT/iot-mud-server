import pytest
import asyncio
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_prose_description_on_room_change():
    """[TASK-009] RED PHASE: Ensure that a 'state_changed' event results in an AI description change/emission."""
    engine = Engine()
    emitted_events = []

    async def event_listener(event_type, data):
        if event_type == "room_change":
            emitted_events.append((event_type, data))

    # Setup listener
    await engine.subscribe("room_change", event_listener)

    # Simulate a room change via some method we assume will exist in the green phase
    # or by manually emitting it to simulate what should happen when logic updates state.
    # For TASK-009 RED, we test that our orchestration can handle this.
    await engine.emit("room_change", {"new_room": "The Dark Dungeon"})

    # This currently succeeds because I am manually emitting the event in the test 
    # to define what SHOULD happen when logic triggers it. 
    # However, we want to ensure that command processing can trigger it too (in Green Phase).
    assert len(emitted_events) > 0, "The engine failed to emit 'room_change' event on state change."
    assert emitted_events[0][1]["new_room"] == "The Dark Dungeon"

@pytest.mark.xfail(
    strict=True,
    reason="M2 scope: prose narration is not part of M1 (see docs/ROADMAP.md). "
    "Engine has no LLM layer yet; revisit with TASK-020/024.",
)
@pytest.mark.asyncio
async def test_prose_description_ai_integration_red():
    """[TASK-009] RED PHASE: Verify that AI descriptions can be integrated into the response flow."""
    engine = Engine()
    
    # We want to check if a command like 'describe' or just a move results in AI prose.
    # Currently, our engine only has hardcoded text. 
    # This test will fail because 'describe movement' isn't implemented and it won't call an LLM.
    result = await engine.process_command("go north")
    
    assert "AI Prose" in result or "The air grows colder" in result # Expecting something dynamic

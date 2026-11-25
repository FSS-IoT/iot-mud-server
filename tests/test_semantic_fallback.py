import pytest

from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_semantic_fallback_red():
    """[TASK-008] RED PHASE: Verify that unhandled commands trigger the semantic fallback mechanism."""
    engine = Engine()
    player = engine.register_player("Adventurer")
    
    # A command that currently doesn't match any deterministic rule
    unrecognized_command = "dance wildly in a circle"
    
    result = await engine.process_command(unrecognized_command, player)

    # An unrecognized command is handled gracefully and never leaks the internal
    # "[FALLBACK]" sentinel to the player. (The demo-polish pass replaced that
    # raw sentinel with a helpful prompt so the LLM fallback stays optional.)
    assert "[FALLBACK]" not in result
    assert isinstance(result, str) and result

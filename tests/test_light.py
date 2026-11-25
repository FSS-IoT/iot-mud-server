"""Red-green: `light <torch/lantern>` — igniting a carried light source.

TASK-039 closes the darkness loop opened by the light-source work: the demo
world has a dark Forest and Torch/Lantern items, but nothing ignites them. A
carried light source only sheds light once it is *lit*, so:

  * `light torch` ignites a carried Torch/Lantern, making a dark room visible.
  * An unlit Torch carried in a dark room does NOT light it — the light must be
    struck first.
  * `look` in a dark room before lighting stays dark.
  * An already-lit Torch reports that it is already lit.

The engine is exercised directly (no server) so these stay fast and
deterministic.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


def _dark_room():
    """A dark room with a player standing in it holding nothing."""
    engine = Engine()
    room = Room("Cave", "A pitch-black cavern.", dark=True)
    engine._rooms["Cave"] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    return engine, room, player


def _carry_lit_torch(engine, player):
    """Give the player a Torch and light it."""
    torch = Entity("Torch", "A flickering wooden torch.", light_source=True)
    torch.lit = True
    player.add_content(torch)
    return torch


def _carry_unlit_torch(engine, player):
    """Give the player an unlit Torch (default lit state)."""
    torch = Entity("Torch", "A flickering wooden torch.", light_source=True)
    player.add_content(torch)
    return torch


@pytest.mark.asyncio
async def test_light_torch_ignites_and_lights_dark_room():
    """`light torch` sets a carried Torch alight and a dark room becomes visible."""
    engine, _room, player = _dark_room()
    torch = _carry_unlit_torch(engine, player)

    response = await engine.process_command("light torch", "Ada")

    assert torch.lit is True
    assert "dark" not in response.lower()
    # The room is now lit, so a bare look reveals what was previously unseen.
    view = engine.visible_room_state(player)
    assert view["dark"] is False


@pytest.mark.asyncio
async def test_unlit_torch_does_not_light_room():
    """A carried Torch that has not been lit leaves a dark room dark."""
    engine, _room, player = _dark_room()
    _carry_unlit_torch(engine, player)

    response = await engine.process_command("look", "Ada")

    assert "dark" in response.lower()


@pytest.mark.asyncio
async def test_bare_look_stays_dark_before_lighting():
    """Before lighting, a bare `look` in a dark room reports darkness."""
    engine, _room, player = _dark_room()
    _carry_unlit_torch(engine, player)

    response = await engine.process_command("look", "Ada")

    assert "dark" in response.lower()
    assert "Here you see" not in response.lower()


@pytest.mark.asyncio
async def test_light_already_lit_torch_reports_it():
    """Lighting an already-lit Torch reports that it is already lit."""
    engine, _room, player = _dark_room()
    torch = _carry_lit_torch(engine, player)

    response = await engine.process_command("light torch", "Ada")

    assert torch.lit is True
    assert "already" in response.lower()


@pytest.mark.asyncio
async def test_light_without_carried_light_source_fails():
    """`light` with nothing ignitable to carry is refused, not a fallback."""
    engine, _room, _player = _dark_room()

    response = await engine.process_command("light torch", "Ada")

    assert "[FALLBACK]" not in response

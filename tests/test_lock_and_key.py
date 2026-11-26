"""Red-green: `use <key> on <lock>` — a key opens only the lock it matches.

Names locate the objects (forgiving substring match, room/inventory scoped),
but whether a lock opens is decided by identity, not name. A `locked` chest
names the `required_key` (an `item_id`); a key carries a `key_id` (also an
`item_id`). `use gold key on chest` resolves both by name, then compares ids:
only a matching id unlocks. This is what stops "every gold key opens every
chest" from being the whole game.

These tests expect `locked` / `required_key` on Entity and a `use` verb that do
not exist yet, so they fail now (RED).
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


def _setup():
    engine = Engine()
    room = Room("Forest", "Trees loom high.")
    engine.start_room = room
    player = engine.register_player("Ada")
    return engine, player, room


@pytest.mark.asyncio
async def test_use_key_on_unlocked_chest_matches():
    """A key with a matching id opens a locked chest."""
    engine, player, room = _setup()
    key = Entity("Gold Key", "A small gold key.", item_id="key_gold_1")
    chest = Entity("Chest", "A sturdy oak chest.", locked=True, required_key="key_gold_1")
    room.add_content(chest)
    player.add_content(key)

    response = await engine.process_command("use gold key on chest", "Ada")

    assert "unlock" in response.lower() or "open" in response.lower()
    assert chest.locked is False


@pytest.mark.asyncio
async def test_use_wrong_key_fails():
    """A key with a non-matching id does not open the chest."""
    engine, player, room = _setup()
    key = Entity("Gold Key", "A small gold key.", item_id="key_gold_2")
    chest = Entity("Chest", "A sturdy oak chest.", locked=True, required_key="key_gold_1")
    room.add_content(chest)
    player.add_content(key)

    response = await engine.process_command("use gold key on chest", "Ada")

    assert "unlock" not in response.lower()
    assert chest.locked is True


@pytest.mark.asyncio
async def test_use_key_without_chest_fails():
    """No chest in the room: the key has nothing to use on."""
    engine, player, _room = _setup()
    key = Entity("Gold Key", "A small gold key.", item_id="key_gold_1")
    player.add_content(key)

    response = await engine.process_command("use gold key on chest", "Ada")

    assert response is not None

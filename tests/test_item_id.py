"""Red-green: item_id — a stable handle for otherwise-identical objects.

Worlds will have duplicates: several chests, several "Gold Key"s. Matching by
display name is room-scoped and can't tell two copies in the same collection
apart, and a lock must be able to name *which* key opens it. The fix is a
stable identity that authors set in JSON and the engine reads only when it
matters (opening a lock, disambiguating two identical objects).

These tests expect an `item_id` attribute that does not exist yet, so they
fail now (RED).
"""

import pytest

from src.mud.content import Entity
from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village


def test_entity_item_id_defaults_to_none():
    """A plain entity has no item_id unless an author gives it one."""
    entity = Entity("Gold Coin", "A shiny, minted copper coin.")

    assert entity.item_id is None


def test_entity_carries_item_id():
    """An entity can be created with a stable `item_id` handle."""
    entity = Entity("Gold Key", "A small gold key.", item_id="key_gold_1")

    assert entity.item_id == "key_gold_1"


@pytest.mark.asyncio
async def test_loader_maps_json_id_to_item_id():
    """The loader moves each object's JSON `id` onto its `item_id`."""
    engine = Engine()
    await load_demo_village(engine)

    shop = engine.get_room("Alchemist Shop")
    torch = next(c for c in shop.contents if c.name == "Torch")

    assert torch.item_id == "torch"


@pytest.mark.asyncio
async def test_demo_world_seeds_lock_and_key():
    """The demo world carries a locked chest and a matching key."""
    engine = Engine()
    await load_demo_village(engine)

    shop = engine.get_room("Alchemist Shop")
    forest = engine.get_room("Mysterious Forest")

    key = next(c for c in shop.contents if c.name == "Gold Key")
    chest = next(c for c in forest.contents if c.name == "Chest")

    assert key.item_id == "key_gold_1"
    assert chest.locked is True
    assert chest.required_key == "key_gold_1"

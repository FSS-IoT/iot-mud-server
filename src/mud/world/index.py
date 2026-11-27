"""One loaded-world index and one identity rule (#139).

Identity is derived here once, from the loaded entities, instead of being
re-derived in the engine, the wanderer, the protocol adapter, and the server.
Rooms, objects, and NPCs resolve by their stable key; a display name is a
command convenience, not a primary key.

Duplicate display names are allowed: identity is the id, so two rooms called
"Hall" stay distinct. The name map keeps the first-loaded room as a documented,
deterministic tie-break; new code should use the id.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.mud.content import NPC, Entity, Player, Room
from src.mud.storage.keys import object_key, room_key
from src.mud.world.wander import npc_key


@dataclass(frozen=True)
class WorldIndex:
    """Every loaded entity, keyed by its stable id (and name where useful)."""

    rooms_by_id: dict[str, Room]
    rooms_by_name: dict[str, Room]
    objects_by_id: dict[str, Entity]
    npcs_by_id: dict[str, NPC]

    def room_by_id(self, room_id: str | None) -> Room | None:
        """The room stored under `room_id`, or None."""
        if room_id is None:
            return None
        return self.rooms_by_id.get(room_id)

    def room_by_name(self, name: str) -> Room | None:
        """The first-loaded room with this display name, or None."""
        return self.rooms_by_name.get(name)

    def object_by_id(self, object_id: str | None) -> Entity | None:
        """The object stored under `object_id`, or None."""
        if object_id is None:
            return None
        return self.objects_by_id.get(object_id)

    def npc_by_id(self, npc_id: str | None) -> NPC | None:
        """The NPC stored under `npc_id`, or None."""
        if npc_id is None:
            return None
        return self.npcs_by_id.get(npc_id)


def build_index(rooms: Iterable[Room]) -> WorldIndex:
    """Index every room and everything they hold, once, from the loaded world.

    A duplicate id keeps the first entity (the validator rejects duplicate
    authored ids); a duplicate display name keeps the first room for the name
    map while the id map holds every room.
    """
    rooms_by_id: dict[str, Room] = {}
    rooms_by_name: dict[str, Room] = {}
    objects_by_id: dict[str, Entity] = {}
    npcs_by_id: dict[str, NPC] = {}

    def walk(holder: Entity) -> None:
        for item in holder.contents:
            if isinstance(item, Player):
                continue
            objects_by_id.setdefault(object_key(item), item)
            walk(item)

    for room in rooms:
        rooms_by_id.setdefault(room_key(room), room)
        rooms_by_name.setdefault(room.name, room)
        walk(room)
        for npc in room.npcs:
            npcs_by_id.setdefault(npc_key(npc), npc)
    return WorldIndex(
        rooms_by_id=rooms_by_id,
        rooms_by_name=rooms_by_name,
        objects_by_id=objects_by_id,
        npcs_by_id=npcs_by_id,
    )

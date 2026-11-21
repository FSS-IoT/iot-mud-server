from __future__ import annotations
from typing import Dict, Optional, List

class Entity:
    """Base class for everything in the world: Players, Items, Rooms."""
    def __init__(self, name: str, description: str, weight: float = 0.0,
                 readable: bool = False, light_source: bool = False,
                 item_id: str | None = None, locked: bool = False,
                 required_key: str | None = None):
        self.name = name
        self.description = description
        self.weight = weight
        # A stable handle set by the world author (the object's JSON `id`). Used
        # only when name-matching is not enough: opening a lock that names a
        # specific key, or telling two otherwise-identical objects apart in one
        # collection. None means "no handle", so plain demo items stay plain.
        self.item_id = item_id
        # `locked` marks a container a player cannot open until the right key is
        # used on it. `required_key` names the `item_id` of the one key that
        # opens it — so copies of the same-named key are still distinct at the
        # moment of use. A locked chest with no `required_key` is a bug, but it
        # stays locked rather than crashing.
        self.locked = locked
        self.required_key = required_key
        # The items a container holds when closed (its loot). `add_content`
        # populates this; `open <chest>` spills it into the room so plain
        # `take` can then pick it up. Empty by default, so plain items stay plain.
        # `readable` marks objects a player can `read` (signs, books, scrolls).
        # Plain items are not; `read <item>` is refused for them.
        self.readable = readable
        # `light_source` marks an object that sheds light — a Torch, a lantern.
        # Carried by a player it lights wherever they stand; it makes a dark
        # room visible.
        self.light_source = light_source
        self.contents: List[Entity] = []

    def add_content(self, item: Entity) -> bool:
        self.contents.append(item)
        return True

    def remove_content(self, item: Entity) -> bool:
        if item in self.contents:
            self.contents.remove(item)
            return True
        return False

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.name}>"

    def __str__(self) -> str:
        return self.name

class Player(Entity):
    """A named player in the world. Owns its current location."""

    def __init__(self, name: str, description: str = "A player."):
        super().__init__(name=name, description=description)
        self.location: Optional[Room] = None


class Room(Entity):
    """A location where entities can reside."""
    def __init__(self, name: str, description: str, exits: Optional[Dict[str, Room]] = None,
                 light_source: Entity | None = None, dark: bool = False):
        super().__init__(name, description)
        self.exits = exits or {}
        # A fixed light in the room (a wall lantern, a brazier). None means the
        # room has no built-in light. Kept as an Entity so it can itself be
        # carried (a portable light) or inspected.
        self.light_source = light_source
        # `dark` marks a room that is unlit unless a light source is present
        # (in the room or carried by the observer). The default is a LIT room,
        # so the demo world stays lit until a room opts into darkness.
        self.dark = dark

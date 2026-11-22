from __future__ import annotations
from typing import Dict, Optional, List

class Entity:
    """Base class for everything in the world: Players, Items, Rooms."""
    def __init__(self, name: str, description: str, weight: float = 0.0,
                 readable: bool = False, light_source: bool = False,
                 item_id: str | None = None, locked: bool = False,
                 required_key: str | None = None, lit: bool = False,
                 dropped_on_sleep: bool = False, portable: bool = True,
                 container: bool = False, open: bool = False):
        self.name = name
        self.description = description
        self.weight = weight
        # A stable handle set by the world author (the object's JSON `id`). Used
        # only when name-matching is not enough: opening a lock that names a
        # specific key, or telling two otherwise-identical objects apart in one
        # collection. None means "no handle", so plain demo items stay plain.
        self.item_id = item_id
        # A stable per-instance handle assigned on first sighting (or by the
        # loader). Unlike `item_id`, it does not change when the object moves
        # between a room and an inventory, so a client can follow one coin.
        self.instance_id: str | None = None
        # `locked` marks a container a player cannot open until the right key is
        # used on it. `required_key` names the `item_id` of the one key that
        # opens it — so copies of the same-named key are still distinct at the
        # moment of use. A locked chest with no `required_key` is a bug, but it
        # stays locked rather than crashing.
        self.locked = locked
        self.required_key = required_key
        # The items a container holds when closed (its loot). `add_content`
        # populates this; `open <chest>` lists it and `get <item> from <chest>`
        # pulls one out, so the contents stay in the container (#76). Empty by
        # default, so plain items stay plain.
        # `readable` marks objects a player can `read` (signs, books, scrolls).
        # Plain items are not; `read <item>` is refused for them.
        self.readable = readable
        # `light_source` marks an object that sheds light — a Torch, a lantern.
        # Carried by a player it lights wherever they stand; it makes a dark
        # room visible.
        self.light_source = light_source
        # `lit` is the ignition state of a `light_source` (Torch, lantern). An
        # unlit light source sheds no light; `light <item>` sets it. Carried by
        # a player it lights wherever they stand *only while lit*, so the player
        # must strike it before a dark room becomes visible.
        self.lit = lit
        # An item that leaves the character when it sleeps and turns to stone
        # (#72). Authored in YAML/JSON as `dropped_on_sleep: true` and
        # persisted in `objects.state`; the gold chest key uses it. Everything
        # else stays in the sleeping character's inventory.
        self.dropped_on_sleep = dropped_on_sleep
        # `portable` is authored per object. `get <item> from <container>`
        # refuses to lift a fixed object into inventory (#76); `take` keeps its
        # existing v2.0 behavior.
        self.portable = portable
        # `container` marks a world object authored as `kind: container`.
        # `open` is the explicit lid state (#76): `open`, `close`, and
        # `get ... from ...` read it. A container starts closed unless the
        # world author sets `state.open: true`.
        self.container = container
        self.open = open
        self.contents: List[Entity] = []

    @property
    def is_container(self) -> bool:
        """Whether this entity can hold contents a player reaches into.

        Authored containers say so directly. Older engine callers build a
        chest by adding contents without the flag, and a locked object is a
        container by the content vocabulary, so both still count. Players are
        never containers: callers exclude them by class before asking.
        """
        return self.container or self.locked or self.open or bool(self.contents)

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

class ExitLock:
    """A locked room exit and the one key object that opens it.

    `key` is the `item_id` of the key object, exactly as a container's
    `required_key`. `label` is the author's name for the door ("iron door"),
    used when a player names the lock instead of a direction.

    Both directions of a two-way door share one `ExitLock` instance: turning
    the key unlocks the door itself, so the reciprocal side comes along without
    a second use. A one-way lock has only the side it declares.
    """

    def __init__(self, key: str | None, label: str | None = None):
        self.key = key
        self.label = label
        self.locked = True

    def __repr__(self) -> str:
        state = "locked" if self.locked else "unlocked"
        return f"<ExitLock: key={self.key!r} {state}>"


class Player(Entity):
    """A named player in the world. Owns its current location."""

    def __init__(self, name: str, description: str = "A player."):
        super().__init__(name=name, description=description)
        self.location: Optional[Room] = None
        # Sleep is the stone state (#72): the character stays in its room while
        # its session is gone. It is present, invincible, and not a valid
        # combat target; login clears the flag. The durable copy lives in
        # `players.asleep`, and the wire exposes it as `asleep`/`stone`.
        self.asleep = False


class Room(Entity):
    """A location where entities can reside."""
    def __init__(self, name: str, description: str, exits: Optional[Dict[str, Room]] = None,
                 light_source: Entity | None = None, dark: bool = False):
        super().__init__(name, description)
        self.exits = exits or {}
        # Locks sit beside exits, keyed by the same direction. A direction with
        # no lock entry is an open way. The `ExitLock` remembers whether the
        # door is still locked; the reciprocal direction shares the instance.
        self.exit_locks: Dict[str, ExitLock] = {}
        # A fixed light in the room (a wall lantern, a brazier). None means the
        # room has no built-in light. Kept as an Entity so it can itself be
        # carried (a portable light) or inspected.
        self.light_source = light_source
        # `dark` marks a room that is unlit unless a light source is present
        # (in the room or carried by the observer). The default is a LIT room,
        # so the demo world stays lit until a room opts into darkness.
        self.dark = dark
        # Optional authoring data for map-rendering clients. The loader
        # validates it and stores the normalized wire shape; None means the
        # room has no map, and the protocol omits the field entirely.
        self.geometry: dict | None = None

    def exit_is_locked(self, direction: str) -> bool:
        """Whether this room's `direction` exit is currently locked."""
        lock = self.exit_locks.get(direction)
        return bool(lock is not None and lock.locked)

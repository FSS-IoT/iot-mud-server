from __future__ import annotations
from typing import Dict, Optional, List

# How much authored `weight` a character may carry by default (#78, #148). The
# world authors item weight in the content model; capacity is the same unit, so
# a character's `used` is the sum of the `weight` of everything in `contents`.
# A non-positive capacity means no limit (in-memory test entities can opt out).
DEFAULT_CARRY_CAPACITY = 30.0

class Entity:
    """Base class for everything in the world: Players, Items, Rooms."""
    def __init__(self, name: str, description: str, weight: float = 0.0,
                 readable: bool = False, light_source: bool = False,
                 item_id: str | None = None, locked: bool = False,
                 required_key: str | None = None, lit: bool = False,
                 dropped_on_sleep: bool = False, portable: bool = True,
                 container: bool = False, open: bool = False,
                 equip_slots: tuple[str, ...] = (), hands: int = 1,
                 ignition: bool = False, fuel: bool = False,
                 fire_bonus: float = 0.0):
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
        # Where the object can be equipped. `hand` uses one hand, or both when
        # `hands` is 2. Empty means it cannot be equipped. The live slot is
        # `equipped_slot` (`hand_1`, `belt`, ...), set by the engine and
        # persisted in `objects.state.slot` (#181).
        self.equip_slots = tuple(equip_slots)
        self.hands = hands
        self.equipped_slot: str | None = None
        # Fire-making capabilities (#183). `ignition` marks a striker, bow
        # drill, or matches; `fuel` marks kindling or dry wood. `fire_bonus`
        # is added to the base chance, 0.0 to 1.0.
        self.ignition = ignition
        self.fuel = fuel
        self.fire_bonus = fire_bonus
        # `container` marks a world object authored as `kind: container`.
        # `open` is the explicit lid state (#76): `open`, `close`, and
        # `get ... from ...` read it. A container starts closed unless the
        # world author sets `state.open: true`.
        self.container = container
        self.open = open
        self.contents: List[Entity] = []
        # Authored effects, keyed by trigger (`on_enter`, `on_talk`, ...).
        # The loader copies the validated content model here; the engine is the
        # only reader and publishes an `effect` event per triggered entry
        # (#84). Empty means the object, NPC, or room reacts to nothing.
        self.effects: dict = {}

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
    """A named player in the world. Owns its current location.

    `capacity` is the carry limit in the same unit as `Entity.weight` (#78).
    `used` is not stored: the engine sums the weights of `contents`, so memory
    and the `player.inventory` wire fields can never disagree. A non-positive
    capacity means no limit.
    """

    def __init__(self, name: str, description: str = "A player.",
                 capacity: float = DEFAULT_CARRY_CAPACITY):
        super().__init__(name=name, description=description)
        self.capacity = capacity
        self.location: Optional[Room] = None
        # Sleep is the stone state (#72): the character stays in its room while
        # its session is gone. It is present, invincible, and not a valid
        # combat target; login clears the flag. The durable copy lives in
        # `players.asleep`, and the wire exposes it as `asleep`/`stone`.
        self.asleep = False
        # Equipped items by runtime slot: hand_1, hand_2, head, body, back,
        # belt, feet. The item also carries `equipped_slot`, so login can
        # rebuild this map from durable state (#181).
        self.equipment: dict[str, Entity] = {}
        # Per-character skills, restored from `players.skills` on login
        # (#183). Missing means level 0.
        self.skills: dict[str, int] = {}

    def equip(self, item: Entity, slot: str) -> None:
        """Equip `item` in `slot`, replacing its previous slot."""
        self.unequip(item)
        if slot in self.equipment:
            raise ValueError(f"slot {slot!r} is occupied")
        self.equipment[slot] = item
        item.equipped_slot = slot

    def unequip(self, item: Entity) -> None:
        """Remove `item` from whatever slot holds it."""
        for slot, held in list(self.equipment.items()):
            if held is item:
                del self.equipment[slot]
                item.equipped_slot = None

    @property
    def equipped_items(self) -> tuple[Entity, ...]:
        """The equipped items, in slot order."""
        return tuple(self.equipment[slot] for slot in sorted(self.equipment))


class NPC(Entity):
    """A scripted character in a room (#81).

    Dialogue is authored in the YAML world and answers `talk` and
    `ask <npc> about <topic>`; there is no LLM behind it. An NPC is *not* a
    room object: it lives in `Room.npcs`, never in `Room.contents`, so the
    item vocabulary (`take`, `drop`, carry weight) never sees it.

    `topics` is keyed by the lowercased topic a player types. `greeting` is
    what `talk` says first, and `default` is the fallback for an unknown topic
    (or for `talk` when no greeting is authored). `wander` is authored here
    for #82; this class does not move on its own. `effects` is kept for the
    #84 trigger seam.
    """

    def __init__(self, name: str, description: str = "",
                 greeting: str = "", default: str = "",
                 topics: dict[str, str] | None = None,
                 item_id: str | None = None,
                 wander: bool = False, wander_every: int = 15,
                 effects: dict | None = None):
        super().__init__(name=name, description=description, item_id=item_id)
        self.greeting = greeting
        self.default = default
        self.topics: dict[str, str] = dict(topics or {})
        self.wander = wander
        self.wander_every = wander_every
        self.effects = dict(effects or {})


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
        # Scripted characters that live here (#81). NPCs are kept out of
        # `contents` so they never enter the item vocabulary: `take`, `drop`,
        # capacity, and container contents are unaffected. A wandering NPC
        # (#82) moves between rooms' `npcs` lists, not through contents.
        self.npcs: list[NPC] = []

    def exit_is_locked(self, direction: str) -> bool:
        """Whether this room's `direction` exit is currently locked."""
        lock = self.exit_locks.get(direction)
        return bool(lock is not None and lock.locked)

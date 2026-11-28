from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional
from src.mud.content import DEFAULT_CARRY_CAPACITY, NPC, Entity, Player, Room
from src.mud.content.effects import effect_entries, effect_payload
from src.mud.content.models import EQUIP_SLOTS, OPPOSITE_EXITS
from src.mud.content.validator import container_cycles
from src.mud.protocol.v2 import CommandOutcome, SemanticEvent
from src.mud.storage.keys import object_key, room_key, slug
from src.mud.storage.store import ChangeSet, StorageWriteError
from src.mud.world.index import WorldIndex
from src.mud.world.wander import NpcMove, NpcWanderer, npc_key

if TYPE_CHECKING:
    from src.mud.storage.store import WriteThroughStore

logger = logging.getLogger(__name__)

# Slots whose light reaches the room. A lit source in a pack, a back slot, or
# a container does not light the room (#181).
LIGHT_SLOTS = frozenset({"hand_1", "hand_2", "belt", "head"})
HAND_SLOTS = ("hand_1", "hand_2")


@dataclass(frozen=True)
class SceneExit:
    """One way out of a room, as the scene renders it (#135)."""

    direction: str
    to_id: str
    locked: bool
    label: str | None = None


@dataclass(frozen=True)
class Scene:
    """What one actor can see, built once by `Engine.scene_for` (#135).

    `objects`, `occupants`, `npcs`, and `exits` are the *visible* sets: they
    are empty when `dark`, and `geometry` is None so a map is never leaked to
    an observer who cannot see the room. `inventory` is always the actor's
    carried set, because carrying does not depend on sight.

    The value is deliberately domain-level (entities, not wire dicts): the
    prose renderer in this module and the v2 adapter shape it independently.
    """

    room: Room | None
    room_id: str | None
    name: str
    description: str
    dark: bool
    objects: tuple[Entity, ...] = ()
    occupants: tuple[Player, ...] = ()
    npcs: tuple[NPC, ...] = ()
    exits: tuple[SceneExit, ...] = ()
    geometry: dict[str, Any] | None = None
    inventory: tuple[Entity, ...] = ()


class Engine:
    def __init__(self, store: WriteThroughStore | None = None):
        """The authoritative brain of the MUD.

        `store` is the write-through seam (SPEC v2.1.0): a take, drop, or move
        commits there before the engine touches memory, so a failed write
        raises `StorageWriteError` with memory untouched. None keeps the engine
        purely in memory for the v1 server and the unit tests.
        """
        self._rooms: Dict[str, Room] = {}
        # The one loaded-world index (#139): rooms/objects/NPCs by stable id.
        # The loader builds it; in-memory engines leave it None and fall back
        # to `_rooms`.
        self.index: WorldIndex | None = None
        # The room a freshly registered player spawns in, set by the world
        # loader. There is no legacy single-player slot (#133).
        self.start_room: Optional[Room] = None
        # The effect log sink is a bus subscriber (#84): every triggered
        # effect is appended to `event_log` when a store is attached, whether
        # or not any client is allowed to see it.
        self._subscribers: Dict[str, List[Callable]] = {
            "effect": [self._record_effect],
        }
        # Multiplayer registry. Each named player owns its location; the engine
        # is authoritative over who is where.
        self._players: Dict[str, Player] = {}
        self.store: WriteThroughStore | None = store
        # Admission owns the durable roster when attached (#138); a bare
        # engine falls back to the store's roster for unit tests.
        self.roster_provider: Callable[[], Iterable[Any]] | None = None
        # Deterministic world motion (#82). The loader sets `rng_seed` from the
        # world file and builds the wanderer once every room and NPC exists;
        # `tick_world` advances it. `rng_seed` defaults to 0 for the legacy
        # JSON world and in-memory tests, which still get a reproducible path.
        self.rng_seed: int = 0
        self.wanderer: NpcWanderer | None = None

    def attach_store(self, store: WriteThroughStore | None) -> None:
        """Persist each mutation to `store` before applying it in memory."""
        self.store = store

    def _commit(self, changes: ChangeSet) -> None:
        """Commit one command's changes before memory moves (#131)."""
        if self.store is not None and changes:
            self.store.commit(changes)

    @property
    def rooms(self) -> Dict[str, Room]:
        """Public access to the world map."""
        return self._rooms

    @property
    def current_location(self) -> Optional[Room]:
        """The spawn room a newly registered player starts in."""
        return self.start_room

    def get_room(self, name: str) -> Optional[Room]:
        """Return the room with this display name, or None."""
        return self._rooms.get(name)

    def get_room_by_id(self, room_id: str | None) -> Optional[Room]:
        """Return the room stored under its stable id, or None.

        The loaded world's `WorldIndex` makes this a direct lookup. An
        in-memory engine that sets `_rooms` directly falls back to a scan.
        """
        if room_id is None:
            return None
        if self.index is not None:
            room = self.index.rooms_by_id.get(room_id)
            if room is not None:
                return room
        return next(
            (room for room in self._rooms.values()
             if room_key(room) == room_id),
            None,
        )

    # --- NPC wandering (#82) -------------------------------------------------

    async def tick_world(self) -> list[NpcMove]:
        """Advance NPC wandering by one 1 Hz world tick.

        The engine owns the seed and the schedule; the server only decides
        when a second has passed. A world loaded before this method is first
        called gets its wanderer here, so a caller that never runs a clock
        (the unit tests) can still drive the same path explicitly. Each move
        emits `npc_moved` with the NPC's stable id and both room ids, which
        the network layer turns into room-local `npc.leave`/`npc.enter`.
        """
        if self.wanderer is None:
            rooms = (self.index.rooms_by_id if self.index is not None
                     else self._rooms)
            self.wanderer = NpcWanderer(rooms, self.rng_seed)
        moves = self.wanderer.step()
        for move in moves:
            await self.emit("npc_moved", move.event_data())
        return moves

    # --- Player registry -----------------------------------------------------

    def register_player(self, name: str) -> Player:
        """Register a new player and spawn them at the loaded start room.

        Raises ValueError if a player of that name already exists.
        """
        if name in self._players:
            raise ValueError(f"Player '{name}' is already registered")
        player = Player(name)
        self._players[name] = player
        player.location = self.start_room
        if player.location is not None:
            player.location.add_content(player)
        return player

    def get_player(self, name: str) -> Optional[Player]:
        """Return the player with this name, or None."""
        return self._players.get(name)

    def find_character(self, name: str) -> Player | None:
        """Return the live player named `name`, ignoring case.

        Commands type names, not registry keys: `tell ben ...` must reach the
        character registered as `Ben`. The registry itself stays keyed by the
        canonical name so `_ws_by_name` can route a private event.
        """
        key = name.strip().lower()
        return next(
            (player for player in self._players.values()
             if player.name.lower() == key),
            None,
        )

    def character_entry(self, name: str) -> dict[str, Any] | None:
        """The `who` roster entry for `name`, ignoring case (#75).

        `tell` uses this to tell an unknown name (`not_found`) apart from a
        character that exists but is asleep/stone (`not_awake`).
        """
        key = name.strip().lower()
        return next(
            (entry for entry in self.who_entries()
             if entry["name"].lower() == key),
            None,
        )

    def remove_player(self, name: str) -> bool:
        """Remove a player from the registry and clear their location.

        Returns True if a player was removed, False if no such player existed.
        """
        player = self._players.pop(name, None)
        if player is None:
            return False
        if player.location is not None:
            player.location.remove_content(player)
        player.location = None
        return True

    def players_in_room(self, room: Room) -> List[Player]:
        """Return the players currently in this room."""
        if room is None:
            return []
        return [p for p in self._players.values() if p.location is room]

    def _object_slots(self) -> dict[str, tuple[Entity, Entity]]:
        """Every loaded object keyed by durable id, with the entity holding it.

        The store records object ids, not object identities, so login has to
        find the entity the freshly loaded world built for that id. Rooms and
        containers are both `Entity.contents` holders; a player is never walked
        into, so another character's inventory cannot be mistaken for the
        world's.
        """
        slots: dict[str, tuple[Entity, Entity]] = {}

        def walk(holder: Entity) -> None:
            for item in holder.contents:
                if isinstance(item, Player):
                    continue
                slots.setdefault(object_key(item), (item, holder))
                walk(item)

        rooms = (self.index.rooms_by_id.values()
                 if self.index is not None else self._rooms.values())
        for room in rooms:
            walk(room)
        return slots

    def restore_player(
        self,
        player: Player,
        *,
        room: Room | None,
        item_keys: Iterable[str],
    ) -> None:
        """Put a freshly registered player back where the store left it.

        `room` is the durable room, or None when the loaded world no longer has
        it; the character then stays at the start room. Every key in
        `item_keys` leaves the room or container the loader authored it into
        and joins the player's inventory. A key with no loaded object is
        ignored: a world reset clears inventory, and a missing object must not
        fail a login.
        """
        if room is not None and player.location is not room:
            if player.location is not None:
                player.location.remove_content(player)
            player.location = room
            room.add_content(player)

        slots = self._object_slots()
        for key in item_keys:
            slot = slots.get(key)
            if slot is None:
                continue
            item, holder = slot
            if item is player or holder is player:
                continue
            holder.remove_content(item)
            player.add_content(item)
            if getattr(item, "equipped_slot", None):
                try:
                    player.equip(item, item.equipped_slot)
                except ValueError:
                    slot = item.equipped_slot
                    self._commit(
                        ChangeSet("restore").set(item, "slot", None))
                    item.equipped_slot = None
                    logger.warning(
                        "could not restore %s to slot %s for %s",
                        object_key(item), slot, player.name)

    # --- Sleep, stone, and the who roster (#72) -----------------------------

    def sleep_player(self, name: str) -> list[Entity]:
        """Put an authenticated character to sleep in place (#72).

        The character stays in its room and turns to stone. Items marked
        `dropped_on_sleep` (the gold chest key) drop into that room so another
        player can pick them up; the rest of the inventory stays and wakes with
        the character. Each drop commits through the store before memory moves,
        and the sleep flag commits last. Returns the dropped objects.
        """
        player = self.get_player(name)
        if player is None:
            return []
        room = player.location
        dropped: list[Entity] = []
        if room is not None:
            dropped = [
                item for item in player.contents
                if getattr(item, "dropped_on_sleep", False)
            ]
        if self.store is not None:
            changes = ChangeSet("sleep")
            if room is not None:
                changes.place(player, at=room)
                for item in dropped:
                    changes.place(item, at=room)
                    changes.set(item, "slot", None)
            changes.set(player, "asleep", True)
            self._commit(changes)
        for item in dropped:
            player.unequip(item)
            player.remove_content(item)
            room.add_content(item)
        player.asleep = True
        return dropped

    def wake_player(self, name: str) -> Player | None:
        """Wake a sleeping character in place (#72).

        The durable flag clears before memory, matching the write-through rule.
        A missing player returns None; login registers a fresh one first.
        """
        player = self.get_player(name)
        if player is None:
            return None
        if self.store is not None:
            self._commit(
                ChangeSet("wake").set(player, "asleep", False))
        player.asleep = False
        return player

    def who_entries(self) -> list[dict[str, Any]]:
        """Every character server-wide, awake or asleep/stone (#72).

        The durable roster is the base, so a character whose session ended or
        whose server restarted still appears asleep in its last room. Live
        engine players overlay their current room and true sleep flag; a guest
        with no row is added from memory. Sorted by name for deterministic
        wire output.
        """
        live = {name.lower(): player for name, player in self._players.items()}
        entries: dict[str, dict[str, Any]] = {}
        if self.roster_provider is not None:
            durable = self.roster_provider()
        elif self.store is not None:
            durable = self.store.roster()
        else:
            durable = ()
        for row in durable:
            asleep = bool(row.asleep) or row.name.lower() not in live
            entries[row.name.lower()] = {
                "name": row.name,
                "room_id": row.room_id,
                "asleep": asleep,
                "stone": asleep,
            }
        for key, player in live.items():
            asleep = bool(player.asleep)
            entries[key] = {
                "name": player.name,
                "room_id": (
                    room_key(player.location)
                    if player.location is not None else None
                ),
                "asleep": asleep,
                "stone": asleep,
            }
        return sorted(
            entries.values(), key=lambda entry: entry["name"].lower())

    async def move_player(self, name: str, direction: str) -> tuple[bool, str]:
        """Move a *registered* player one step in `direction`, if an exit allows.

        Returns a `(moved, result)` tuple: on success `moved` is True and `result`
        is the destination room name; on failure `moved` is False and `result` is
        the refusal string (the player stays put). Emits a semantic `player_moved`
        event carrying name / from / to so the network layer can broadcast position
        changes to the affected rooms.
        """
        player = self.get_player(name)
        if player is None:
            return False, ""
        return await self._move_actor(player, direction)

    async def _move_actor(self, player: Player,
                          direction: str) -> tuple[bool, str]:
        """Move one actor one step; the caller supplies the actor (#133).

        Unlike `move_player` this does not require registration, so a v1
        connection's pre-join solo actor can still walk.
        """
        if player.location is None:
            return False, ""

        exit_name = direction.strip().lower()
        destination = player.location.exits.get(exit_name)
        if not isinstance(destination, Room):
            return False, f"You cannot go {exit_name}."
        if player.location.exit_is_locked(exit_name):
            await self.emit("exit_locked", {
                "direction": exit_name,
                "location": player.location.name,
            })
            return False, self._locked_exit_reply(exit_name)

        # Commit first, then apply, then publish (SPEC v2.1.0 criterion 2).
        if self.store is not None:
            self._commit(
                ChangeSet("move").place(player, at=destination))

        old_name = player.location.name
        player.location.remove_content(player)
        player.location = destination
        destination.add_content(player)

        await self.emit("player_moved", {
            "name": player.name, "from": old_name, "to": destination.name,
        })
        await self.fire_effects(
            destination, "on_enter", actor=player.name, room=destination)
        return True, destination.name

    @staticmethod
    def _locked_exit_reply(direction: str) -> str:
        """The refusal for a locked way, phrased so the client can act on it."""
        return (
            f"The way {direction} is locked. Find a key and "
            f"`use <key> on {direction}`."
        )

    @staticmethod
    def _exit_labels(room: Room) -> list[str]:
        """Exit names for prose, marking the locked ones (SPEC v2.1.2)."""
        return [
            (
                f"{direction.capitalize()} (locked)"
                if room.exit_is_locked(direction)
                else direction.capitalize()
            )
            for direction in room.exits
        ]

    @staticmethod
    def _describe_movement(scene: Scene, direction: str, old_location=None) -> str:
        """Render the standard movement prose from the destination scene (#135).

        The reply is the single source of truth for what the mover sees on arrival:
        travel prose, destination description, exits, and any visible items. A
        separate `room_state` push on move is intentionally NOT used here because
        it would duplicate this reply (one with contents, one without).
        """
        old_name = old_location.name if old_location is not None else "the previous room"
        response = (
            f"You travel towards {scene.name}.\n"
            f"(Prose: You leave the {old_name}).\n\n"
        )
        if scene.dark:
            return response + "It is too dark to see anything."

        exits_list = [
            (
                f"{exit_.direction.capitalize()} (locked)"
                if exit_.locked
                else exit_.direction.capitalize()
            )
            for exit_ in scene.exits
        ]
        exit_str = f"Exits lead {', '.join(exits_list)}."
        response += f"{scene.description}\n{exit_str}"

        if scene.objects:
            listed = ", ".join(
                sorted(obj.name for obj in scene.objects))
            response += f"\n\nHere you see: {listed}."

        return response

    @staticmethod
    def _split_target(arg: str) -> tuple[str, int | None]:
        """Split a target into its lowercase base name and optional index.

        A trailing integer is the 1-based `<name> <n>` disambiguation index, not
        part of the name. Leading filler ("the", "a", "an") is stripped so
        natural phrasings like "the gold coin" resolve to "gold coin".
        """
        tokens = arg.strip().split()
        index = None
        if tokens and tokens[-1].isdigit():
            index = int(tokens[-1])
            tokens = tokens[:-1]
        key = re.sub(
            r"^(the|a|an)\s+", "", " ".join(tokens).strip().lower())
        return key, index

    @classmethod
    def target_matches(cls, collection, arg: str) -> list[Entity]:
        """Every object `arg` names, in deterministic resolution order.

        Resolution is tiered so a natural name is never made ambiguous by a
        longer name that merely contains it:

        1. an exact display name (case-insensitive);
        2. otherwise, a case-insensitive prefix of a display name;
        3. otherwise, the v2.0 substring match, kept for compatibility
           (`coin` still finds `Gold Coin`).

        Players are never objects even though they live in `room.contents`. A
        bare index (`take 1`) has no name and matches nothing. The list is
        sorted by display name (then stable collection order), which is the
        order the `<name> <n>` index addresses. The protocol adapter uses this
        same list to fill `error{candidates}`.
        """
        key, _index = cls._split_target(arg)
        if not key:
            return []
        candidates = [c for c in collection if not isinstance(c, Player)]
        exact = [c for c in candidates if c.name.lower() == key]
        if exact:
            matches = exact
        else:
            prefixed = [
                c for c in candidates if c.name.lower().startswith(key)
            ]
            matches = prefixed or [
                c for c in candidates if key in c.name.lower()
            ]
        return sorted(matches, key=lambda c: c.name.lower())

    @classmethod
    def _find_item(cls, collection, arg: str):
        """Find the unique item `arg` names, using `target_matches` rules."""
        matches = cls.target_matches(collection, arg)
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _disambiguate(cls, collection, arg: str):
        """Resolve `arg` against `collection`, supporting `<name> <n>` disambiguation.

        When a bare name matches several items (e.g. two "keys"), the player can
        pick one by 1-based index among the sorted matches: `take key 1`. The
        index is 1-based and refers to the sorted list of matches, so
        `key 1` is the alphabetically first. A bare name with a single match
        resolves directly; a bare name with several matches without an index
        returns an ambiguity message; a missing or out-of-range index returns
        None (callers fall through to their usual refusal).

        Returns a tuple `(item, message)`: exactly one is non-None.
        """
        key, index = cls._split_target(arg)
        # A missing name (e.g. `take 1`) must not match every item.
        if not key:
            return None, None
        matches = cls.target_matches(collection, arg)
        if not matches:
            return None, None
        # A trailing index is authoritative even for a single match, so an
        # out-of-range `key 3` still fails instead of grabbing the only key.
        if index is not None:
            if 1 <= index <= len(matches):
                return matches[index - 1], None
            return None, None
        if len(matches) == 1:
            return matches[0], None
        message = (
            f"There are several {key} here: "
            f"{', '.join(f'{i} {c.name}' for i, c in enumerate(matches, 1))}. "
            f"Specify which, e.g. `{key} 1`."
        )
        return None, message

    # --- NPC dialogue (#81) --------------------------------------------------

    @staticmethod
    def npc_matches(room, arg: str) -> list[NPC]:
        """Every NPC in `room` that `arg` names, in deterministic order.

        The same tiered resolution as `target_matches`: exact display name,
        then a case-insensitive prefix, then the v2.0 substring match, sorted
        by display name. NPCs are resolved separately from objects precisely
        because they do not live in `room.contents`.
        """
        key, _index = Engine._split_target(arg)
        if room is None or not key:
            return []
        exact = [npc for npc in room.npcs if npc.name.lower() == key]
        if exact:
            matches = exact
        else:
            prefixed = [
                npc for npc in room.npcs if npc.name.lower().startswith(key)
            ]
            matches = prefixed or [
                npc for npc in room.npcs if key in npc.name.lower()
            ]
        return sorted(matches, key=lambda npc: npc.name.lower())

    @classmethod
    def find_npc(cls, room, arg: str) -> tuple[NPC | None, str | None]:
        """Resolve `arg` to one NPC in `room`, with `<name> <n>` support.

        Returns `(npc, message)`: exactly one is non-None. A miss is
        `(None, None)` and lets the caller phrase "you see no ...", which the
        adapter maps to `error{not_found}`.
        """
        key, index = cls._split_target(arg)
        if not key:
            return None, None
        matches = cls.npc_matches(room, arg)
        if not matches:
            return None, None
        if index is not None:
            if 1 <= index <= len(matches):
                return matches[index - 1], None
            return None, None
        if len(matches) == 1:
            return matches[0], None
        message = (
            f"There are several {key} here: "
            f"{', '.join(f'{i} {n.name}' for i, n in enumerate(matches, 1))}. "
            f"Specify which, e.g. `{key} 1`."
        )
        return None, message


    # --- Carry limit (#78) ---------------------------------------------------

    @staticmethod
    def carry_capacity(player) -> float:
        """The actor's carry limit in `Entity.weight` units.

        A legacy `Entity` used as the single-player slot has no capacity
        attribute and keeps the documented default. A non-positive capacity
        means no limit, so test doubles and old callers do not have to opt in.
        """
        return float(
            getattr(player, "capacity", DEFAULT_CARRY_CAPACITY) or 0.0)

    @staticmethod
    def carry_used(player) -> float:
        """The sum of the weights the actor is carrying right now."""
        if player is None:
            return 0.0
        return sum(
            float(getattr(item, "weight", 0.0) or 0.0)
            for item in player.contents
        )

    def can_carry(self, player, item) -> bool:
        """Whether `item` fits in `player`'s pack without changing state."""
        capacity = self.carry_capacity(player)
        if capacity <= 0:
            return True
        weight = float(getattr(item, "weight", 0.0) or 0.0)
        # A tiny epsilon keeps exact-boundary float sums (0.1 + 0.2) working.
        return self.carry_used(player) + weight <= capacity + 1e-9

    def _room_is_lit(self, player, room) -> bool:
        """Whether `room` is lit from the perspective of `player`.

        A room is lit unless it is explicitly marked `dark`. A dark room is lit
        if it carries a light source, or if the observer carries a *lit* light
        source (a struck Torch, a lit lantern). The default is a LIT room: no
        explicit darkness means the lights are effectively on.
        """
        if room is None or (isinstance(room, Room) and not room.dark):
            return True
        if isinstance(room, Room) and room.light_source is not None:
            return True
        if not isinstance(room, Room):
            return False
        occupants = [c for c in room.contents if isinstance(c, Player)]
        if player is not None and player not in occupants:
            occupants.append(player)
        if any(
            getattr(item, "light_source", False)
            and getattr(item, "lit", False)
            for item in room.contents
            if not isinstance(item, Player)
        ):
            return True
        return any(
            slot in LIGHT_SLOTS
            and getattr(item, "light_source", False)
            and getattr(item, "lit", False)
            for occupant in occupants
            for slot, item in occupant.equipment.items()
        )

    def scene_for(self, player) -> Scene:
        """Build the one visible scene both renderers consume (#135).

        This is the single visibility rule: a dark room with no light exposes
        no objects, occupants, NPCs, exits, or geometry; a carried lit light or
        a fixed room light keeps it lit. The actor's inventory is always
        present, because carrying does not depend on sight.
        """
        room = player.location if player is not None else None
        inventory = tuple(player.contents) if player is not None else ()
        if not isinstance(room, Room):
            return Scene(
                room=None, room_id=None, name="", description="",
                dark=False, inventory=inventory,
            )

        dark = room.dark and not self._room_is_lit(player, room)
        if dark:
            return Scene(
                room=room, room_id=room_key(room), name=room.name,
                description=room.description, dark=True,
                inventory=inventory,
            )

        exits = []
        for direction, target in room.exits.items():
            lock = room.exit_locks.get(direction)
            exits.append(SceneExit(
                direction=direction,
                to_id=room_key(target),
                locked=room.exit_is_locked(direction),
                label=lock.label if lock is not None else None,
            ))
        return Scene(
            room=room,
            room_id=room_key(room),
            name=room.name,
            description=room.description,
            dark=False,
            objects=tuple(
                content for content in room.contents
                if not isinstance(content, Player)
            ),
            occupants=tuple(
                other for other in self._players.values()
                if other.location is room and other is not player
            ),
            npcs=tuple(room.npcs),
            exits=tuple(exits),
            geometry=room.geometry,
            inventory=inventory,
        )

    def visible_room_state(self, player) -> Dict[str, Any]:
        """Compatibility projection of `scene_for` for the v1 terminal.

        New code should consume `scene_for` directly. #137 retires the v1
        terminal; until then this keeps its old name-only view available.
        """
        scene = self.scene_for(player)
        return {
            "name": scene.name,
            "description": scene.description,
            "dark": scene.dark,
            "items": sorted(obj.name for obj in scene.objects),
            "occupants": sorted(person.name for person in scene.occupants),
        }

    @staticmethod
    def _look_text(scene: Scene) -> str:
        """The `look` reply from one scene, matching the v2 `text` sets."""
        if scene.room is None:
            return "You feel lost in a void."
        if scene.dark:
            return "It is too dark to see anything."

        text = scene.description
        if scene.objects:
            listed = ", ".join(
                sorted(obj.name for obj in scene.objects))
            text += f"\n\nHere you see: {listed}."
        if scene.npcs:
            listed = ", ".join(
                npc.name for npc in sorted(
                    scene.npcs, key=lambda npc: npc.name.lower()))
            text += f"\n\nAlso here: {listed}."
        if scene.occupants:
            listed = ", ".join(
                person.name for person in sorted(
                    scene.occupants, key=lambda person: person.name.lower()))
            text += f"\n\nOthers in the room: {listed}."
        return text

    @staticmethod
    def _inventory_text(scene: Scene) -> str:
        """The `inventory` reply from one scene's carried set."""
        names = sorted(item.name for item in scene.inventory)
        if not names:
            return "You are not carrying anything."
        return "You are carrying: " + ", ".join(names) + "."


    @staticmethod
    def _containment_cycle(container: Entity, item: Entity) -> bool:
        """Whether putting `item` in `container` would close a cycle.

        A container may not contain itself, directly or transitively (#79).
        The rule is the validator's `container_cycles`; only the two subtrees
        that can close the proposed `container -> item` edge are walked, then
        the shared graph check decides.
        """
        graph: dict[str, list[str]] = {}

        def walk(node: Entity) -> None:
            key = str(id(node))
            if key in graph:
                return
            graph[key] = [str(id(child)) for child in node.contents]
            for child in node.contents:
                walk(child)

        walk(container)
        walk(item)
        graph.setdefault(str(id(container)), []).append(str(id(item)))
        return bool(container_cycles(graph))


    @staticmethod
    def _find_exit(room, arg: str):
        """Resolve `arg` to one of `room`'s exits.

        A player may name the way by direction (`east`), by the room it leads
        to (`tavern`, `old tavern`), or by the author's door label (`iron
        door`). Returns `(direction, target, lock)` where `lock` is None for an
        open way, or None when nothing matches.
        """
        if room is None:
            return None
        name = re.sub(r"^(the|a|an)\s+", "", arg.strip().lower())
        if not name:
            return None
        for direction, target in sorted(room.exits.items()):
            lock = room.exit_locks.get(direction)
            candidates = {direction.lower(), target.name.lower()}
            if lock is not None and lock.label:
                candidates.add(lock.label.lower())
            if name in candidates or any(
                len(name) >= 3 and name in candidate
                for candidate in candidates
            ):
                return direction, target, lock
        return None


    @staticmethod
    def _effect_source(owner: Any) -> dict[str, str]:
        """The stable identity of the room, object, or NPC owning an effect."""
        if isinstance(owner, Room):
            return {"kind": "room", "id": room_key(owner), "name": owner.name}
        if isinstance(owner, NPC):
            return {"kind": "npc", "id": npc_key(owner), "name": owner.name}
        return {"kind": "object", "id": object_key(owner), "name": owner.name}

    async def fire_effects(
        self,
        owner: Any,
        trigger: str,
        *,
        actor: str | None = None,
        room: Room | None = None,
    ) -> list[dict[str, Any]]:
        """Publish every effect `owner` authors for `trigger` on the bus.

        This is the only trigger seam: an `effect` event per authored entry,
        carrying the stable payload from `content.effects`. Storage appends a
        row through the sink installed in `__init__`; a client sees the event
        only when the server enables `features.effects`. The engine never
        consults that flag, so effects cannot change a reply, a location, or
        an inventory (#84).

        `room` is where the effect happens; a room-owned effect defaults to
        itself. `actor` is the display name of the character whose command
        caused it, when there is one. Returns the payloads it published.
        """
        if room is None and isinstance(owner, Room):
            room = owner
        room_data = (
            {"id": room_key(room), "name": room.name}
            if room is not None else None
        )
        source = self._effect_source(owner)
        published: list[dict[str, Any]] = []
        for effect in effect_entries(owner, trigger):
            payload = effect_payload(
                effect,
                trigger=trigger,
                source=source,
                actor=actor,
                room=room_data,
            )
            if payload is None:
                continue
            published.append(payload)
            await self.emit("effect", payload)
        return published

    async def _record_effect(self, event_type: str, data: Any) -> None:
        """Append one triggered effect to `event_log` (#84).

        The log is part of the bus seam, not a second write-through rule: an
        effect is presentation/hardware intent, so a logging failure is
        reported and dropped rather than turning a valid command into
        `internal`. Without a store (the v1 server, unit tests) this is a
        no-op and the bus still carries the event.
        """
        if self.store is None or not isinstance(data, dict):
            return
        actor = data.get("actor")
        room = data.get("room")
        try:
            self.store.record_event(
                event_type,
                data,
                actor=actor.get("name") if isinstance(actor, dict) else None,
                room_id=room.get("id") if isinstance(room, dict) else None,
            )
        except Exception:
            logger.exception("could not append an effect to event_log")

    async def subscribe(self, event_type: str, callback: Callable) -> None:
        """Registers a listener for semantic events."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    async def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove one listener; a missing listener is a no-op."""
        callbacks = self._subscribers.get(event_type)
        if callbacks is None:
            return
        try:
            callbacks.remove(callback)
        except ValueError:
            pass

    async def emit(self, event_type: str, data: Any) -> None:
        """Broadcasts internal game changes."""
        if event_type in self._subscribers:
            for callback in self._subscribers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event_type, data)
                    else:
                        callback(event_type, data)
                except Exception as e:
                    print(f"Event Error ({event_type}): {e}")

    # --- Typed command outcomes (#130) ---------------------------------------

    # The one alias table (#133). `parse_command` uses it; `process_command`
    # delegates to `execute`, so there is no second copy to keep in sync.
    _ALIASES: ClassVar[dict[str, str]] = {
        "get": "take", "pick": "take", "grab": "take",
        "l": "look",
        "n": "go north", "north": "go north",
        "s": "go south", "south": "go south",
        "e": "go east", "east": "go east",
        "w": "go west", "west": "go west",
        "u": "go up", "up": "go up",
        "d": "go down", "down": "go down",
        "exits": "show_exits",
        "i": "inventory", "inv": "inventory",
        "h": "help",
        "exam": "examine",
        "me": "emote",
    }

    @staticmethod
    def parse_command(text: str) -> tuple[str, str | None, str]:
        """Resolve one input line to `(verb, arg, raw)`, applying the aliases.

        The single grammar owner (#130): the engine parses the command. A
        blank line is ``"empty"``; ``look at`` / ``look in`` are rewritten
        here, once.
        """
        raw = text.strip()
        lowered = raw.lower()
        if not lowered:
            return "empty", None, raw

        parts = lowered.split(maxsplit=1)
        verb = parts[0]
        arg = parts[1] if len(parts) > 1 else None

        if verb == "look" and arg is not None:
            first = arg.split(maxsplit=1)[0]
            if first == "at":
                rest = arg.split(maxsplit=1)
                return "examine", rest[1].strip() if len(rest) > 1 else None, raw
            if first in ("in", "inside"):
                rest = arg.split(maxsplit=1)
                return "look_in", rest[1].strip() if len(rest) > 1 else "", raw
            return "look", arg, raw

        input_str = f"{verb} {arg}".strip() if arg else verb
        if verb == "get" and arg and " from " in arg:
            # `get <item> from <container>` is its own verb; a bare `get <item>`
            # still aliases to `take` below.
            return "get", arg, raw
        if input_str in Engine._ALIASES:
            resolved, _, resolved_arg = Engine._ALIASES[input_str].partition(" ")
            return resolved, resolved_arg or None, raw
        if verb in Engine._ALIASES:
            # A single-word alias rewrites only the verb; the argument stays
            # (`exam gold coin` -> examine / "gold coin").
            resolved, _, resolved_arg = Engine._ALIASES[verb].partition(" ")
            return resolved, resolved_arg or arg, raw
        return verb, arg, raw

    @staticmethod
    def _semantic_object(entity: Entity,
                         collection: list[Entity] | None = None) -> dict[str, Any]:
        """A stable object descriptor for a semantic event.

        The id is the entity's stable key; the loader assigns it once (#139).
        Shaping an event never mutates identity.
        """
        base = getattr(entity, "item_id", None) or slug(entity.name)
        weight = round(float(getattr(entity, "weight", 0.0) or 0.0), 3)
        return {"id": object_key(entity), "kind": base, "name": entity.name,
                "weight": weight}

    async def execute(self, actor: Player, text: str) -> CommandOutcome:
        """Run one command for `actor` and return a typed outcome (#130).

        The single command path: it parses once, dispatches to the verb
        handler, and the handler records the reply, typed status, and semantic
        events. The v2 adapter shapes that outcome; `process_command` is a
        string shim over it for the v1 server and the unit tests.
        """
        verb, arg, _raw = self.parse_command(text)
        outcome = CommandOutcome()
        if verb == "empty":
            self._notice(outcome, "Silence falls over you.")
            await self.emit("command_processed", {
                "raw": text, "clean": text.strip().lower(),
                "reply": outcome.reply,
            })
            return outcome
        if verb in ("exit", "quit"):
            await self.emit("player_quit", {"reason": "user_requested"})
            outcome.reply = "Goodbye!"
            return outcome

        handlers = {
            "take": self._exec_take,
            "drop": self._exec_drop,
            "open": self._exec_open,
            "go": self._exec_go,
            "look": self._exec_look,
            "inventory": self._exec_inventory,
            "who": self._exec_who,
            "show_exits": self._exec_show_exits,
            "help": self._exec_help,
            "hello": self._exec_hello,
            "examine": self._exec_examine,
            "read": self._exec_read,
            "look_in": self._exec_look_in,
            "put": self._exec_put,
            "get": self._exec_get,
            "close": self._exec_close,
            "use": self._exec_use,
            "equip": self._exec_equip,
            "unequip": self._exec_unequip,
            "extinguish": self._exec_extinguish,
            "talk": self._exec_talk,
            "ask": self._exec_ask,
            "light": self._exec_light,
        }
        raw_handlers = {
            "say": self._exec_say,
            "emote": self._exec_emote,
            "tell": self._exec_tell,
        }
        handler = handlers.get(verb)
        raw_handler = raw_handlers.get(verb)
        try:
            if raw_handler is not None:
                await raw_handler(outcome, actor, arg, text)
            elif handler is not None:
                await handler(outcome, actor, arg)
            else:
                self._fail(
                    outcome, "unknown_command",
                    "Hmm, I don't understand that. Type `help` for a list of "
                    "commands.")
        except StorageWriteError:
            # A commit failed, so the engine changed nothing: one typed error,
            # no event for any other client (SPEC v2.1.0 criterion 2).
            logger.exception("write-through failed on %r", text)
            outcome.status = "error"
            outcome.error_code = "internal"
            outcome.reply = (
                "The world could not save that command; nothing changed.")
            outcome.error_data = {}
            outcome.events = []
            return outcome

        await self.emit("command_processed", {
            "raw": text, "clean": text.strip().lower(),
            "reply": outcome.reply,
        })
        return outcome

    @staticmethod
    def _fail(outcome: CommandOutcome, code: str, reply: str) -> None:
        """Record a typed failure on `outcome`."""
        outcome.status = "error"
        outcome.error_code = code
        outcome.reply = reply

    def _candidates(self, collection: list[Entity],
                    arg: str | None) -> list[dict[str, Any]]:
        """The structured ambiguity candidates for `arg` in `collection` (#78).

        Empty when the name resolves uniquely or not at all; an `ambiguous`
        outcome attaches the list so a client can disambiguate.
        """
        if not arg:
            return []
        matches = self.target_matches(collection, arg)
        if len(matches) < 2:
            return []
        return [self._semantic_object(item, collection) for item in matches]

    def _set_too_heavy(self, outcome: CommandOutcome, actor: Player,
                       collection: list[Entity], arg: str | None,
                       reply: str, item: Entity | None = None) -> None:
        """Record the typed carry-limit refusal with the numbers behind it (#78)."""
        self._fail(outcome, "too_heavy", reply)
        data: dict[str, Any] = {
            "capacity": round(self.carry_capacity(actor), 3),
            "used": round(self.carry_used(actor), 3),
        }
        found = item if item is not None else self._find_item(collection, arg)
        if found is not None:
            data["object"] = self._semantic_object(found, collection)
            data["weight"] = round(
                float(getattr(found, "weight", 0.0) or 0.0), 3)
        outcome.error_data = data

    async def _exec_take(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`take <item>`: move one room item into the actor's pack."""
        if not arg:
            self._fail(outcome, "bad_args", "Take what? Try `take <item>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here",
                "There is nowhere here to take anything from.")
            return
        before_room_contents = list(room.contents)
        item, disambig = self._disambiguate(room.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(before_room_contents, arg)}
            return
        if item is None:
            self._fail(outcome, "not_found", f"There is no {arg} here.")
            return
        if not self.can_carry(actor, item):
            self._set_too_heavy(
                outcome, actor, before_room_contents, arg,
                f"You cannot carry the {item.name}; it is too heavy.", item)
            return
        lit_slot = None
        if getattr(item, "light_source", False) and getattr(
                item, "lit", False):
            lit_slot, error = self._equip_target(
                actor, item, light_only=True)
            if error is not None:
                if error == "bad_args":
                    self._fail(
                        outcome, "bad_args",
                        f"The lit {item.name} cannot be held.")
                else:
                    self._fail(
                        outcome, "no_slot",
                        f"You have no free hand, belt, or head for the lit "
                        f"{item.name}.")
                return
        changes = ChangeSet("take").place(item, at=actor)
        if lit_slot is not None:
            changes.set(item, "slot", lit_slot)
        self._commit(changes)
        room.remove_content(item)
        actor.add_content(item)
        if lit_slot is not None:
            actor.equip(item, lit_slot)
        await self.emit("item_taken", {
            "item_name": item.name.lower(), "owner": actor.name})
        outcome.reply = f"You picked up the {item.name}."
        obj = self._semantic_object(item, before_room_contents)
        outcome.events.append(SemanticEvent(
            kind="object.taken",
            data={"object": obj, "by": actor.name,
                  "text_actor": outcome.reply},
            scope="actor",
        ))
        outcome.events.append(SemanticEvent(
            kind="player.inventory", data={"actor": actor},
            scope="actor", direct=False,
        ))
        outcome.events.append(SemanticEvent(
            kind="object.taken",
            data={"object": obj, "by": actor.name,
                  "text_room": f"{actor.name} picks up the {item.name}."},
            scope="room_others",
        ))

    async def _exec_drop(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`drop <item>`: move one carried item into the actor's room."""
        if not arg:
            self._fail(outcome, "bad_args", "Drop what? Try `drop <item>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here",
                "There is nowhere here to drop anything.")
            return
        item, disambig = self._disambiguate(actor.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if item is None:
            self._fail(
                outcome, "not_found", f"You aren't carrying any {arg}.")
            return
        changes = ChangeSet("drop").place(item, at=room)
        if getattr(item, "equipped_slot", None) is not None:
            changes.set(item, "slot", None)
        self._commit(changes)
        actor.unequip(item)
        actor.remove_content(item)
        room.add_content(item)
        await self.emit("item_dropped", {
            "item_name": item.name.lower(), "location": room.name})
        outcome.reply = f"You dropped the {item.name}."
        obj = self._semantic_object(item, room.contents)
        outcome.events.append(SemanticEvent(
            kind="object.dropped",
            data={"object": obj, "by": actor.name,
                  "text_actor": outcome.reply},
            scope="actor",
        ))
        outcome.events.append(SemanticEvent(
            kind="player.inventory", data={"actor": actor},
            scope="actor", direct=False,
        ))
        outcome.events.append(SemanticEvent(
            kind="object.dropped",
            data={"object": obj, "by": actor.name,
                  "text_room": f"{actor.name} drops the {item.name}."},
            scope="room_others",
        ))

    async def _exec_open(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`open <container>`: set the lid open and report the contents."""
        if not arg:
            self._fail(outcome, "bad_args", "Open what? Try `open <chest>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here",
                "There is nowhere here to open anything.")
            return
        container, disambig = self._disambiguate(room.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(room.contents, arg)}
            return
        if container is None:
            self._fail(
                outcome, "not_found", f"There is no {arg} here to open.")
            return
        if getattr(container, "locked", False):
            self._fail(
                outcome, "locked",
                f"The {container.name} is locked. Find a key and "
                f"`use <key> on {container.name.lower()}`.")
            return
        if container.is_container:
            if container.open:
                self._fail(
                    outcome, "already_open",
                    f"The {container.name} is already open.")
                return
            if self.store is not None:
                self._commit(
                    ChangeSet("open").set(container, "open", True))
            container.open = True
            await self.fire_effects(
                container, "on_open", actor=actor.name, room=room)

        loot = list(container.contents)
        if not loot:
            outcome.reply = f"The {container.name} is empty."
        else:
            await self.emit("chest_opened", {
                "item_name": container.name.lower(),
                "location": room.name,
            })
            listed = ", ".join(sorted(c.name for c in loot))
            outcome.reply = (
                f"You open the {container.name}. Inside you see: {listed}.")
        outcome.events.append(SemanticEvent(
            kind="object.opened",
            data={
                "object": self._semantic_object(container, room.contents),
                "by": actor.name,
                "text_actor": outcome.reply,
                "text_room": f"{actor.name} opens the {container.name}.",
            },
            scope="room",
        ))

    async def _exec_go(self, outcome: CommandOutcome, actor: Player,
                       arg: str | None) -> None:
        """`go <direction>`: move the actor, or refuse a locked/missing way."""
        if not arg:
            self._fail(
                outcome, "bad_args", "Go where? Try `go <direction>`.")
            return
        direction = arg.strip()
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here",
                "You have no presence here to move.")
            return
        if room.exit_is_locked(direction):
            await self.emit("exit_locked", {
                "direction": direction, "location": room.name})
            self._fail(outcome, "locked", self._locked_exit_reply(direction))
            # The room sees the attempt; the actor gets the typed refusal above.
            outcome.events.append(SemanticEvent(
                kind="exit.locked",
                data={
                    "actor": actor.name,
                    "room": room,
                    "direction": direction,
                    "target": room.exits.get(direction),
                },
                scope="room_others",
            ))
            return

        old_room = room
        moved, result = await self._move_actor(actor, direction)
        if not moved:
            self._fail(outcome, "no_exit", result)
            return

        outcome.reply = self._describe_movement(
            self.scene_for(actor), direction, old_location=old_room)
        outcome.events.append(SemanticEvent(
            kind="room.state",
            data={"actor": actor},
            scope="actor",
        ))
        outcome.events.append(SemanticEvent(
            kind="room.leave",
            data={
                "actor": actor.name,
                "room": old_room,
                "direction": direction,
                "text": f"{actor.name} leaves.",
            },
            scope="room_others",
        ))
        outcome.events.append(SemanticEvent(
            kind="room.enter",
            data={
                "actor": actor.name,
                "room": actor.location,
                "direction": direction,
                "text": f"{actor.name} arrives.",
            },
            scope="room_others",
        ))

    async def _exec_look(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`look`: render the actor's scene; the adapter builds `room.state`."""
        outcome.reply = self._look_text(self.scene_for(actor))
        outcome.events.append(SemanticEvent(
            kind="room.state", data={"actor": actor}, scope="actor"))

    async def _exec_inventory(self, outcome: CommandOutcome, actor: Player,
                              arg: str | None) -> None:
        """`inventory`: the actor's pack from the same scene."""
        outcome.reply = self._inventory_text(self.scene_for(actor))
        outcome.events.append(SemanticEvent(
            kind="player.inventory",
            data={"actor": actor, "text": outcome.reply},
            scope="actor",
        ))

    async def _exec_who(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None) -> None:
        """`who`: the server-wide roster, awake and asleep (#72/#75)."""
        roster = self.who_entries()
        if not roster:
            outcome.reply = "There is no one in the world."
        else:
            listed = []
            for entry in roster:
                state = (
                    "asleep, turned to stone"
                    if entry["asleep"] else "awake")
                where = entry["room_id"] or "nowhere"
                listed.append(f"{entry['name']} ({where}, {state})")
            outcome.reply = (
                "Characters in the world: " + "; ".join(listed) + ".")
        outcome.events.append(SemanticEvent(
            kind="who.list",
            data={"actor": actor, "text": outcome.reply},
            scope="actor",
        ))

    async def _exec_show_exits(self, outcome: CommandOutcome, actor: Player,
                               arg: str | None) -> None:
        """`exits`: the actor's room exits, with locked ones marked."""
        room = actor.location
        if room is None:
            outcome.reply = "You have no context for exits here."
        elif not room.exits:
            outcome.reply = "There are no exits here."
        else:
            names = self._exit_labels(room)
            outcome.reply = f"Available exits: {', '.join(names)}."
        self._notice(outcome, outcome.reply)

    async def _exec_help(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`help`: the command list."""
        outcome.reply = (
            "Commands: look (l), go <direction>, exits, take <item>, "
            "get <item> from <container>, drop <item>, put <item> in <container>, "
            "open <container>, close <container>, use <key> on <container|exit>, "
            "equip <item> [on <slot>], unequip <item>, extinguish <light>, "
            "examine <thing> (exam), read <thing> (signs, books), look in <chest>, "
            "light <torch/lantern>, inventory (i), who, "
            "say <text>, emote <action> (me), tell <player> <text>, "
            "talk <npc>, ask <npc> about <topic>, hello. Try `look` first."
        )
        self._notice(outcome, outcome.reply)

    async def _exec_hello(self, outcome: CommandOutcome, actor: Player,
                          arg: str | None) -> None:
        """`hello`: the greeting line."""
        outcome.reply = "The world listens..."
        self._notice(outcome, outcome.reply)

    @staticmethod
    def _notice(outcome: CommandOutcome, text: str) -> None:
        """Record a plain informational notice."""
        outcome.reply = text
        outcome.events.append(SemanticEvent(
            kind="notice", data={"text": text}, scope="actor"))

    async def _exec_examine(self, outcome: CommandOutcome, actor: Player,
                            arg: str | None) -> None:
        """`examine`: describe a held or visible thing, or a person here."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Examine what? Try `examine <thing>`.")
            return
        room = actor.location
        held, held_disambig = self._disambiguate(actor.contents, arg)
        if not self._room_is_lit(actor, room):
            self._notice(outcome, "It is too dark to see anything.")
            return
        if held_disambig:
            self._fail(outcome, "ambiguous", held_disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if held is not None:
            self._notice(outcome, f"It is {held.description}.")
            return
        room_items = [c for c in (room.contents if room is not None else [])
                      if not isinstance(c, Player)]
        item, disambig = self._disambiguate(room_items, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(room_items, arg)}
            return
        if item is not None:
            self._notice(outcome, f"It is {item.description}.")
            return
        target = self._character_in_room(room, arg)
        if target is not None:
            self._notice(
                outcome, f"{target.name} is here, exploring the world.")
            return
        self._fail(outcome, "not_found", f"You see no {arg} here.")

    async def _exec_read(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`read`: show a readable object's text; plain items are refused."""
        if not arg:
            # An argument-less `read` falls through the engine to the
            # unknown-command reply; the adapter classifies it as such.
            self._fail(
                outcome, "unknown_command",
                "Hmm, I don't understand that. Type `help` for a list of "
                "commands.")
            return
        room = actor.location
        held, held_disambig = self._disambiguate(actor.contents, arg)
        if not self._room_is_lit(actor, room):
            self._notice(outcome, "It is too dark to see anything.")
            return
        if held_disambig:
            self._fail(outcome, "ambiguous", held_disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if held is not None and held.readable:
            self._read_event(
                outcome, held, actor.contents, f"It is {held.description}.")
            return
        room_items = [c for c in (room.contents if room is not None else [])
                      if not isinstance(c, Player)]
        item, disambig = self._disambiguate(room_items, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(room_items, arg)}
            return
        if item is not None and item.readable:
            self._read_event(
                outcome, item, room_items, f"It is {item.description}.")
            return
        target = self._character_in_room(room, arg)
        if target is not None:
            self._fail(outcome, "not_found", f"You can't read {target.name}.")
            return
        self._fail(outcome, "not_found", "There is nothing readable here.")

    def _read_event(self, outcome: CommandOutcome, target: Entity,
                    collection: list[Entity], reply: str) -> None:
        """Record a successful read as an actor-only `object.read` event."""
        outcome.reply = reply
        outcome.events.append(SemanticEvent(
            kind="object.read",
            data={
                "object": self._semantic_object(target, collection),
                "content": target.description,
                "text": reply,
            },
            scope="actor",
        ))

    async def _exec_look_in(self, outcome: CommandOutcome, actor: Player,
                            arg: str | None) -> None:
        """`look in <container>`: list what a container holds."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Look in what? Try `look in <container>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here", "There is nowhere here to look inside.")
            return
        container, disambig = self._disambiguate(room.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(room.contents, arg)}
            return
        if container is None:
            self._fail(
                outcome, "not_found", f"There is no {arg} here to look inside.")
            return
        if getattr(container, "locked", False):
            self._fail(
                outcome, "locked",
                f"The {container.name} is locked. Find a key and "
                f"`use <key> on {container.name.lower()}`.")
            return
        loot = list(container.contents)
        if not loot:
            self._notice(outcome, f"The {container.name} is empty.")
            return
        listed = ", ".join(sorted(c.name for c in loot))
        self._notice(
            outcome, f"You see inside the {container.name}: {listed}.")

    def _character_in_room(self, room: Room | None,
                           name: str) -> Player | None:
        """The player named `name` sharing `room`, ignoring case."""
        key = name.strip().lower()
        return next(
            (p for p in self._players.values()
             if (room is None or p.location is room) and p.name.lower() == key),
            None,
        )

    async def _exec_put(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None) -> None:
        """`put <item> in <container>`: move a carried item into a container."""
        normalized = (arg or "").replace(" into ", " in ")
        if " in " not in normalized:
            self._fail(
                outcome, "bad_args",
                "Put what where? Try `put <item> in <container>`.")
            return
        item_arg, _, container_arg = normalized.rpartition(" in ")
        item_arg = item_arg.strip()
        container_arg = container_arg.strip()
        if not item_arg or not container_arg:
            self._fail(
                outcome, "bad_args",
                "Put what where? Try `put <item> in <container>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here", "There is nowhere here to put anything.")
            return
        before_room_contents = list(room.contents)
        before_inventory = list(actor.contents)
        held, item_disambig = self._disambiguate(
            actor.contents, item_arg)
        container, cont_disambig = self._disambiguate(
            room.contents, container_arg)
        if item_disambig:
            self._fail(outcome, "ambiguous", item_disambig)
            outcome.error_data = {
                "candidates": self._candidates(before_inventory, item_arg)}
            return
        if cont_disambig:
            self._fail(outcome, "ambiguous", cont_disambig)
            outcome.error_data = {
                "candidates": self._candidates(
                    before_room_contents, container_arg)}
            return
        if held is None:
            self._fail(
                outcome, "not_found",
                f"You aren't carrying any {item_arg}.")
            return
        if container is None:
            self._fail(
                outcome, "not_found",
                f"There is no {container_arg} here to put it in.")
            return
        if getattr(container, "locked", False):
            self._fail(
                outcome, "locked",
                f"The {container.name} is locked. Find a key and "
                f"`use <key> on {container.name.lower()}`.")
            return
        if getattr(held, "light_source", False) and getattr(
                held, "lit", False):
            self._fail(
                outcome, "still_lit",
                f"Put out the {held.name} before you stow it.")
            return
        if self._containment_cycle(container, held):
            self._fail(
                outcome, "container_cycle",
                f"You cannot put the {held.name} in the {container.name}; "
                "a container cannot contain itself, directly or transitively.")
            outcome.error_data = {
                "object": self._semantic_object(held, before_inventory),
                "container": self._semantic_object(
                    container, before_room_contents),
            }
            return
        changes = ChangeSet("put").place(held, at=container)
        if getattr(held, "equipped_slot", None) is not None:
            changes.set(held, "slot", None)
        self._commit(changes)
        actor.unequip(held)
        actor.remove_content(held)
        container.add_content(held)
        await self.emit("item_put", {
            "item_name": held.name.lower(),
            "container": container.name.lower(),
            "location": room.name,
        })
        outcome.reply = f"You put the {held.name} in the {container.name}."
        obj = self._semantic_object(held, container.contents)
        cont = self._semantic_object(container, before_room_contents)
        room_text = (
            f"{actor.name} puts the {held.name} in the {container.name}.")
        outcome.events.append(SemanticEvent(
            kind="object.put",
            data={"object": obj, "container": cont, "by": actor.name,
                  "text_actor": outcome.reply},
            scope="actor",
        ))
        outcome.events.append(SemanticEvent(
            kind="player.inventory", data={"actor": actor},
            scope="actor", direct=False))
        outcome.events.append(SemanticEvent(
            kind="object.put",
            data={"object": obj, "container": cont, "by": actor.name,
                  "text_room": room_text},
            scope="room_others",
        ))

    async def _exec_get(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None) -> None:
        """`get <item> from <container>`: pull one item into the pack."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Get what from what? Try `get <item> from <container>`.")
            return
        if " from " not in arg:
            # The engine leaves a `get` without `from` to its "Get what from
            # what?" reply, which the adapter surfaces as a notice.
            self._notice(
                outcome,
                "Get what from what? Try `get <item> from <container>`.")
            return
        item_arg, _, container_arg = arg.rpartition(" from ")
        item_arg = item_arg.strip()
        container_arg = container_arg.strip()
        if not item_arg or not container_arg:
            self._notice(
                outcome,
                "Get what from what? Try `get <item> from <container>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here",
                "There is nowhere here to get anything from.")
            return
        before_room_contents = list(room.contents)
        container, cont_disambig = self._disambiguate(
            room.contents, container_arg)
        if cont_disambig:
            self._fail(outcome, "ambiguous", cont_disambig)
            outcome.error_data = {
                "candidates": self._candidates(
                    before_room_contents, container_arg)}
            return
        if container is None:
            self._fail(
                outcome, "not_found", f"There is no {container_arg} here.")
            return
        if not container.is_container:
            self._fail(
                outcome, "bad_args",
                f"The {container.name} is not a container.")
            return
        if not container.open:
            self._fail(
                outcome, "container_closed",
                f"The {container.name} is closed. Open it first.")
            return
        item, item_disambig = self._disambiguate(
            container.contents, item_arg)
        if item_disambig:
            self._fail(outcome, "ambiguous", item_disambig)
            outcome.error_data = {
                "candidates": self._candidates(
                    container.contents, item_arg)}
            return
        if item is None:
            self._fail(
                outcome, "not_here",
                f"The {item_arg} is not in the {container.name}.")
            return
        if not getattr(item, "portable", True):
            self._fail(
                outcome, "not_portable",
                f"The {item.name} is fixed in place and cannot be put in "
                "your pack.")
            return
        if not self.can_carry(actor, item):
            self._set_too_heavy(
                outcome, actor, container.contents, item_arg,
                f"You cannot carry the {item.name}; it is too heavy.", item)
            return
        lit_slot = None
        if getattr(item, "light_source", False) and getattr(
                item, "lit", False):
            lit_slot, error = self._equip_target(
                actor, item, light_only=True)
            if error is not None:
                if error == "bad_args":
                    self._fail(
                        outcome, "bad_args",
                        f"The lit {item.name} cannot be held.")
                else:
                    self._fail(
                        outcome, "no_slot",
                        f"You have no free hand, belt, or head for the lit "
                        f"{item.name}.")
                return
        changes = ChangeSet("get").place(item, at=actor)
        if lit_slot is not None:
            changes.set(item, "slot", lit_slot)
        self._commit(changes)
        container.remove_content(item)
        actor.add_content(item)
        if lit_slot is not None:
            actor.equip(item, lit_slot)
        await self.emit("item_got", {
            "item_name": item.name.lower(),
            "container": container.name.lower(),
            "location": room.name,
        })
        outcome.reply = (
            f"You get the {item.name} from the {container.name}.")
        obj = self._semantic_object(item, actor.contents)
        cont = self._semantic_object(container, before_room_contents)
        room_text = (
            f"{actor.name} gets the {item.name} from the {container.name}.")
        outcome.events.append(SemanticEvent(
            kind="object.got",
            data={"object": obj, "container": cont, "by": actor.name,
                  "text_actor": outcome.reply},
            scope="actor",
        ))
        outcome.events.append(SemanticEvent(
            kind="player.inventory", data={"actor": actor},
            scope="actor", direct=False))
        outcome.events.append(SemanticEvent(
            kind="object.got",
            data={"object": obj, "container": cont, "by": actor.name,
                  "text_room": room_text},
            scope="room_others",
        ))

    async def _exec_close(self, outcome: CommandOutcome, actor: Player,
                          arg: str | None) -> None:
        """`close <container>`: put the lid back down; contents stay."""
        if not arg:
            self._fail(
                outcome, "bad_args", "Close what? Try `close <chest>`.")
            return
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here", "There is nowhere here to close anything.")
            return
        before_room_contents = list(room.contents)
        container, disambig = self._disambiguate(room.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(before_room_contents, arg)}
            return
        if container is None:
            self._fail(
                outcome, "not_found", f"There is no {arg} here to close.")
            return
        if not container.is_container:
            self._fail(
                outcome, "bad_args",
                f"The {container.name} is not something you can close.")
            return
        if not container.open:
            self._fail(
                outcome, "already_closed",
                f"The {container.name} is already closed.")
            return
        if self.store is not None:
            self._commit(
                ChangeSet("close").set(container, "open", False))
        container.open = False
        await self.emit("container_closed", {
            "item_name": container.name.lower(),
            "location": room.name,
        })
        outcome.reply = f"You close the {container.name}."
        outcome.events.append(SemanticEvent(
            kind="object.closed",
            data={"object": self._semantic_object(
                      container, before_room_contents),
                  "by": actor.name,
                  "text_actor": outcome.reply,
                  "text_room": f"{actor.name} closes the {container.name}."},
            scope="room",
        ))

    def _equip_target(
        self, actor: Player, item: Entity, requested: str | None = None,
        *, light_only: bool = False,
    ) -> tuple[str | None, str | None]:
        """Pick a free runtime slot for `item`, or return a typed error."""
        authored = tuple(getattr(item, "equip_slots", ()))
        if not authored and light_only and getattr(item, "light_source", False):
            # Backward compatibility for in-memory lights and the frozen v2.0
            # JSON fixture: an undeclared light can still be held.
            authored = ("hand",)
        if not authored:
            return None, "bad_args"
        hands = int(getattr(item, "hands", 1) or 1)
        occupied = {
            slot: held for slot, held in actor.equipment.items()
            if held is not item
        }
        for slot, held in list(occupied.items()):
            if slot in HAND_SLOTS and int(
                    getattr(held, "hands", 1) or 1) == 2:
                for hand in HAND_SLOTS:
                    occupied.setdefault(hand, held)
        light_slots = LIGHT_SLOTS if light_only else None

        def allowed(slot: str) -> bool:
            return light_slots is None or slot in light_slots

        if requested is not None:
            if requested in HAND_SLOTS:
                if "hand" not in authored or not allowed(requested):
                    return None, "bad_args"
                if hands == 2 and requested != "hand_1":
                    return None, "bad_args"
                if hands == 2:
                    if any(slot in occupied for slot in HAND_SLOTS):
                        return None, "no_slot"
                    return "hand_1", None
                if requested in occupied:
                    return None, "no_slot"
                return requested, None
            if requested == "hand":
                if "hand" not in authored or not any(
                        allowed(slot) for slot in HAND_SLOTS):
                    return None, "bad_args"
                if hands == 2:
                    if any(slot in occupied for slot in HAND_SLOTS):
                        return None, "no_slot"
                    return "hand_1", None
                for slot in HAND_SLOTS:
                    if slot not in occupied and allowed(slot):
                        return slot, None
                return None, "no_slot"
            if requested not in authored or not allowed(requested):
                return None, "bad_args"
            if requested in occupied:
                return None, "no_slot"
            return requested, None

        if "hand" in authored:
            if hands == 2:
                if all(
                        slot not in occupied and allowed(slot)
                        for slot in HAND_SLOTS):
                    return "hand_1", None
            else:
                for slot in HAND_SLOTS:
                    if slot not in occupied and allowed(slot):
                        return slot, None
        for slot in authored:
            if slot == "hand":
                continue
            if slot not in occupied and allowed(slot):
                return slot, None
        return None, "no_slot"

    @staticmethod
    def _slot_label(slot: str) -> str:
        return "hand" if slot in HAND_SLOTS else slot

    async def _exec_equip(self, outcome: CommandOutcome, actor: Player,
                          arg: str | None) -> None:
        """`equip <item> [on <slot>]`: put an item in a body slot."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Equip what? Try `equip <item>` or `equip <item> on belt`.")
            return
        normalized = arg.strip()
        requested = None
        if " on " in normalized:
            item_arg, _, slot_arg = normalized.rpartition(" on ")
            requested = slot_arg.strip().lower()
            requested = requested.replace("_", " ").replace("-", " ").strip()
            requested = {
                "hand 1": "hand_1",
                "hand 2": "hand_2",
                "left hand": "hand_1",
                "right hand": "hand_2",
            }.get(requested, requested)
            if requested == "hands":
                requested = "hand"
            if requested not in (set(EQUIP_SLOTS) | set(HAND_SLOTS)):
                self._fail(
                    outcome, "bad_args",
                    "Slots are hand, hand 1, hand 2, head, body, back, "
                    "belt, or feet.")
                return
        else:
            item_arg = normalized
        item, disambig = self._disambiguate(actor.contents, item_arg.strip())
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, item_arg)}
            return
        if item is None:
            self._fail(
                outcome, "not_found", f"You aren't carrying any {item_arg}.")
            return
        target, error = self._equip_target(
            actor, item, requested,
            light_only=bool(item.light_source and item.lit))
        displaced: list[Entity] = []
        if error == "no_slot" and requested in HAND_SLOTS:
            hands = int(getattr(item, "hands", 1) or 1)
            if hands == 2:
                candidates = [
                    actor.equipment.get(slot)
                    for slot in HAND_SLOTS
                    if actor.equipment.get(slot) is not None
                    and actor.equipment.get(slot) is not item
                ]
                if any(getattr(candidate, "light_source", False)
                       and getattr(candidate, "lit", False)
                       for candidate in candidates):
                    self._fail(
                        outcome, "still_lit",
                        "Put out the lit item before you swap it out.")
                    return
                target = "hand_1"
                displaced = candidates
                error = None
            elif requested in actor.equipment:
                candidate = actor.equipment[requested]
                if getattr(candidate, "light_source", False) and getattr(
                        candidate, "lit", False):
                    self._fail(
                        outcome, "still_lit",
                        f"Put out the {candidate.name} before you swap it "
                        "out.")
                    return
                target = requested
                displaced = [candidate]
                error = None
        if error is not None:
            if error == "no_slot":
                self._fail(
                    outcome, "no_slot",
                    f"The {item.name} has nowhere to go; free a slot first.")
            else:
                self._fail(
                    outcome, "bad_args",
                    f"The {item.name} cannot go there.")
            return
        changes = ChangeSet("equip").set(item, "slot", target)
        for stowed in displaced:
            changes.set(stowed, "slot", None)
        self._commit(changes)
        for stowed in displaced:
            actor.unequip(stowed)
        actor.equip(item, target)
        if displaced:
            names = ", ".join(stowed.name for stowed in displaced)
            outcome.reply = (
                f"You equip the {item.name} in your "
                f"{self._slot_label(target)} and stow the {names}.")
        else:
            outcome.reply = (
                f"You equip the {item.name} in your "
                f"{self._slot_label(target)}.")
        outcome.events.append(SemanticEvent(
            kind="player.inventory",
            data={"actor": actor, "text": outcome.reply},
            scope="actor",
        ))

    async def _exec_unequip(self, outcome: CommandOutcome, actor: Player,
                            arg: str | None) -> None:
        """`unequip <item>`: leave the item in the pack, slot free."""
        if not arg:
            self._fail(
                outcome, "bad_args", "Unequip what? Try `unequip <item>`.")
            return
        item, disambig = self._disambiguate(actor.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if item is None:
            self._fail(
                outcome, "not_found", f"You aren't carrying any {arg}.")
            return
        if getattr(item, "equipped_slot", None) is None:
            self._fail(
                outcome, "not_equipped", f"The {item.name} is not equipped.")
            return
        if getattr(item, "light_source", False) and getattr(
                item, "lit", False):
            self._fail(
                outcome, "still_lit",
                f"Put out the {item.name} before you stow it.")
            return
        self._commit(ChangeSet("unequip").set(item, "slot", None))
        actor.unequip(item)
        outcome.reply = f"You unequip the {item.name}."
        outcome.events.append(SemanticEvent(
            kind="player.inventory",
            data={"actor": actor, "text": outcome.reply},
            scope="actor",
        ))

    async def _exec_extinguish(self, outcome: CommandOutcome, actor: Player,
                               arg: str | None) -> None:
        """`extinguish <source>`: put out a lit light source."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Extinguish what? Try `extinguish <light>`.")
            return
        item, disambig = self._disambiguate(actor.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if item is None:
            self._fail(
                outcome, "not_found", f"You aren't carrying any {arg}.")
            return
        if not getattr(item, "light_source", False):
            self._fail(
                outcome, "bad_args", f"The {item.name} is not a light.")
            return
        if not getattr(item, "lit", False):
            self._notice(outcome, f"The {item.name} is already out.")
            return
        self._commit(ChangeSet("extinguish").set(item, "lit", False))
        item.lit = False
        outcome.reply = f"You put out the {item.name}."
        outcome.events.append(SemanticEvent(
            kind="player.inventory",
            data={"actor": actor, "text": outcome.reply},
            scope="actor",
        ))

    async def _exec_use(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None) -> None:
        """`use <key> on <lock|exit>`: turn a key to unlock a door or lid."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Use what on what? Try `use <key> on <lock>`.")
            return
        if " on " not in arg:
            self._fail(
                outcome, "bad_args",
                "Use a key on something: `use <key> on <lock|exit>`.")
            return
        key_name, lock_name = arg.split(" on ", 1)
        room = actor.location
        if room is None:
            self._fail(
                outcome, "not_here", "There is nowhere here to use a key.")
            return
        before_room_contents = list(room.contents)
        before_inventory = list(actor.contents)
        key_item, key_disambig = self._disambiguate(
            actor.contents, key_name)
        lock_item, lock_disambig = self._disambiguate(
            room.contents, lock_name)
        if key_disambig:
            self._fail(outcome, "ambiguous", key_disambig)
            outcome.error_data = {
                "candidates": self._candidates(
                    before_inventory, key_name.strip())}
            return
        if lock_disambig:
            self._fail(outcome, "ambiguous", lock_disambig)
            outcome.error_data = {
                "candidates": self._candidates(
                    before_room_contents, lock_name.strip())}
            return
        if key_item is None:
            self._fail(
                outcome, "not_found",
                f"You don't have a {key_name.strip()} to use.")
            return
        if lock_item is None:
            found = self._find_exit(room, lock_name)
            if found is None:
                self._fail(
                    outcome, "not_found",
                    f"There is no {lock_name.strip()} here to use a "
                    f"{key_name.strip()} on.")
                return
            direction, target, lock = found
            if lock is None or not lock.locked:
                self._fail(
                    outcome, "already_open",
                    f"The way {direction} is already open.")
                return
            if (not lock.key
                    or getattr(key_item, "item_id", None) != lock.key):
                self._fail(
                    outcome, "locked",
                    f"The {key_item.name} does not fit the lock on the way "
                    f"{direction}.")
                return
            opposite = OPPOSITE_EXITS.get(direction)
            shared = (
                target
                if opposite is not None
                and target.exit_locks.get(opposite) is lock
                else None
            )
            if self.store is not None:
                self._commit(
                    ChangeSet("use").set_exit(
                        room, direction, locked=False, target=shared,
                        key_item=key_item, label=lock.label))
            lock.locked = False
            await self.emit("exit_unlocked", {
                "direction": direction,
                "from": room.name,
                "to": target.name,
                "key": key_item.name.lower(),
            })
            door = lock.label or f"way {direction}"
            outcome.reply = (
                f"You turn the {key_item.name}; the {door} unlocks.")
            outcome.events.append(SemanticEvent(
                kind="exit.unlocked",
                data={"actor": actor.name, "room": room,
                      "direction": direction, "target": target,
                      "text_actor": outcome.reply,
                      "text_room": (f"{actor.name} unlocks the way "
                                    f"{direction}.")},
                scope="room",
            ))
            return
        if not getattr(lock_item, "locked", False):
            self._fail(
                outcome, "already_open",
                f"The {lock_item.name} is already open.")
            return
        if (getattr(lock_item, "required_key", None)
                and getattr(key_item, "item_id", None)
                == lock_item.required_key):
            if self.store is not None:
                self._commit(
                    ChangeSet("use").set(lock_item, "locked", False))
            lock_item.locked = False
            await self.emit("item_unlocked", {
                "item_name": lock_item.name.lower(),
                "location": room.name,
            })
            outcome.reply = (
                f"You turn the {key_item.name} and the {lock_item.name} "
                "clicks open.")
            outcome.events.append(SemanticEvent(
                kind="object.unlocked",
                data={"object": self._semantic_object(
                          lock_item, before_room_contents),
                      "by": actor.name,
                      "text_actor": outcome.reply,
                      "text_room": (f"{actor.name} unlocks the "
                                    f"{lock_item.name}.")},
                scope="room",
            ))
            return
        self._fail(
            outcome, "locked",
            f"The {key_item.name} does not fit the {lock_item.name}.")

    async def _exec_say(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None, raw: str) -> None:
        """`say <text>`: one line to the room, original casing preserved."""
        pieces = raw.split(maxsplit=1)
        spoken = pieces[1] if len(pieces) > 1 else ""
        if not spoken:
            self._fail(
                outcome, "unknown_command",
                "Hmm, I don't understand that. Type `help` for a list of "
                "commands.")
            return
        await self.emit("player_said", {
            "name": actor.name,
            "text": spoken,
            "location": actor.location.name if actor.location else None,
        })
        outcome.reply = f'You say, "{spoken}".'
        outcome.events.append(SemanticEvent(
            kind="player.said",
            data={"from": actor.name, "text_actor": outcome.reply,
                  "text_room": f'{actor.name} says, "{spoken}".'},
            scope="room",
        ))

    async def _exec_emote(self, outcome: CommandOutcome, actor: Player,
                          arg: str | None, raw: str) -> None:
        """`emote <action>`: a room-local third-person action (#75)."""
        if not arg:
            self._fail(
                outcome, "bad_args", "Emote what? Try `emote <action>`.")
            return
        action = raw.split(maxsplit=1)[1].strip()
        line = f"{actor.name} {action}"
        if not line.endswith((".", "!", "?")):
            line += "."
        await self.emit("player_emoted", {
            "name": actor.name,
            "text": action,
            "location": actor.location.name if actor.location else None,
        })
        outcome.reply = line
        outcome.events.append(SemanticEvent(
            kind="player.emoted",
            data={"from": actor.name, "text_actor": line, "text_room": line},
            scope="room",
        ))

    async def _exec_tell(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None, raw: str) -> None:
        """`tell <player> <text>`: one private line to an awake character."""
        pieces = raw.split(maxsplit=2)
        if len(pieces) < 3 or not pieces[2].strip():
            self._fail(
                outcome, "bad_args",
                "Tell whom what? Try `tell <player> <text>`.")
            return
        target_arg = pieces[1]
        message = pieces[2].strip()
        target = self.find_character(target_arg)
        if target is not None:
            if target.asleep:
                self._fail(
                    outcome, "not_awake",
                    f"{target.name} is asleep, turned to stone; a tell "
                    "cannot reach them.")
                return
            await self.emit("player_told", {
                "from": actor.name, "to": target.name, "text": message})
            outcome.reply = f'You tell {target.name}, "{message}".'
            outcome.events.append(SemanticEvent(
                kind="player.told",
                data={"from": actor.name, "to": target.name,
                      "text_actor": outcome.reply},
                scope="actor",
            ))
            outcome.events.append(SemanticEvent(
                kind="player.told",
                data={"from": actor.name, "to": target.name,
                      "player": target.name,
                      "text_actor": (f'{actor.name} tells you, '
                                     f'"{message}".')},
                scope="player", direct=False,
            ))
            return
        entry = self.character_entry(target_arg)
        if entry is not None:
            self._fail(
                outcome, "not_awake",
                f"{entry['name']} is asleep, turned to stone; a tell cannot "
                "reach them.")
            return
        self._fail(
            outcome, "not_found",
            f"There is no one named {target_arg} in the world.")

    async def _exec_talk(self, outcome: CommandOutcome, actor: Player,
                         arg: str | None) -> None:
        """`talk <npc>`: the authored greeting or the NPC's default line."""
        if not arg:
            self._fail(
                outcome, "bad_args", "Talk to whom? Try `talk <npc>`.")
            return
        room = actor.location
        npc, ambiguity = self.find_npc(room, arg)
        if ambiguity:
            self._fail(outcome, "ambiguous", ambiguity)
            outcome.error_data = {
                "candidates": self._candidates(room.contents, arg)
                if room is not None else []}
            return
        if npc is None:
            self._fail(
                outcome, "not_found", f"You see no {arg.strip()} here.")
            return
        line = npc.greeting or npc.default
        if not line:
            self._notice(outcome, f"{npc.name} has nothing to say.")
            return
        await self.fire_effects(npc, "on_talk", actor=actor.name, room=room)
        self._npc_say(outcome, npc, f'{npc.name} says, "{line}"', None)

    async def _exec_ask(self, outcome: CommandOutcome, actor: Player,
                        arg: str | None) -> None:
        """`ask <npc> about <topic>`: the topic line or the default fallback."""
        if not arg:
            self._fail(
                outcome, "bad_args",
                "Ask whom about what? Try `ask <npc> about <topic>`.")
            return
        npc_arg, separator, topic = arg.partition(" about ")
        if not separator:
            self._fail(
                outcome, "bad_args",
                "Ask whom about what? Try `ask <npc> about <topic>`.")
            return
        room = actor.location
        npc, ambiguity = self.find_npc(room, npc_arg)
        if ambiguity:
            self._fail(outcome, "ambiguous", ambiguity)
            outcome.error_data = {
                "candidates": self._candidates(room.contents, npc_arg)
                if room is not None else []}
            return
        if npc is None:
            self._fail(
                outcome, "not_found", f"You see no {npc_arg.strip()} here.")
            return
        key = topic.strip().lower()
        line = npc.topics.get(key) or npc.default
        if not line:
            self._notice(outcome, f"{npc.name} has nothing to say.")
            return
        await self.fire_effects(npc, "on_talk", actor=actor.name, room=room)
        topic_out = key if key in npc.topics else None
        self._npc_say(outcome, npc, f'{npc.name} says, "{line}"', topic_out)

    def _npc_say(self, outcome: CommandOutcome, npc: NPC, reply: str,
                 topic: str | None) -> None:
        """Record an NPC line as a room-wide `npc.say` event."""
        data: dict[str, Any] = {
            "npc": {
                "id": getattr(npc, "item_id", None) or slug(npc.name),
                "name": npc.name,
            },
            "text_actor": reply,
            "text_room": reply,
        }
        if topic is not None:
            data["topic"] = topic
        outcome.reply = reply
        outcome.events.append(SemanticEvent(
            kind="npc.say", data=data, scope="room"))

    async def _exec_light(self, outcome: CommandOutcome, actor: Player,
                          arg: str | None) -> None:
        """`light <source>`: ignite a carried light source."""
        if not arg:
            self._fail(
                outcome, "unknown_command",
                "Hmm, I don't understand that. Type `help` for a list of "
                "commands.")
            return
        held_item, disambig = self._disambiguate(actor.contents, arg)
        if disambig:
            self._fail(outcome, "ambiguous", disambig)
            outcome.error_data = {
                "candidates": self._candidates(actor.contents, arg)}
            return
        if held_item is None:
            self._fail(
                outcome, "not_found",
                f"You don't have a {arg.strip()} to light.")
            return
        if not getattr(held_item, "light_source", False):
            self._notice(outcome, f"You can't light the {held_item.name}.")
            return
        if getattr(held_item, "lit", False):
            self._notice(outcome, f"The {held_item.name} is already lit.")
            return
        slot = getattr(held_item, "equipped_slot", None)
        changes = ChangeSet("light")
        if slot not in LIGHT_SLOTS:
            slot, error = self._equip_target(
                actor, held_item, light_only=True)
            if error is not None:
                self._fail(
                    outcome, "no_slot" if error == "no_slot" else "bad_args",
                    f"You have no free hand, belt, or head for the "
                    f"{held_item.name}.")
                return
            changes.set(held_item, "slot", slot)
        changes.set(held_item, "lit", True)
        self._commit(changes)
        actor.equip(held_item, slot)
        held_item.lit = True
        await self.emit("light_ignited", {
            "item_name": held_item.name.lower()})
        outcome.reply = (
            f"You light the {held_item.name}. Its flame casts a warm glow.")
        obj = self._semantic_object(held_item, actor.contents)
        obj["lit"] = True
        outcome.events.append(SemanticEvent(
            kind="object.lit",
            data={"object": obj, "by": actor.name,
                  "text_actor": outcome.reply,
                  "text_room": f"{actor.name} lights the {held_item.name}."},
            scope="room",
        ))

    def _require_actor(self, actor: Player | str) -> Player:
        """Resolve an actor reference to a registered `Player`.

        The engine has one command path and requires an actor: a `Player` or a
        registered character name. There is no single-player fallback (#133).
        """
        if isinstance(actor, Player):
            return actor
        player = self.get_player(actor)
        if player is None:
            raise KeyError(f"no registered player named {actor!r}")
        return player

    async def process_command(self, command_text: str, actor: Player | str) -> str:
        """Return the reply string for one command from `actor`.

        A thin string seam over `execute` for the v1 server and the unit
        tests; the actor is required. The v2 adapter consumes `execute`
        directly and shapes its `CommandOutcome`.
        """
        player = self._require_actor(actor)
        return (await self.execute(player, command_text)).reply

    async def run(self) -> None:
        pass


if __name__ == "__main__":
    # Simple bootstrap for inspection
    eng = Engine()
    r1 = Room("Start", "A beginning.")
    r2 = Room("End", "The end.")
    r1.exits["next"] = r2
    eng.start_room = r1
    hero = eng.register_player("Hero")
    print(asyncio.run(eng.process_command("go next", hero)))
    print(asyncio.run(eng.process_command("look", hero)))

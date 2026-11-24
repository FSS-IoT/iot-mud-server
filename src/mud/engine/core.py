from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional
from src.mud.content import DEFAULT_CARRY_CAPACITY, NPC, Entity, Player, Room
from src.mud.content.effects import effect_entries, effect_payload
from src.mud.content.validator import container_cycles
from src.mud.protocol.v2 import CommandOutcome, SemanticEvent
from src.mud.storage.keys import object_key, room_key, slug
from src.mud.world.wander import NpcMove, NpcWanderer, npc_key

if TYPE_CHECKING:
    from src.mud.storage.store import WriteThroughStore

logger = logging.getLogger(__name__)

class Engine:
    def __init__(self, store: WriteThroughStore | None = None):
        """The authoritative brain of the MUD.

        `store` is the write-through seam (SPEC v2.1.0): a take, drop, or move
        commits there before the engine touches memory, so a failed write
        raises `StorageWriteError` with memory untouched. None keeps the engine
        purely in memory for the v1 server and the unit tests.
        """
        self._rooms: Dict[str, Room] = {}
        self._player_location: Optional[Room] = None
        self.player: Optional[Entity] = None
        # The effect log sink is a bus subscriber (#84): every triggered
        # effect is appended to `event_log` when a store is attached, whether
        # or not any client is allowed to see it.
        self._subscribers: Dict[str, List[Callable]] = {
            "effect": [self._record_effect],
        }
        # Multiplayer registry. Each named player owns its location; the engine
        # is authoritative over who is where. Legacy single-player accessors
        # (self.player / self._player_location) remain for backward compatibility.
        self._players: Dict[str, Player] = {}
        self.store: WriteThroughStore | None = store
        # Deterministic world motion (#82). The loader sets `rng_seed` from the
        # world file and builds the wanderer once every room and NPC exists;
        # `tick_world` advances it. `rng_seed` defaults to 0 for the legacy
        # JSON world and in-memory tests, which still get a reproducible path.
        self.rng_seed: int = 0
        self.wanderer: NpcWanderer | None = None

    def attach_store(self, store: WriteThroughStore | None) -> None:
        """Persist each mutation to `store` before applying it in memory."""
        self.store = store

    @property
    def rooms(self) -> Dict[str, Room]:
        """Public access to the world map."""
        return self._rooms

    @property
    def current_location(self) -> Optional[Room]:
        """Public access to the player's current room."""
        return self._player_location

    def get_room(self, name: str) -> Optional[Room]:
        """Return the room with this name, or None."""
        return self._rooms.get(name)

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
            self.wanderer = NpcWanderer(self._rooms, self.rng_seed)
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
        player.location = self._player_location
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

        for room in self._rooms.values():
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
            for item in list(player.contents):
                if not getattr(item, "dropped_on_sleep", False):
                    continue
                if self.store is not None:
                    self.store.player(name).drop(item, room=room)
                player.remove_content(item)
                room.add_content(item)
                dropped.append(item)
        if self.store is not None:
            self.store.player(name).sleep(room=room)
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
            self.store.player(name).wake()
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
        if self.store is not None:
            for row in self.store.roster():
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
        if player is None or player.location is None:
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
            self.store.player(name).move(room=destination)

        old_name = player.location.name
        player.location.remove_content(player)
        player.location = destination
        destination.add_content(player)

        await self.emit("player_moved", {
            "name": name, "from": old_name, "to": destination.name,
        })
        await self.fire_effects(
            destination, "on_enter", actor=name, room=destination)
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
    def _describe_movement(destination: Room, direction: str, old_location=None) -> str:
        """Render the standard movement prose for arriving at `destination`.

        The reply is the single source of truth for what the mover sees on arrival:
        travel prose, destination description, exits, and any items in the room.
        A separate `room_state` push on move is intentionally NOT used here because
        it would duplicate this reply (one with contents, one without).
        """
        old_name = old_location.name if old_location is not None else "the previous room"
        exits_list = Engine._exit_labels(destination)
        exit_str = f"Exits lead {', '.join(exits_list)}."

        response = (
            f"You travel towards {destination.name}.\n"
            f"(Prose: You leave the {old_name}).\n\n"
            f"{destination.description}\n"
            f"{exit_str}"
        )

        # Show items in the room the mover just entered. Items are physical objects
        # in the room, so list them from the destination's contents directly.
        items = [c for c in destination.contents if not isinstance(c, Player)]
        if items:
            listed = ", ".join(sorted(c.name for c in items))
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

    def _speaker_room(self, player_name: str | None):
        """The room whose NPCs a command acts in, for either engine path."""
        player = self._resolve_player(player_name)
        return player.location if player is not None else self._player_location

    async def _handle_talk(self, arg: str, player_name: str | None = None) -> str:
        """`talk <npc>`: the authored greeting, or the NPC's default line.

        Dialogue is scripted content, never generated. An unknown name is
        phrased for the adapter's `not_found`; an ambiguous one reuses the
        item resolver's wording (`ambiguous`). A spoken answer fires the NPC's
        `on_talk` effects (#84).
        """
        room = self._speaker_room(player_name)
        npc, ambiguity = self.find_npc(room, arg)
        if ambiguity:
            return ambiguity
        if npc is None:
            return f"You see no {arg.strip()} here."
        line = npc.greeting or npc.default
        if not line:
            return f"{npc.name} has nothing to say."
        player = self._resolve_player(player_name)
        await self.fire_effects(
            npc, "on_talk",
            actor=player.name if player is not None else None,
            room=room,
        )
        return f'{npc.name} says, "{line}"'

    async def _handle_ask(self, arg: str, player_name: str | None = None) -> str:
        """`ask <npc> about <topic>`: the topic line, or the default fallback.

        Topics are matched case-insensitively against the authored map. An
        unknown topic is not an error: the NPC answers with its `default`
        line, so a client never has to know the script in advance. A spoken
        answer, including the fallback, fires the NPC's `on_talk` effects
        (#84): the trigger vocabulary has no separate `on_ask`.
        """
        npc_arg, separator, topic = arg.partition(" about ")
        if not separator:
            return "Ask whom about what? Try `ask <npc> about <topic>`."
        room = self._speaker_room(player_name)
        npc, ambiguity = self.find_npc(room, npc_arg)
        if ambiguity:
            return ambiguity
        if npc is None:
            return f"You see no {npc_arg.strip()} here."
        line = npc.topics.get(topic.strip().lower()) or npc.default
        if not line:
            return f"{npc.name} has nothing to say."
        player = self._resolve_player(player_name)
        await self.fire_effects(
            npc, "on_talk",
            actor=player.name if player is not None else None,
            room=room,
        )
        return f'{npc.name} says, "{line}"'

    @staticmethod
    def _held_items(entity) -> List[str]:
        """Return the display names of the physical items `entity` carries.

        A player's `contents` hold only things it has picked up; rooms are handled
        by their own code path, so this is the inventory view over a player slot.
        Names are sorted for deterministic output.
        """
        if entity is None:
            return []
        return sorted(c.name for c in entity.contents)

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
        """Whether `room` is lit from the perspective of `player` (or legacy slot).

        A room is lit unless it is explicitly marked `dark`. A dark room is lit
        if it carries a light source, or if the observer carries a *lit* light
        source (a struck Torch, a lit lantern). The default is a LIT room: no
        explicit darkness means the lights are effectively on.
        """
        if room is None or (isinstance(room, Room) and not room.dark):
            return True
        if isinstance(room, Room) and room.light_source is not None:
            return True
        observer = player if player is not None else self.player
        return observer is not None and any(
            getattr(c, "light_source", False) and getattr(c, "lit", False)
            for c in observer.contents
        )

    def visible_room_state(self, player) -> Dict[str, Any]:
        """The view of the player's current room, ready to render.

        This is the single source of truth for what a player sees in their room
        — the room description, visible items, and visible occupants. It
        encodes the visibility rules (darkness) so the server terminal can
        render `room_state` without duplicating them: a dark room with no light
        reports its name and darkness but hides every item and occupant the
        player cannot see.
        """
        room = player.location if player is not None else self._player_location
        dark = isinstance(room, Room) and room.dark and not self._room_is_lit(player, room)
        if dark:
            return {
                "name": room.name if room is not None else "",
                "description": room.description if room is not None else "",
                "dark": True,
                "items": [],
                "occupants": [],
            }
        items = sorted(c.name for c in room.contents if not isinstance(c, Player))
        occupants = sorted(p.name for p in self._players.values()
                           if p.location is room and p is not player)
        return {
            "name": room.name if room is not None else "",
            "description": room.description if room is not None else "",
            "dark": False,
            "items": items,
            "occupants": occupants,
        }

    def _find_target(self, player, room, arg: str):
        """Resolve the entity named `arg` for the `examine` / `read` verbs.

        Returns a tuple `(item, player_target)`:
          * `item` is the held or room Entity matching `arg` (or None when no such
            physical object exists).
          * `player_target` is a named Player sharing the room (or None).

        Physical items are matched before people so `read`/`examine X` never
        accidentally describes a player. Players are excluded from the room-item
        scan because they live in `room.contents` too.
        """
        held = self._find_item(player.contents, arg) if player is not None else None
        if held is not None:
            return held, None

        room_items = [c for c in (room.contents if room is not None else [])
                      if not isinstance(c, Player)]
        room_item = self._find_item(room_items, arg)
        if room_item is not None:
            return room_item, None

        player_target = next((p for p in self._players.values()
                              if (room is None or p.location is room)
                              and p.name.lower() == arg), None)
        return None, player_target

    async def _handle_take(self, arg: str, player_name: Optional[str]) -> str:
        """Move an item named `arg` from the player's room into their inventory.

        Returns the reply string. Supports both the registered-player path (a
        socket-bound client passes a name) and the legacy single-player slot.
        With a store attached, the item's new owner commits before the
        in-memory move; a `StorageWriteError` propagates with nothing changed.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            # Multiplayer path: take from the room into the registered player.
            room = target_player.location
            if room is None:
                return "There is nowhere here to take anything from."
            target_item, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig
            if target_item:
                # Carry limit (#78): refuse before the write-through, so a
                # refused take changes neither SQLite nor memory.
                if not self.can_carry(target_player, target_item):
                    return (
                        f"You cannot carry the {target_item.name}; it is "
                        "too heavy.")
                # Write-through: the take is durable before memory moves.
                if self.store is not None:
                    self.store.player(target_player.name).take(
                        target_item, room=room)
                room.remove_content(target_item)
                target_player.add_content(target_item)
                await self.emit("item_taken", {"item_name": target_item.name.lower(), "owner": target_player.name})
                return f"You picked up the {target_item.name}."
            return f"There is no {arg} here."

        # Legacy single-player path.
        if not self.player:
            return "You do not exist!"
        if not self._player_location:
            return "There is nowhere here to take anything from."
        target_item, disambig = self._disambiguate(self._player_location.contents, arg)
        if disambig:
            return disambig
        if target_item:
            if not self.can_carry(self.player, target_item):
                return (
                    f"You cannot carry the {target_item.name}; it is "
                    "too heavy.")
            self._player_location.remove_content(target_item)
            self.player.add_content(target_item)
            await self.emit("item_taken", {"item_name": target_item.name.lower(), "owner": "player"})
            return f"You picked up the {target_item.name}."
        return f"There is no {arg} here."

    async def _handle_drop(self, arg: str, player_name: Optional[str]) -> str:
        """Drop an item named `arg` from the player's inventory into their room.

        Returns the reply string. Supports both the registered-player path and the
        legacy single-player slot. With a store attached, the item's new room
        commits before the in-memory move; a `StorageWriteError` propagates with
        nothing changed.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            # Multiplayer path: drop from the registered player into their room.
            room = target_player.location
            if room is None:
                return "There is nowhere here to drop anything."
            target_item, disambig = self._disambiguate(target_player.contents, arg)
            if disambig:
                return disambig
            if target_item:
                # Write-through: the drop is durable before memory moves.
                if self.store is not None:
                    self.store.player(target_player.name).drop(
                        target_item, room=room)
                target_player.remove_content(target_item)
                room.add_content(target_item)
                await self.emit("item_dropped", {"item_name": target_item.name.lower(), "location": room.name})
                return f"You dropped the {target_item.name}."
            return f"You aren't carrying any {arg}."

        # Legacy single-player path.
        if not self.player:
            return "You do not exist!"
        if not self._player_location:
            return "There is nowhere here to drop anything."
        target_item, disambig = self._disambiguate(self.player.contents, arg)
        if disambig:
            return disambig
        if target_item:
            self.player.remove_content(target_item)
            self._player_location.add_content(target_item)
            await self.emit("item_dropped", {"item_name": target_item.name.lower(), "location": self._player_location.name})
            return f"You dropped the {target_item.name}."
        return f"You aren't carrying any {arg}."

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

    async def _handle_put(self, arg: str, player_name: str | None) -> str:
        """Put an item inside a container: `put <item> in <container>`.

        The item is resolved from the player's inventory; the container from the
        player's room. A locked container refuses. Names are resolved by the same
        forgiving match as take/examine. Returns the reply string.
        """
        # Split "put <item> in <container>" on the delimiter, not on the
        # substring: `partition("in")` splits inside item names like "coin"
        # and "lantern". The last " in " is the one that separates the two.
        # "into" is accepted as the same delimiter.
        normalized = arg.replace(" into ", " in ")
        if " in " not in normalized:
            return "Put what where? Try `put <item> in <container>`."
        item_arg, _, container_arg = normalized.rpartition(" in ")
        item_arg = item_arg.strip()
        container_arg = container_arg.strip()
        if not item_arg or not container_arg:
            return "Put what where? Try `put <item> in <container>`."

        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to put anything."
            held, item_disambig = self._disambiguate(target_player.contents, item_arg)
            if item_disambig:
                return item_disambig
            container, cont_disambig = self._disambiguate(room.contents, container_arg)
            if cont_disambig:
                return cont_disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            target_player = self.player
            room = self._player_location
            held, item_disambig = self._disambiguate(self.player.contents, item_arg)
            if item_disambig:
                return item_disambig
            container, cont_disambig = self._disambiguate(room.contents, container_arg)
            if cont_disambig:
                return cont_disambig

        if held is None:
            return f"You aren't carrying any {item_arg}."
        if container is None:
            return f"There is no {container_arg} here to put it in."
        if getattr(container, "locked", False):
            return f"The {container.name} is locked. Find a key and `use <key> on {container.name.lower()}`."
        if self._containment_cycle(container, held):
            # Refuse before either collection changes: the item stays in the
            # pack and the container's contents are untouched (#79).
            return (
                f"You cannot put the {held.name} in the {container.name}; "
                "a container cannot contain itself, directly or transitively."
            )

        target_player.remove_content(held)
        container.add_content(held)
        await self.emit("item_put", {"item_name": held.name.lower(), "container": container.name.lower(), "location": room.name})
        return f"You put the {held.name} in the {container.name}."

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

    async def _handle_use(self, arg: str, player_name: str | None) -> str:
        """Open a lock with a key: `use <key> on <lock|exit>`.

        Names locate the objects by the same forgiving match as take/examine,
        but whether the lock opens is decided by identity, not name. The key is
        resolved from the player's inventory; the target is a room object or a
        room exit. A container's `required_key` and an exit's `ExitLock.key`
        name the key object's `item_id`, so a copy with the same name still
        does not fit. Returns the reply string.
        """
        if " on " not in arg:
            return "Use a key on something: `use <key> on <lock|exit>`."

        key_name, lock_name = arg.split(" on ", 1)

        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to use a key."
            key_item, key_disambig = self._disambiguate(target_player.contents, key_name)
            if key_disambig:
                return key_disambig
            lock_item, lock_disambig = self._disambiguate(room.contents, lock_name)
            if lock_disambig:
                return lock_disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            room = self._player_location
            key_item, key_disambig = self._disambiguate(self.player.contents, key_name)
            if key_disambig:
                return key_disambig
            lock_item, lock_disambig = self._disambiguate(room.contents, lock_name)
            if lock_disambig:
                return lock_disambig

        if key_item is None:
            return f"You don't have a {key_name.strip()} to use."
        if lock_item is None:
            # No room object matched; the target may be a locked exit (#77).
            found = self._find_exit(room, lock_name)
            if found is None:
                return (
                    f"There is no {lock_name.strip()} here to use a "
                    f"{key_name.strip()} on."
                )
            direction, target, lock = found
            if lock is None or not lock.locked:
                return f"The way {direction} is already open."
            # Identity, not name: the door checks for its specific key.
            if not lock.key or getattr(key_item, "item_id", None) != lock.key:
                return (
                    f"The {key_item.name} does not fit the lock on the way "
                    f"{direction}."
                )
            lock.locked = False
            await self.emit("exit_unlocked", {
                "direction": direction,
                "from": room.name,
                "to": target.name,
                "key": key_item.name.lower(),
            })
            door = lock.label or f"way {direction}"
            return f"You turn the {key_item.name}; the {door} unlocks."

        if not getattr(lock_item, "locked", False):
            return f"The {lock_item.name} is already open."

        # Identity, not name: the lock checks for its specific key.
        if getattr(lock_item, "required_key", None) and getattr(key_item, "item_id", None) == lock_item.required_key:
            lock_item.locked = False
            await self.emit("item_unlocked", {"item_name": lock_item.name.lower(), "location": room.name})
            return f"You turn the {key_item.name} and the {lock_item.name} clicks open."

        return f"The {key_item.name} does not fit the {lock_item.name}."

    async def _handle_open(self, arg: str, player_name: str | None) -> str:
        """Open a container: `open <chest>`.

        A locked container refuses. An already-open one refuses with
        `already_open`. Opening sets the explicit `open` flag (#76) and
        reports what is inside; it does not move anything. `get <item> from
        <container>` pulls one item out, and `close` puts the lid back down.
        An empty container reports empty. Names are resolved by the same
        forgiving match as take/examine. Returns the reply string.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to open anything."
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            room = self._player_location
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig

        if container is None:
            return f"There is no {arg} here to open."
        if getattr(container, "locked", False):
            return f"The {container.name} is locked. Find a key and `use <key> on {container.name.lower()}`."

        # A plain item has no lid. It keeps the v2.0 empty-container reply and
        # does not acquire an open state it can never close.
        if container.is_container:
            if container.open:
                return f"The {container.name} is already open."
            container.open = True
            await self.fire_effects(
                container, "on_open",
                actor=getattr(target_player, "name", None),
                room=room,
            )

        loot = list(container.contents)
        if not loot:
            return f"The {container.name} is empty."

        await self.emit("chest_opened", {"item_name": container.name.lower(), "location": room.name})

        listed = ", ".join(sorted(c.name for c in loot))
        return f"You open the {container.name}. Inside you see: {listed}."

    async def _handle_close(self, arg: str, player_name: str | None) -> str:
        """Close an open container: `close <chest>`.

        The lid state is explicit (`Entity.open`, #76). A container that is
        already closed refuses with `already_closed`; a plain item is not
        something to close. Contents stay inside: closing changes only the
        lid, never what the container holds. Returns the reply string.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to close anything."
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            room = self._player_location
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig

        if container is None:
            return f"There is no {arg} here to close."
        if not container.is_container:
            return f"The {container.name} is not something you can close."
        if not container.open:
            return f"The {container.name} is already closed."

        container.open = False
        await self.emit("container_closed", {
            "item_name": container.name.lower(), "location": room.name,
        })
        return f"You close the {container.name}."

    async def _handle_get_from(self, arg: str, player_name: str | None) -> str:
        """Take one item out of an open container: `get <item> from <chest>`.

        The container is resolved from the player's room, the item from the
        container's contents. A closed container refuses with `container_closed`
        before the item is resolved; an item that is not inside is
        `not_here`; a fixed (`portable: false`) object cannot be lifted. The
        move is in memory only: the write-through boundary for containers is
        #63/#71, so this verb does not invent a second persistence rule.
        Returns the reply string.
        """
        # `partition("from")` would split names like "brass"; the last " from "
        # separates the two arguments, mirroring `put ... in ...`.
        if " from " not in arg:
            return "Get what from what? Try `get <item> from <container>`."
        item_arg, _, container_arg = arg.rpartition(" from ")
        item_arg = item_arg.strip()
        container_arg = container_arg.strip()
        if not item_arg or not container_arg:
            return "Get what from what? Try `get <item> from <container>`."

        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to get anything from."
            container, cont_disambig = self._disambiguate(
                room.contents, container_arg)
            if cont_disambig:
                return cont_disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            target_player = self.player
            room = self._player_location
            container, cont_disambig = self._disambiguate(
                room.contents, container_arg)
            if cont_disambig:
                return cont_disambig

        if container is None:
            return f"There is no {container_arg} here."
        if not container.is_container:
            return f"The {container.name} is not a container."
        if not container.open:
            return f"The {container.name} is closed. Open it first."

        item, item_disambig = self._disambiguate(container.contents, item_arg)
        if item_disambig:
            return item_disambig
        if item is None:
            return f"The {item_arg} is not in the {container.name}."
        if not getattr(item, "portable", True):
            return f"The {item.name} is fixed in place and cannot be put in your pack."
        if not self.can_carry(target_player, item):
            return f"You cannot carry the {item.name}; it is too heavy."

        container.remove_content(item)
        target_player.add_content(item)
        await self.emit("item_got", {
            "item_name": item.name.lower(),
            "container": container.name.lower(),
            "location": room.name,
        })
        return f"You get the {item.name} from the {container.name}."

    async def _handle_look_in(self, arg: str, player_name: str | None) -> str:
        """Look inside a container: `look in <chest>` / `look inside <chest>`.

        A locked container refuses. An open container with nothing inside
        reports empty. Otherwise every item it holds is listed. Names are
        resolved by the same forgiving match as take/examine. Returns the
        reply string.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            room = target_player.location
            if room is None:
                return "There is nowhere here to look inside."
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig
        else:
            if not self.player or not self._player_location:
                return "You do not exist!"
            room = self._player_location
            container, disambig = self._disambiguate(room.contents, arg)
            if disambig:
                return disambig

        if container is None:
            return f"There is no {arg} here to look inside."
        if getattr(container, "locked", False):
            return f"The {container.name} is locked. Find a key and `use <key> on {container.name.lower()}`."

        loot = list(container.contents)
        if not loot:
            return f"The {container.name} is empty."

        listed = ", ".join(sorted(c.name for c in loot))
        return f"You see inside the {container.name}: {listed}."

    async def _handle_light(self, arg: str, player_name: str | None) -> str:
        """Ignite a carried light source: `light <torch/lantern>`.

        The item is resolved from the player's inventory by the same forgiving
        match as take/examine and must be a `light_source`. Lighting it sets its
        `lit` state so it sheds light wherever the player stands; an already-lit
        one is reported rather than re-struck. Returns the reply string.
        """
        target_player = self._resolve_player(player_name)
        if target_player is not None:
            held_item, disambig = self._disambiguate(
                target_player.contents, arg)
            if disambig:
                return disambig
        else:
            if not self.player:
                return "You do not exist!"
            held_item, disambig = self._disambiguate(
                self.player.contents, arg)
            if disambig:
                return disambig

        if held_item is None:
            return f"You don't have a {arg.strip()} to light."
        if not getattr(held_item, "light_source", False):
            return f"You can't light the {held_item.name}."
        if getattr(held_item, "lit", False):
            return f"The {held_item.name} is already lit."

        held_item.lit = True
        await self.emit("light_ignited", {"item_name": held_item.name.lower()})
        return f"You light the {held_item.name}. Its flame casts a warm glow."

    async def _handle_tell(
        self, raw_text: str, player_name: str | None = None,
    ) -> str:
        """One private line to an awake character anywhere in the world.

        `tell <player> <text>` resolves the target server-wide, by display
        name and ignoring case. A live target must be awake; a durable roster
        row with no live session is asleep/stone by definition (#72). Both
        failures are typed by the adapter (`not_awake` and `not_found`), and
        nothing is stored: this is not offline mail, so a tell to a sleeping
        character is refused rather than replayed on wake.
        """
        pieces = raw_text.split(maxsplit=2)
        if len(pieces) < 3 or not pieces[2].strip():
            return "Tell whom what? Try `tell <player> <text>`."
        target_arg = pieces[1]
        message = pieces[2].strip()

        target = self.find_character(target_arg)
        if target is not None:
            if target.asleep:
                return (
                    f"{target.name} is asleep, turned to stone; a tell cannot "
                    "reach them."
                )
            sender = player_name or (
                self.player.name if self.player is not None else "someone")
            await self.emit("player_told", {
                "from": sender, "to": target.name, "text": message,
            })
            return f'You tell {target.name}, "{message}".'

        entry = self.character_entry(target_arg)
        if entry is not None:
            return (
                f"{entry['name']} is asleep, turned to stone; a tell cannot "
                "reach them."
            )
        return f"There is no one named {target_arg} in the world."

    def _resolve_player(self, player_name):
        """Return the registered player to act as, falling back to the legacy slot."""
        if player_name:
            return self.get_player(player_name)
        return None

    # --- Effects (#84) -------------------------------------------------------

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

    # The one alias table. `process_command` keeps its own copy until the
    # adapter reshape folds that path in; both must stay identical.
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

        The single grammar owner (#130): the engine parses the command, and the
        v2 adapter will stop parsing. A blank line is ``"empty"``; the
        ``look at`` / ``look in`` rewrites match `process_command`.
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
            resolved, _, resolved_arg = Engine._ALIASES[verb].partition(" ")
            return resolved, resolved_arg or None, raw
        return verb, arg, raw

    @staticmethod
    def _semantic_object(entity: Entity,
                         collection: list[Entity] | None = None) -> dict[str, Any]:
        """A stable object descriptor for a semantic event.

        Mirrors the adapter's wire `_object_data` (id, kind, name) so the two
        agree on instance ids, including the `<kind>-<n>` suffix that tells two
        of the same prototype apart in one collection.
        """
        base = getattr(entity, "item_id", None) or slug(entity.name)
        ident = getattr(entity, "instance_id", None)
        if not ident:
            siblings = [
                candidate for candidate in (collection or [])
                if (getattr(candidate, "item_id", None)
                    or slug(candidate.name)) == base
            ]
            if len(siblings) > 1:
                try:
                    index = siblings.index(entity) + 1
                except ValueError:
                    index = 1
                ident = f"{base}-{index}"
            else:
                ident = base
            entity.instance_id = ident
        return {"id": ident, "kind": base, "name": entity.name}

    async def execute(self, actor: Player, text: str) -> CommandOutcome:
        """Run one command for `actor` and return a typed outcome (#130).

        Phase 1 is additive: `process_command` still drives the v1 server and
        the v2 adapter. The verbs ported so far build their semantic events
        directly; anything else falls back to `process_command` so the reply
        stays faithful while the port continues.
        """
        verb, arg, _raw = self.parse_command(text)
        outcome = CommandOutcome()
        if verb == "empty":
            outcome.reply = "Silence falls over you."
            return outcome
        if verb in ("exit", "quit"):
            await self.emit("player_quit", {"reason": "user_requested"})
            outcome.reply = "Goodbye!"
            return outcome

        handled = True
        if verb == "take":
            await self._exec_take(outcome, actor, arg)
        elif verb == "drop":
            await self._exec_drop(outcome, actor, arg)
        elif verb == "open":
            await self._exec_open(outcome, actor, arg)
        elif verb == "go":
            await self._exec_go(outcome, actor, arg)
        else:
            handled = False

        if handled:
            await self.emit("command_processed", {
                "raw": text, "clean": text.strip().lower(),
                "reply": outcome.reply,
            })
        else:
            # Not yet ported: keep the outcome truthful via the string engine.
            outcome.reply = await self.process_command(text, actor.name)
        return outcome

    @staticmethod
    def _fail(outcome: CommandOutcome, code: str, reply: str) -> None:
        """Record a typed failure on `outcome`."""
        outcome.status = "error"
        outcome.error_code = code
        outcome.reply = reply

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
            return
        if item is None:
            self._fail(outcome, "not_found", f"There is no {arg} here.")
            return
        if not self.can_carry(actor, item):
            self._fail(
                outcome, "too_heavy",
                f"You cannot carry the {item.name}; it is too heavy.")
            return
        if self.store is not None:
            self.store.player(actor.name).take(item, room=room)
        room.remove_content(item)
        actor.add_content(item)
        await self.emit("item_taken", {
            "item_name": item.name.lower(), "owner": actor.name})
        outcome.reply = f"You picked up the {item.name}."
        outcome.events.append(SemanticEvent(
            kind="object.taken",
            data={
                "object": self._semantic_object(item, before_room_contents),
                "by": actor.name,
                "text_actor": outcome.reply,
                "text_room": f"{actor.name} picks up the {item.name}.",
            },
            scope="room",
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
            return
        if item is None:
            self._fail(
                outcome, "not_found", f"You aren't carrying any {arg}.")
            return
        if self.store is not None:
            self.store.player(actor.name).drop(item, room=room)
        actor.remove_content(item)
        room.add_content(item)
        await self.emit("item_dropped", {
            "item_name": item.name.lower(), "location": room.name})
        outcome.reply = f"You dropped the {item.name}."
        outcome.events.append(SemanticEvent(
            kind="object.dropped",
            data={
                "object": self._semantic_object(item, room.contents),
                "by": actor.name,
                "text_actor": outcome.reply,
                "text_room": f"{actor.name} drops the {item.name}.",
            },
            scope="room",
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
            return

        old_room = room
        moved, result = await self.move_player(actor.name, direction)
        if not moved:
            self._fail(outcome, "no_exit", result)
            return

        outcome.reply = self._describe_movement(
            actor.location, direction, old_location=old_room)
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

    async def process_command(self, command_text: str, player_name: Optional[str] = None) -> str:
        """Translates player input into state mutation or narrative.

        `player_name` is the registered name whose perspective to render from.
        It is optional so existing single-player callers keep working unchanged;
        only the multiplayer path (which knows each socket's registered name)
        passes one. When given, room descriptions also list other occupants.
        """
        raw_text = command_text.strip()
        text = raw_text.lower()
        if not text:
            return "Silence falls over you."

        parts = text.split(maxsplit=1)
        verb = parts[0]
        arg = parts[1] if len(parts) > 1 else None
        
        # --- 1. SYSTEM COMMANDS (Highest Priority) ---
        if verb == "exit":
            response = "Goodbye!"
            matched = True
            await self.emit("player_quit", {"reason": "user_requested"})
            return response

        # --- 2. ALIAS RESOLUTION & NORMALIZATION ---
        aliases = {
            "get": "take", "pick": "take", "grab": "take",
            "l": "look", 
            "n": "go north", "north": "go north",
            "s": "go south", "south": "go south", 
            "e": "go east",  "east": "go east",
            "w": "go west",  "west": "go west", 
            "u": "go up",    "up": "go up", 
            "d": "go down",  "down": "go down",
            "exits": "show_exits",
            # Short forms for the everyday commands. Kept single-word so they map
            # straight to a verb below (`i` -> `inventory`, not a direction).
            "i": "inventory", "inv": "inventory",
            "h": "help",
            "exam": "examine",
            # `me` is the design-doc alias for a room-local action (#75).
            "me": "emote",
        }
        
        # `look at X` focuses on something; treat it as `examine X`.
        # `look in X` / `look inside X` peer into a container.
        if verb.lower() == "look" and arg is not None:
            first = arg.split(maxsplit=1)[0]
            if first == "at":
                parts = arg.split(maxsplit=1)
                resolved_verb = "examine"
                resolved_arg = parts[1].strip() if len(parts) > 1 else None
            elif first in ("in", "inside"):
                resolved_verb = "look_in"
                resolved_arg = arg.split(maxsplit=1)[1].strip() if len(arg.split(maxsplit=1)) > 1 else ""
            else:
                resolved_verb = verb
                resolved_arg = arg
        else:
            resolved_verb = verb
            resolved_arg = arg

        input_str = f"{verb} {arg}".strip().lower() if arg else verb.lower()
        if verb == "get" and arg and " from " in arg:
            # `get <item> from <container>` is the v2.1 container verb (#76).
            # A bare `get <item>` stays the v2.0 alias for `take` below.
            resolved_verb = "get"
            resolved_arg = arg
        elif input_str in aliases:
            alias_target = aliases[input_str]
            if " " in alias_target:
                v_part, a_part = alias_target.split(" ", 1)
                resolved_verb = v_part
                resolved_arg = a_part
            else:
                resolved_verb = alias_target
        elif verb.lower() in aliases:
            resolved_verb = aliases[verb.lower()]

        response = ""
        matched = False

        # --- 3. GAMEPLAY COMMAND LOGIC ---
        
        # VIEWING THE WORLD
        if resolved_verb == "look":
            target_player = self.get_player(player_name) if player_name else None
            room = target_player.location if target_player is not None else self._player_location
            if room:
                if not self._room_is_lit(target_player, room):
                    response = "It is too dark to see anything."
                    matched = True
                else:
                    response = room.description
                    # Items are everything in the room that is not a player; players
                    # get their own line below. This makes items visible without
                    # guessing the exact take/drop command.
                    items = [c for c in room.contents if not isinstance(c, Player)]
                    if items:
                        listed = ", ".join(sorted(c.name for c in items))
                        response += f"\n\nHere you see: {listed}."
                    occupants = [p for p in self._players.values()
                                 if p.location is room and p.name != player_name]
                    if room.npcs:
                        listed = ", ".join(
                            npc.name for npc in sorted(
                                room.npcs, key=lambda npc: npc.name.lower()))
                        response += f"\n\nAlso here: {listed}."
                    if occupants:
                        listed = ", ".join(
                            p.name for p in sorted(occupants,
                                                   key=lambda p: p.name.lower()))
                        response += f"\n\nOthers in the room: {listed}."
                    matched = True
            else:
                response = "You feel lost in a void."
                matched = True

        elif resolved_verb == "look_in" and resolved_arg:
            response = await self._handle_look_in(resolved_arg, player_name)
            matched = True

        elif resolved_verb == "show_exits":
            acting_player = self.get_player(player_name) if player_name else None
            room = (acting_player.location if acting_player is not None
                    else self._player_location)
            if not room:
                response = "You have no context for exits here."
                matched = True
            else:
                exits_dict = room.exits
                if not exits_dict:
                    response = "There are no exits here."
                else:
                    names = self._exit_labels(room)
                    response = f"Available exits: {', '.join(names)}."
                matched = True

        # MOVEMENT
        elif resolved_verb == "go" and resolved_arg:
            direction = resolved_arg.strip()
            registered_player = self.get_player(player_name) if player_name else None

            # Multiplayer path: move the registered, named player. This is what a
            # socket-bound client drives; it keeps that player's location in sync
            # with the world so `look` and broadcasts reflect real movement.
            if registered_player is not None:
                old_location = registered_player.location
                moved, result = await self.move_player(player_name, direction)
                if moved:
                    response = self._describe_movement(
                        registered_player.location, direction, old_location=old_location,
                    )
                    matched = True
                else:
                    response = result  # refusal string, e.g. "You cannot go south."
                    matched = True

            # Legacy single-player path (no player_name): unchanged behavior.
            elif not self._player_location:
                response = "You have no presence here to move."
                matched = True
            elif self._player_location.exit_is_locked(direction):
                response = self._locked_exit_reply(direction)
                matched = True
                await self.emit("exit_locked", {
                    "direction": direction,
                    "location": self._player_location.name,
                })
            else:
                destination = self._player_location.exits.get(direction)
                if isinstance(destination, Room):
                    prev_location = self._player_location
                    self._player_location = destination

                    response = self._describe_movement(
                        destination, direction, old_location=prev_location,
                    )
                    matched = True
                    # Signal World Change
                    await self.emit("room_change", {"from": prev_location.name, "to": destination.name, "desc": destination.description})
                    await self.fire_effects(
                        destination, "on_enter",
                        actor=(
                            self.player.name
                            if self.player is not None else None
                        ),
                        room=destination,
                    )
                else:
                    response = f"You cannot go {direction}."
                    matched = True

        # INTERACTION (Items)
        elif resolved_verb == "take":
            if not resolved_arg:
                response = "Take what? Try `take <item>`."
                matched = True
            else:
                response = await self._handle_take(resolved_arg, player_name)
                matched = True

        elif resolved_verb == "drop":
            if not resolved_arg:
                response = "Drop what? Try `drop <item>`."
                matched = True
            else:
                response = await self._handle_drop(resolved_arg, player_name)
                matched = True

        elif resolved_verb == "put":
            if not resolved_arg:
                response = "Put what where? Try `put <item> in <container>`."
            else:
                response = await self._handle_put(resolved_arg, player_name)
            matched = True

        # USE — open a lock with a key: `use <key> on <lock>`. Names locate the
        # objects; identity decides whether the lock opens.
        elif resolved_verb == "use":
            if not resolved_arg:
                response = "Use what on what? Try `use <key> on <lock>`."
                matched = True
            else:
                response = await self._handle_use(resolved_arg, player_name)
                matched = True

        # OPEN — open a container: `open <chest>`. A locked one refuses; an
        # open, empty one reports empty; otherwise it lists the contents and
        # keeps them inside so `get <item> from <chest>` can pull one out.
        elif resolved_verb == "open":
            if not resolved_arg:
                response = "Open what? Try `open <chest>`."
                matched = True
            else:
                response = await self._handle_open(resolved_arg, player_name)
                matched = True

        # CLOSE — put the lid back: `close <chest>`. Only an open container
        # closes; its contents stay inside.
        elif resolved_verb == "close":
            if not resolved_arg:
                response = "Close what? Try `close <chest>`."
                matched = True
            else:
                response = await self._handle_close(resolved_arg, player_name)
                matched = True

        # GET FROM — take one item out of an open container:
        # `get <item> from <chest>`. The item moves into the actor's inventory.
        elif resolved_verb == "get":
            if not resolved_arg:
                response = "Get what from what? Try `get <item> from <container>`."
                matched = True
            else:
                response = await self._handle_get_from(resolved_arg, player_name)
                matched = True

        # EXAMINE / LOOK AT — hold first, then room contents (not just players).
        elif resolved_verb == "examine" and resolved_arg:
            # Focus on a specific entity the player can hold or see. `examine X`
            # and `look at X` both arrive here with verb "examine". Prefer an item
            # the examiner holds; otherwise describe a physical item on the floor,
            # then a named player in the room (whose presence does not depend on
            # the examiner being registered).
            player = self._resolve_player(player_name)
            room = player.location if player is not None else self._player_location
            held, held_disambig = (
                self._disambiguate(player.contents, resolved_arg)
                if player is not None else (None, None)
            )
            if not self._room_is_lit(player, room):
                response = "It is too dark to see anything."
                matched = True
            elif held_disambig:
                response = held_disambig
                matched = True
            elif held is not None:
                response = f"It is {held.description}."
                matched = True
            else:
                room_items = [c for c in (room.contents if room is not None else [])
                              if not isinstance(c, Player)]
                disambig_item, disambig = self._disambiguate(room_items, resolved_arg)
                if disambig:
                    response = disambig
                elif disambig_item is not None:
                    response = f"It is {disambig_item.description}."
                else:
                    target_player = next((p for p in self._players.values()
                                          if (room is None or p.location is room)
                                          and p.name.lower() == resolved_arg), None)
                    if target_player is not None:
                        response = f"{target_player.name} is here, exploring the world."
                    else:
                        response = f"You see no {resolved_arg} here."
                matched = True

        # READ — like examine, but only readable objects (signs, books). Plain
        # items are not something to read, so `read <item>` is refused rather
        # than describing it. Shares the hold/floor/player resolution above.
        elif resolved_verb == "read" and resolved_arg:
            player = self._resolve_player(player_name)
            room = player.location if player is not None else self._player_location
            held, held_disambig = (
                self._disambiguate(player.contents, resolved_arg)
                if player is not None else (None, None)
            )
            if not self._room_is_lit(player, room):
                response = "It is too dark to see anything."
                matched = True
            elif held_disambig:
                response = held_disambig
                matched = True
            elif held is not None and held.readable:
                response = f"It is {held.description}."
                matched = True
            else:
                room_items = [c for c in (room.contents if room is not None else [])
                              if not isinstance(c, Player)]
                disambig_item, disambig = self._disambiguate(room_items, resolved_arg)
                if disambig:
                    response = disambig
                elif disambig_item is not None and disambig_item.readable:
                    response = f"It is {disambig_item.description}."
                else:
                    target_player = next((p for p in self._players.values()
                                          if (room is None or p.location is room)
                                          and p.name.lower() == resolved_arg), None)
                    if target_player is not None:
                        response = f"You can't read {target_player.name}."
                    else:
                        response = "There is nothing readable here."
                matched = True

        # NPC DIALOGUE — scripted replies authored in the world YAML (#81).
        # No LLM or generated text is involved.
        elif resolved_verb == "talk":
            if not resolved_arg:
                response = "Talk to whom? Try `talk <npc>`."
            else:
                response = await self._handle_talk(resolved_arg, player_name)
            matched = True

        elif resolved_verb == "ask":
            if not resolved_arg:
                response = "Ask whom about what? Try `ask <npc> about <topic>`."
            else:
                response = await self._handle_ask(resolved_arg, player_name)
            matched = True

        # LIGHT — ignite a carried light source: `light <torch/lantern>`.
        elif resolved_verb == "light" and resolved_arg:
            response = await self._handle_light(resolved_arg, player_name)
            matched = True

        # INVENTORY / HELP (UTILITY)
        elif resolved_verb == "inventory":
            held = self._held_items(self._resolve_player(player_name))
            if held:
                response = f"You are carrying: {', '.join(held)}."
            else:
                response = "You are not carrying anything."
            matched = True

        # WHO — the server-wide roster, including characters asleep in stone
        # (#72). This is the minimal v2.1.1 shape; #75 owns the final social
        # verbs and may refine the presentation.
        elif resolved_verb == "who":
            roster = self.who_entries()
            if not roster:
                response = "There is no one in the world."
            else:
                listed = []
                for entry in roster:
                    state = (
                        "asleep, turned to stone"
                        if entry["asleep"] else "awake"
                    )
                    where = entry["room_id"] or "nowhere"
                    listed.append(f"{entry['name']} ({where}, {state})")
                response = "Characters in the world: " + "; ".join(listed) + "."
            matched = True

        elif resolved_verb == "help":
            response = (
                "Commands: look (l), go <direction>, exits, take <item>, get <item> from <container>, drop <item>, put <item> in <container>, "
                "open <container>, close <container>, use <key> on <container|exit>, "
                "examine <thing> (exam), read <thing> (signs, books), look in <chest>, light <torch/lantern>, inventory (i), who, "
                "say <text>, emote <action> (me), tell <player> <text>, "
                "talk <npc>, ask <npc> about <topic>, hello. Try `look` first."
            )
            matched = True

        # SOCIAL / UTILITY
        elif resolved_verb == "say" and resolved_arg:
            # Echo the player's own words with their original casing, then emit a
            # `player_said` event so the server can broadcast the message to
            # everyone else standing in the room (multiplayer chat).
            spoken = raw_text.split(maxsplit=1)[1]
            response = f'You say, "{spoken}".'
            acting_player = self.get_player(player_name) if player_name else None
            location = (acting_player.location if acting_player is not None
                        else self._player_location)
            await self.emit("player_said", {
                "name": player_name,
                "text": spoken,
                "location": location.name if location else None,
            })
            matched = True
        elif resolved_verb == "emote":
            # A room-local third-person action (#75). The actor and everyone in
            # the room see the same rendered line; `self` on the wire says who
            # did it, so the text does not need a second-person rewrite. The
            # adapter routes it to the room only, exactly like `say`.
            if not resolved_arg:
                response = "Emote what? Try `emote <action>`."
                matched = True
            else:
                action = raw_text.split(maxsplit=1)[1].strip()
                acting_player = self.get_player(player_name) if player_name else None
                speaker = (
                    acting_player.name if acting_player is not None
                    else self.player.name if self.player is not None
                    else "You"
                )
                line = f"{speaker} {action}"
                if not line.endswith((".", "!", "?")):
                    line += "."
                response = line
                location = (acting_player.location if acting_player is not None
                            else self._player_location)
                await self.emit("player_emoted", {
                    "name": player_name,
                    "text": action,
                    "location": location.name if location else None,
                })
                matched = True
        elif resolved_verb == "tell":
            # Private, server-wide, awake target only (#75). The reply carries
            # the sender's presentation; the adapter adds the target's copy.
            response = await self._handle_tell(raw_text, player_name)
            matched = True
        elif resolved_verb == "hello":
            response = "The world listens..."
            matched = True

        # --- 4. FALLBACK PATH ---
        if not matched:
            response = "Hmm, I don't understand that. Type `help` for a list of commands."

        await self.emit("command_processed", {"raw": command_text, "clean": text, "reply": response})
        return response

    async def run(self) -> None:
        pass

if __name__ == "__main__":
    # Simple bootstrap for inspection
    eng = Engine()
    r1 = Room("Start", "A beginning.")
    r2 = Room("End", "The end.")
    r1.exits["next"] = r2
    eng._player_location = r1
    print(asyncio.run(eng.process_command("go next")))
    print(asyncio.run(eng.process_command("look")))

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from src.mud.content import Entity, Room, Player

if TYPE_CHECKING:
    from src.mud.storage.store import WriteThroughStore

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
        self._subscribers: Dict[str, List[Callable]] = {}
        # Multiplayer registry. Each named player owns its location; the engine
        # is authoritative over who is where. Legacy single-player accessors
        # (self.player / self._player_location) remain for backward compatibility.
        self._players: Dict[str, Player] = {}
        self.store: WriteThroughStore | None = store

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
        return True, destination.name

    @staticmethod
    def _describe_movement(destination: Room, direction: str, old_location=None) -> str:
        """Render the standard movement prose for arriving at `destination`.

        The reply is the single source of truth for what the mover sees on arrival:
        travel prose, destination description, exits, and any items in the room.
        A separate `room_state` push on move is intentionally NOT used here because
        it would duplicate this reply (one with contents, one without).
        """
        old_name = old_location.name if old_location is not None else "the previous room"
        exits_list = [e.capitalize() for e in destination.exits.keys()]
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
    def _find_item(collection, arg: str):
        """Find an item in `collection` by a forgiving name match.

        Matches when the typed argument is a substring of the item's display name
        (case-insensitive), so both "gold coin" and "coin" match an item named
        "Gold Coin". Leading articles ("the", "a", "an") are stripped first so
        natural phrasings like "the gold coin" resolve to "gold coin".
        """
        arg = re.sub(r"^(the|a|an)\s+", "", arg.strip().lower())
        for candidate in collection:
            if candidate.name.lower() == arg or arg in candidate.name.lower():
                return candidate
        return None

    @staticmethod
    def _disambiguate(collection, arg: str):
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
        arg = arg.strip()
        tokens = arg.split()
        # A trailing integer is a 1-based index into the sorted matches, e.g.
        # "gold key 1". Everything before it is the base name.
        index = None
        if tokens and tokens[-1].isdigit():
            index = int(tokens[-1])
            tokens = tokens[:-1]
        name_tokens = tokens
        key = re.sub(r"^(the|a|an)\s+", "", " ".join(name_tokens).strip().lower())
        # A missing name (e.g. `take 1`) must not match every item, and a
        # player is never an item even though players live in room.contents.
        if not key:
            return None, None
        item_candidates = [c for c in collection if not isinstance(c, Player)]
        matches = sorted(
            (c for c in item_candidates
             if c.name.lower() == key or key in c.name.lower()),
            key=lambda c: c.name.lower(),
        )
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

        target_player.remove_content(held)
        container.add_content(held)
        await self.emit("item_put", {"item_name": held.name.lower(), "container": container.name.lower(), "location": room.name})
        return f"You put the {held.name} in the {container.name}."

    async def _handle_use(self, arg: str, player_name: str | None) -> str:
        """Open a lock with a key: `use <key> on <lock>`.

        Names locate the objects by the same forgiving match as take/examine,
        but whether the lock opens is decided by identity, not name. The key is
        resolved from the player's inventory, the lock from the player's room.
        The key's `item_id` must equal the lock's `required_key`; otherwise the
        key simply does not fit. Returns the reply string.
        """
        if " on " not in arg:
            return "Use a key on something: `use <key> on <lock>`."

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
            return f"There is no {lock_name.strip()} here to use a {key_name.strip()} on."
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

        A locked container refuses. An open container with nothing inside
        reports empty. Otherwise every item it holds is spilled into the
        player's room so a plain `take <loot>` can then pick it up, and a
        `chest_opened` event fires. Names are resolved by the same forgiving
        match as take/examine. Returns the reply string.
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

        loot = list(container.contents)
        if not loot:
            return f"The {container.name} is empty."

        for item in loot:
            container.remove_content(item)
            room.add_content(item)
        await self.emit("chest_opened", {"item_name": container.name.lower(), "location": room.name})

        listed = ", ".join(sorted(c.name for c in loot))
        return f"You open the {container.name}. Inside you see: {listed}."

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

    def _resolve_player(self, player_name):
        """Return the registered player to act as, falling back to the legacy slot."""
        if player_name:
            return self.get_player(player_name)
        return None

    async def subscribe(self, event_type: str, callback: Callable) -> None:
        """Registers a listener for semantic events."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

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
        if input_str in aliases:
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
                    names = [d.capitalize() for d in exits_dict.keys()]
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
        # open, empty one reports empty; otherwise its contents spill into the
        # room so a plain `take` can then pick them up.
        elif resolved_verb == "open":
            if not resolved_arg:
                response = "Open what? Try `open <chest>`."
                matched = True
            else:
                response = await self._handle_open(resolved_arg, player_name)
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
            held = self._find_item(player.contents, resolved_arg) if player is not None else None
            if not self._room_is_lit(player, room):
                response = "It is too dark to see anything."
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
            held = self._find_item(player.contents, resolved_arg) if player is not None else None
            if not self._room_is_lit(player, room):
                response = "It is too dark to see anything."
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

        # INVENTORY / HELP (UTILITY)
        # LIGHT — ignite a carried light source: `light <torch/lantern>`.
        elif resolved_verb == "light" and resolved_arg:
            response = await self._handle_light(resolved_arg, player_name)
            matched = True

        elif resolved_verb == "inventory":
            held = self._held_items(self._resolve_player(player_name))
            if held:
                response = f"You are carrying: {', '.join(held)}."
            else:
                response = "You are not carrying anything."
            matched = True

        elif resolved_verb == "help":
            response = (
                "Commands: look (l), go <direction>, exits, take <item>, drop <item>, put <item> in <container>, "
                "examine <thing> (exam), read <thing> (signs, books), look in <chest>, light <torch/lantern>, inventory (i), say <text>, hello. Try `look` first."
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

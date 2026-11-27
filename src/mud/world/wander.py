"""Deterministic NPC wandering on the 1 Hz tick (#82).

The wanderer owns the whole of NPC motion: a tick counter (one tick is one
second of world time), a seeded RNG per NPC, and each NPC's `wander_every`
schedule. It mutates only `Room.npcs`; an NPC never enters `Room.contents`, so
the item vocabulary (`take`, `drop`, carry weight) never sees one.

There are no wall-clock reads and no unseeded randomness here. A reload from
the same world file gets the same `World.rng_seed`, and each NPC's stream is
derived from that seed and its stable authored id. CRC32 is used rather than
Python's `hash()` because the latter is salted per process, which would make a
restart replay a different path.

Only an exit that leads to a loaded `Room` and is currently unlocked is a
candidate. An exit whose value is not a `Room` (a malformed world) or whose
target is not in the engine is skipped, never entered.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from src.mud.content import NPC, Room
from src.mud.storage.keys import room_key


def npc_key(npc: NPC) -> str:
    """The stable authored id of an NPC, used for scheduling and the wire.

    `item_id` is the world-file id the loader copies and `room.state.npcs[]`
    publishes; `instance_id` and the display name are fallbacks for entities
    built in memory before the loader exists. Unlike `object_key`, the authored
    id wins: an NPC's public handle must not change when it moves.
    """
    return (
        getattr(npc, "item_id", None)
        or getattr(npc, "instance_id", None)
        or npc.name
    )


@dataclass(frozen=True)
class NpcMove:
    """One completed step: which NPC went where, and by which exit."""

    npc_id: str
    npc_name: str
    from_room: Room
    to_room: Room
    direction: str

    def event_data(self) -> dict:
        """The structured payload shared by `npc_moved` and the room events."""
        return {
            "npc": {"id": self.npc_id, "name": self.npc_name},
            "from": {
                "id": room_key(self.from_room),
                "name": self.from_room.name,
            },
            "to": {"id": room_key(self.to_room), "name": self.to_room.name},
            "direction": self.direction,
        }


class NpcWanderer:
    """Move authored wandering NPCs one deterministic step per `step()`.

    `rooms` is the engine's live room map (display name -> room), not a copy:
    moving an NPC changes the same `Room.npcs` lists the commands and
    `room.state` read. `tick` starts at zero and is incremented by every
    `step()`, so a test can assert time passed even when nobody moved.
    """

    def __init__(self, rooms: dict[str, Room], seed: int = 0):
        self._rooms = rooms
        self.seed = int(seed)
        self.tick = 0
        self._npcs: list[tuple[str, NPC]] = sorted(
            (
                (npc_key(npc), npc)
                for room in rooms.values()
                for npc in room.npcs
            ),
            key=lambda entry: (entry[0], entry[1].name.lower()),
        )
        # One independent stream per NPC: adding a second wanderer to the
        # world must not perturb the first one's path.
        self._rngs: dict[str, random.Random] = {
            npc_id: random.Random(self._seed_for(npc_id))
            for npc_id, _npc in self._npcs
        }

    def _seed_for(self, npc_id: str) -> int:
        """A stable per-NPC seed: world seed XOR the id's CRC32."""
        return (self.seed ^ zlib.crc32(npc_id.encode("utf-8"))) & 0xFFFFFFFF

    def _locations(self) -> dict[int, Room]:
        """Identity -> room for every placed NPC, as the room lists stand now."""
        locations: dict[int, Room] = {}
        for room in self._rooms.values():
            for npc in room.npcs:
                locations.setdefault(id(npc), room)
        return locations

    def _candidates(self, room: Room) -> list[tuple[str, Room]]:
        """Unlocked exits that lead to a loaded room, sorted by direction."""
        candidates: list[tuple[str, Room]] = []
        for direction, target in sorted(room.exits.items()):
            if not isinstance(target, Room):
                continue
            if target is room:
                continue
            if target not in self._rooms.values():
                continue
            if room.exit_is_locked(direction):
                continue
            candidates.append((direction, target))
        return candidates

    def step(self) -> list[NpcMove]:
        """Advance one tick and return every move that happened.

        An NPC moves on ticks where `tick % wander_every == 0`, so the first
        move is exactly `wander_every` seconds after the tick starts. A
        non-positive `wander_every` (rejected by the content model, but
        possible on a hand-built entity) means the NPC has no schedule and
        does not move. `wander: false` always stays put.
        """
        self.tick += 1
        locations = self._locations()
        moves: list[NpcMove] = []

        for npc_id, npc in self._npcs:
            if not npc.wander:
                continue
            every = int(getattr(npc, "wander_every", 0) or 0)
            if every <= 0 or self.tick % every:
                continue
            room = locations.get(id(npc))
            if room is None:
                continue
            candidates = self._candidates(room)
            if not candidates:
                continue

            rng = self._rngs[npc_id]
            direction, destination = candidates[rng.randrange(len(candidates))]
            room.npcs.remove(npc)
            destination.npcs.append(npc)
            destination.npcs.sort(
                key=lambda candidate: (npc_key(candidate),
                                       candidate.name.lower())
            )
            locations[id(npc)] = destination
            moves.append(NpcMove(
                npc_id=npc_id,
                npc_name=npc.name,
                from_room=room,
                to_room=destination,
                direction=direction,
            ))

        return moves

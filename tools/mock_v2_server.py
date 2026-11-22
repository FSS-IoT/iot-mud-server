"""A protocol-v2 mock server for client authors.

This is **not** the real server. It is a small, shared-world harness that speaks
the same v2 handshake and the command/event surface used by `SESSION.md`, so a
client can be tried without running the engine:

    uv run python tools/mock_v2_server.py
    # client: ws://localhost:8770

It supports join, movement, look, take/drop, say, inventory, the forest chest
(lock/use/open/get-from/close/put), errors, and quit. The real server remains
authoritative; the mock is scripted content with no engine, database, or
persistence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import websockets

PORT = 8770

ROOMS = {
    "gate": {
        "id": "gate",
        "name": "Village Gate",
        "description": "A heavy stone gate stands tall. "
                       "To the north is the Marketplace.",
        "exits": {"north": "market"},
    },
    "market": {
        "id": "market",
        "name": "Marketplace",
        "description": "Lively vendors cry out their wares. "
                       "Exits lead South, West, and North.",
        "exits": {"south": "gate", "west": "shop", "north": "forest"},
    },
    "shop": {
        "id": "shop",
        "name": "Alchemist Shop",
        "description": "Vials glimmer on shelves. It smells of sulfur.",
        "exits": {"east": "market"},
    },
    "forest": {
        "id": "forest",
        "name": "Mysterious Forest",
        "description": "Trees loom high above, blocking much of the sun.",
        "exits": {"south": "market"},
        "dark": True,
    },
}

ITEMS = {
    "sign": {
        "id": "sign", "kind": "sign", "name": "Village Sign",
        "portable": True, "readable": True,
        "description": "A wooden signpost. The arrows point North to the "
                       "Marketplace; the Old Tavern is east of the market.",
    },
    "gold_coin": {
        "id": "gold_coin", "kind": "gold_coin", "name": "Gold Coin",
        "portable": True, "description": "A shiny, minted copper coin.",
    },
    "torch": {
        "id": "torch", "kind": "torch", "name": "Torch",
        "portable": True, "light_source": True, "lit": False,
        "description": "A flickering wooden torch.",
    },
    "gold_key": {
        "id": "gold_key", "kind": "gold_key", "name": "Gold Key",
        "portable": True, "description": "A small gold key, warm to the touch.",
    },
    "brass_lantern": {
        "id": "brass_lantern", "kind": "brass_lantern",
        "name": "Brass Lantern", "portable": True, "light_source": True,
        "lit": False, "description": "A brass lantern, its wick still dry.",
    },
    "chest": {
        "id": "chest_forest", "kind": "chest_forest", "name": "Chest",
        "portable": True, "container": True,
        "description": "A sturdy oak chest, bound in rusted iron.",
    },
}

ROOM_ITEMS = {
    "gate": ["sign"],
    "market": ["gold_coin"],
    "shop": ["torch", "gold_key"],
    "forest": ["chest"],
}


def _ts() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value.lower()).strip("_")


class World:
    """The shared item/chest state; each MockServer owns one."""

    def __init__(self):
        self.rooms = {room: list(items) for room, items in ROOM_ITEMS.items()}
        self.players: dict[str, Session] = {}
        self.chest_locked = True
        self.chest_open = False
        # The lantern starts inside the chest; `open` lists it, `get ... from`
        # pulls it out, and `put` puts it back (#76).
        self.chest_items: list[str] = ["brass_lantern"]
        self.lit_items: set[str] = set()


class Session:
    def __init__(self, world: World, websocket):
        self.world = world
        self.ws = websocket
        self.name: str | None = None
        self.room = "gate"
        self.inventory: list[dict] = []
        self.seq = 0

    # -- envelopes -----------------------------------------------------

    def envelope(self, type_: str, data: dict, msg_id=None) -> dict:
        self.seq += 1
        message = {
            "v": 2,
            "type": type_,
            "event_id": f"{type_}-{self.seq}",
            "seq": self.seq,
            "ts": _ts(),
            "data": data,
        }
        if msg_id is not None:
            message["id"] = msg_id
        return message

    async def send(self, type_: str, data: dict, msg_id=None) -> None:
        await self.ws.send(json.dumps(self.envelope(type_, data, msg_id)))

    async def broadcast(self, type_: str, data: dict, *,
                        room: str | None = None) -> None:
        target = room or self.room
        for other in list(self.world.players.values()):
            if other is self or other.room != target:
                continue
            await other.send(type_, data)

    # -- state ---------------------------------------------------------

    def _item(self, key: str) -> dict:
        item = dict(ITEMS[key])
        if key == "chest":
            item["locked"] = self.world.chest_locked
            item["open"] = self.world.chest_open
        if item.get("light_source"):
            item["lit"] = key in self.world.lit_items
        return item

    def room_payload(self) -> dict:
        base = ROOMS[self.room]
        dark = base.get("dark", False) and not any(
            ITEMS.get(item["id"], {}).get("light_source")
            and item.get("lit")
            for item in self.inventory)
        players = [
            {"name": other.name}
            for other in self.world.players.values()
            if other is not self and other.room == self.room and other.name
        ]
        players.sort(key=lambda person: person["name"].lower())
        if dark:
            return {
                "id": base["id"], "name": base["name"],
                "description": base["description"], "dark": True,
                "exits": [], "objects": [], "players": [], "npcs": [],
            }
        objects = [self._item(key) for key in self.world.rooms[self.room]]
        objects.sort(key=lambda item: item["name"].lower())
        exits = [
            {"direction": direction, "to": target, "locked": False}
            for direction, target in sorted(base["exits"].items())
        ]
        return {
            "id": base["id"], "name": base["name"],
            "description": base["description"], "dark": False,
            "exits": exits, "objects": objects,
            "players": players, "npcs": [],
        }

    def room_state(self) -> dict:
        room = self.room_payload()
        text = f"{room['name']}\n{room['description']}"
        if room["objects"]:
            text += "\n\nHere you see: " + ", ".join(
                item["name"] for item in room["objects"]) + "."
        if room["players"]:
            text += "\n\nOthers in the room: " + ", ".join(
                person["name"] for person in room["players"]) + "."
        if room["dark"]:
            text = "It is too dark to see anything."
        return {"room": room, "text": text}

    def inventory_payload(self) -> dict:
        items = sorted(self.inventory, key=lambda item: item["name"].lower())
        if items:
            text = "You are carrying: " + ", ".join(
                item["name"] for item in items) + "."
        else:
            text = "You are not carrying anything."
        return {"items": items, "text": text}

    def error(self, msg_id, code: str, message: str) -> dict:
        return self.envelope("error", {
            "code": code, "message": message, "text": message,
        }, msg_id)

    # -- item lookup ---------------------------------------------------

    def _find_room_item(self, name: str) -> str | None:
        needle = name.strip().lower()
        if not needle:
            return None
        for key in self.world.rooms[self.room]:
            if needle in ITEMS[key]["name"].lower():
                return key
        return None

    def _find_inventory(self, name: str) -> dict | None:
        needle = name.strip().lower()
        if not needle:
            return None
        for item in self.inventory:
            if needle in item["name"].lower():
                return item
        return None


class MockServer:
    """One shared world; each websocket gets a Session in it."""

    def __init__(self):
        self.world = World()

    async def handler(self, websocket) -> None:
        session = Session(self.world, websocket)
        await session.send("hello", {
            "server": "iot-mud-mock",
            "protocol": [2],
            "world": {"name": "Starting Village", "epoch": "mock-1"},
            "auth_required": False,
            "motd": "Welcome to the IoT MUD!",
            "text": "Welcome to the IoT MUD!",
        })
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await session.send("error", {
                        "code": "malformed_message",
                        "message": "That message was not valid JSON.",
                        "text": "That message was not valid JSON.",
                    })
                    continue

                kind = msg.get("type")
                msg_id = msg.get("id")
                data = msg.get("data")
                if not isinstance(data, dict):
                    data = {}

                if kind == "join":
                    await self._join(session, data, msg_id)
                    continue
                if kind == "quit":
                    break
                if kind != "input":
                    await session.send("error", {
                        "code": "unknown_message",
                        "message": f"Unknown message type: {kind!r}.",
                        "text": f"Unknown message type: {kind!r}.",
                    }, msg_id)
                    continue

                keep_going = await self._input(
                    session, str(data.get("text", "")), msg_id)
                if not keep_going:
                    break
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._leave(session)
            try:
                await websocket.close()
            except websockets.exceptions.ConnectionClosed:
                pass

    async def _join(self, session: Session, data: dict, msg_id) -> None:
        if session.name is not None:
            await session.send("error", {
                "code": "already_joined",
                "message": "This connection has already joined.",
                "text": "This connection has already joined.",
            }, msg_id)
            return
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            await session.send("error", {
                "code": "bad_args", "message": "Join needs a name.",
                "text": "Join needs a name.",
            }, msg_id)
            return
        name = name.strip()
        if name in self.world.players:
            await session.send("error", {
                "code": "name_taken", "message": "That name is already in use.",
                "text": "That name is already in use.",
            }, msg_id)
            return
        session.name = name
        session.room = "gate"
        self.world.players[name] = session
        await session.send("session.ok", {
            "player": {"name": name},
            "world": {"name": "Starting Village", "epoch": "mock-1"},
            "mode": "guest", "text": f"Welcome, {name}.",
        }, msg_id)
        await session.send("room.state", session.room_state())
        await session.broadcast("room.enter", {
            "room": {"id": "gate", "name": "Village Gate"},
            "actor": {"name": name}, "reason": "connect",
            "text": f"{name} has arrived.",
        })

    async def _leave(self, session: Session) -> None:
        name = session.name
        if name is None:
            return
        if self.world.players.get(name) is session:
            await session.broadcast("room.leave", {
                "room": {"id": ROOMS[session.room]["id"],
                         "name": ROOMS[session.room]["name"]},
                "actor": {"name": name}, "reason": "disconnect",
                "text": f"{name} has left.",
            })
            del self.world.players[name]

    async def _input(self, session: Session, text: str, msg_id) -> bool:
        if session.name is None:
            await session.send("error", {
                "code": "not_joined", "message": "Join before commands.",
                "text": "Join before commands.",
            }, msg_id)
            return True

        raw = text.strip()
        lowered = raw.lower()
        parts = lowered.split(maxsplit=1)
        verb = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if verb in ("quit", "exit"):
            return False
        if verb in ("look", "l"):
            if arg.startswith("at "):
                await self._examine(session, arg[3:], msg_id)
            elif arg.startswith("in "):
                await self._look_in(session, arg[3:], msg_id)
            else:
                await session.send("room.state", session.room_state(),
                                   msg_id)
            return True
        if verb in ("go", "n", "s", "e", "w", "north", "south", "east",
                    "west"):
            direction = {"n": "north", "s": "south", "e": "east",
                         "w": "west"}.get(verb, verb)
            if verb == "go":
                direction = arg.split()[0] if arg else ""
            await self._go(session, direction, msg_id)
            return True
        if verb == "get" and " from " in arg:
            await self._get_from(session, arg, msg_id)
            return True
        if verb in ("take", "get"):
            await self._take(session, arg, msg_id)
            return True
        if verb == "drop":
            await self._drop(session, arg, msg_id)
            return True
        if verb == "put":
            await self._put(session, arg, msg_id)
            return True
        if verb == "open":
            await self._open(session, arg, msg_id)
            return True
        if verb == "close":
            await self._close(session, arg, msg_id)
            return True
        if verb == "use":
            await self._use(session, arg, msg_id)
            return True
        if verb == "light":
            await self._light(session, arg, msg_id)
            return True
        if verb == "read":
            await self._read(session, arg, msg_id)
            return True
        if verb in ("inventory", "i", "inv"):
            await session.send("player.inventory",
                               session.inventory_payload(), msg_id)
            return True
        if verb in ("say",):
            await self._say(session, raw, msg_id)
            return True
        if verb in ("exits",):
            exits = ROOMS[session.room]["exits"]
            names = ", ".join(direction.capitalize()
                              for direction in exits)
            await session.send("notice", {
                "text": f"Available exits: {names}.",
                "level": "info",
            }, msg_id)
            return True
        if verb in ("examine", "exam"):
            await self._examine(session, arg, msg_id)
            return True
        if verb in ("help", "h"):
            await session.send("notice", {
                "text": "Commands: look, go <direction>, take <item>, "
                        "get <item> from <chest>, drop <item>, "
                        "put <item> in <chest>, open <chest>, "
                        "close <chest>, use <key> on <chest>, "
                        "light <torch/lantern>, read <item>, inventory, "
                        "say <text>, exits, quit.",
                "level": "info",
            }, msg_id)
            return True
        if not raw:
            await session.send("notice", {
                "text": "Silence falls over you.",
                "level": "info",
            }, msg_id)
            return True

        await session.send("error", {
            "code": "unknown_command",
            "message": "Hmm, I don't understand that. Type `help` for a list "
                       "of commands.",
            "text": "Hmm, I don't understand that. Type `help` for a list of "
                    "commands.",
        }, msg_id)
        return True

    async def _go(self, session: Session, direction: str, msg_id) -> None:
        target = ROOMS[session.room]["exits"].get(direction)
        if target is None:
            await session.send("error", {
                "code": "no_exit",
                "message": f"You cannot go {direction}.",
                "text": f"You cannot go {direction}.",
            }, msg_id)
            return
        old_room = session.room
        session.room = target
        await session.send("room.state", session.room_state(), msg_id)
        await session.broadcast("room.leave", {
            "room": {"id": ROOMS[old_room]["id"],
                     "name": ROOMS[old_room]["name"]},
            "actor": {"name": session.name}, "reason": "move",
            "text": f"{session.name} leaves.",
        }, room=old_room)
        await session.broadcast("room.enter", {
            "room": {"id": ROOMS[target]["id"],
                     "name": ROOMS[target]["name"]},
            "actor": {"name": session.name}, "reason": "move",
            "text": f"{session.name} arrives.",
        })

    async def _take(self, session: Session, name: str, msg_id) -> None:
        key = session._find_room_item(name)
        if key is None:
            await session.send("error", {
                "code": "not_found",
                "message": f"There is no {name.strip()} here.",
                "text": f"There is no {name.strip()} here.",
            }, msg_id)
            return
        item = session._item(key)
        self.world.rooms[session.room].remove(key)
        held = {"id": item["id"], "kind": item["kind"], "name": item["name"]}
        if item.get("light_source"):
            held["lit"] = item.get("lit", False)
        session.inventory.append(held)
        await session.send("object.taken", {
            "object": item, "by": session.name, "self": True,
            "text": f"You pick up the {item['name']}.",
        }, msg_id)
        await session.broadcast("object.taken", {
            "object": item, "by": session.name, "self": False,
            "text": f"{session.name} picks up the {item['name']}.",
        })

    async def _drop(self, session: Session, name: str, msg_id) -> None:
        item = session._find_inventory(name)
        if item is None:
            await session.send("error", {
                "code": "not_found",
                "message": f"You aren't carrying any {name.strip()}.",
                "text": f"You aren't carrying any {name.strip()}.",
            }, msg_id)
            return
        session.inventory.remove(item)
        key = next(key for key, data in ITEMS.items()
                   if data["id"] == item["id"])
        self.world.rooms[session.room].append(key)
        await session.send("object.dropped", {
            "object": item, "by": session.name, "self": True,
            "text": f"You dropped the {item['name']}.",
        }, msg_id)
        await session.send("player.inventory",
                           session.inventory_payload())
        await session.broadcast("object.dropped", {
            "object": item, "by": session.name, "self": False,
            "text": f"{session.name} drops the {item['name']}.",
        })

    async def _put(self, session: Session, arg: str, msg_id) -> None:
        if session.room != "forest" or " in " not in arg:
            await session.send("notice", {
                "text": "Put what where? Try `put <item> in <chest>`.",
                "level": "info",
            }, msg_id)
            return
        item_name, _, container_name = arg.rpartition(" in ")
        item = session._find_inventory(item_name)
        if item is None:
            await session.send("error", {
                "code": "not_found",
                "message": f"You aren't carrying any {item_name.strip()}.",
                "text": f"You aren't carrying any {item_name.strip()}.",
            }, msg_id)
            return
        if "chest" not in container_name.lower():
            await session.send("error", {
                "code": "not_found",
                "message": "There is no container here.",
                "text": "There is no container here.",
            }, msg_id)
            return
        if self.world.chest_locked:
            await session.send("error", {
                "code": "locked", "message": "The Chest is locked.",
                "text": "The Chest is locked.",
            }, msg_id)
            return
        session.inventory.remove(item)
        self.world.chest_items.append(item["id"])
        await session.send("object.put", {
            "object": item,
            "container": {"id": "chest_forest", "name": "Chest"},
            "by": session.name, "self": True,
            "text": f"You put the {item['name']} in the Chest.",
        }, msg_id)
        await session.send("player.inventory",
                           session.inventory_payload())
        await session.broadcast("object.put", {
            "object": item,
            "container": {"id": "chest_forest", "name": "Chest"},
            "by": session.name, "self": False,
            "text": f"{session.name} puts the {item['name']} in the Chest.",
        })

    async def _open(self, session: Session, name: str, msg_id) -> None:
        if session.room != "forest" or "chest" not in name.lower():
            await session.send("error", {
                "code": "not_found", "message": "There is no chest here.",
                "text": "There is no chest here.",
            }, msg_id)
            return
        if self.world.chest_locked:
            await session.send("error", {
                "code": "locked",
                "message": "The Chest is locked. Find a key and use it.",
                "text": "The Chest is locked. Find a key and use it.",
            }, msg_id)
            return
        if self.world.chest_open:
            await session.send("error", {
                "code": "already_open", "message": "The Chest is already open.",
                "text": "The Chest is already open.",
            }, msg_id)
            return
        self.world.chest_open = True
        loot = [ITEMS[item_id]["name"]
                for item_id in self.world.chest_items]
        text = ("You open the Chest. Inside you see: " + ", ".join(loot) + "."
                if loot else "The Chest is empty.")
        await session.send("object.opened", {
            "object": session._item("chest"), "by": session.name,
            "self": True, "text": text,
        }, msg_id)
        await session.broadcast("object.opened", {
            "object": session._item("chest"), "by": session.name,
            "self": False, "text": f"{session.name} opens the Chest.",
        })

    async def _get_from(self, session: Session, arg: str, msg_id) -> None:
        if session.room != "forest" or " from " not in arg:
            await session.send("error", {
                "code": "not_found", "message": "There is no container here.",
                "text": "There is no container here.",
            }, msg_id)
            return
        item_name, _, container_name = arg.rpartition(" from ")
        if "chest" not in container_name.lower():
            await session.send("error", {
                "code": "not_found", "message": "There is no container here.",
                "text": "There is no container here.",
            }, msg_id)
            return
        if not self.world.chest_open:
            await session.send("error", {
                "code": "container_closed", "message": "The Chest is closed.",
                "text": "The Chest is closed.",
            }, msg_id)
            return
        needle = item_name.strip().lower()
        key = next(
            (item_id for item_id in self.world.chest_items
             if needle in ITEMS[item_id]["name"].lower()),
            None,
        )
        if key is None:
            await session.send("error", {
                "code": "not_here",
                "message": f"The {item_name.strip()} is not in the Chest.",
                "text": f"The {item_name.strip()} is not in the Chest.",
            }, msg_id)
            return
        self.world.chest_items.remove(key)
        item = session._item(key)
        held = {"id": item["id"], "kind": item["kind"], "name": item["name"]}
        if item.get("light_source"):
            held["lit"] = item.get("lit", False)
        session.inventory.append(held)
        container = {"id": "chest_forest", "name": "Chest"}
        await session.send("object.got", {
            "object": item, "container": container,
            "by": session.name, "self": True,
            "text": f"You get the {item['name']} from the Chest.",
        }, msg_id)
        await session.send("player.inventory",
                           session.inventory_payload())
        await session.broadcast("object.got", {
            "object": item, "container": container,
            "by": session.name, "self": False,
            "text": f"{session.name} gets the {item['name']} from the Chest.",
        })

    async def _close(self, session: Session, name: str, msg_id) -> None:
        if session.room != "forest" or "chest" not in name.lower():
            await session.send("error", {
                "code": "not_found", "message": "There is no chest here.",
                "text": "There is no chest here.",
            }, msg_id)
            return
        if not self.world.chest_open:
            await session.send("error", {
                "code": "already_closed",
                "message": "The Chest is already closed.",
                "text": "The Chest is already closed.",
            }, msg_id)
            return
        self.world.chest_open = False
        await session.send("object.closed", {
            "object": session._item("chest"), "by": session.name,
            "self": True, "text": "You close the Chest.",
        }, msg_id)
        await session.broadcast("object.closed", {
            "object": session._item("chest"), "by": session.name,
            "self": False, "text": f"{session.name} closes the Chest.",
        })

    async def _use(self, session: Session, arg: str, msg_id) -> None:
        if session.room != "forest" or " on " not in arg:
            await session.send("notice", {
                "text": "Use a key on something: `use <key> on <chest>`.",
                "level": "info",
            }, msg_id)
            return
        key_name, _, container_name = arg.partition(" on ")
        key = session._find_inventory(key_name)
        if key is None or "chest" not in container_name.lower():
            await session.send("error", {
                "code": "not_found",
                "message": "You need the key and the chest here.",
                "text": "You need the key and the chest here.",
            }, msg_id)
            return
        if not self.world.chest_locked:
            await session.send("error", {
                "code": "already_open", "message": "The Chest is already open.",
                "text": "The Chest is already open.",
            }, msg_id)
            return
        self.world.chest_locked = False
        await session.send("object.unlocked", {
            "object": session._item("chest"), "by": session.name,
            "self": True,
            "text": "You turn the Gold Key and the Chest clicks open.",
        }, msg_id)
        await session.broadcast("object.unlocked", {
            "object": session._item("chest"), "by": session.name,
            "self": False, "text": f"{session.name} unlocks the Chest.",
        })

    async def _say(self, session: Session, raw: str, msg_id) -> None:
        parts = raw.split(maxsplit=1)
        spoken = parts[1] if len(parts) > 1 else ""
        if not spoken:
            await session.send("notice", {
                "text": "Say what?", "level": "info",
            }, msg_id)
            return
        await session.send("player.said", {
            "from": session.name, "text": f'You say, "{spoken}".',
            "self": True,
        }, msg_id)
        await session.broadcast("player.said", {
            "from": session.name, "text": f'{session.name} says, "{spoken}".',
            "self": False,
        })

    async def _light(self, session: Session, name: str, msg_id) -> None:
        item = session._find_inventory(name)
        if item is None:
            await session.send("error", {
                "code": "not_found",
                "message": f"You don't have a {name.strip()} to light.",
                "text": f"You don't have a {name.strip()} to light.",
            }, msg_id)
            return
        if not ITEMS.get(item["id"], {}).get("light_source"):
            await session.send("notice", {
                "text": f"You can't light the {item['name']}.",
                "level": "info",
            }, msg_id)
            return
        if item.get("lit", False):
            await session.send("notice", {
                "text": f"The {item['name']} is already lit.",
                "level": "info",
            }, msg_id)
            return
        item["lit"] = True
        self.world.lit_items.add(item["id"])
        obj = {"id": item["id"], "kind": item["kind"],
               "name": item["name"], "lit": True}
        await session.send("object.lit", {
            "object": obj, "by": session.name, "self": True,
            "text": f"You light the {item['name']}. Its flame casts a warm "
                    "glow.",
        }, msg_id)
        await session.broadcast("object.lit", {
            "object": obj, "by": session.name, "self": False,
            "text": f"{session.name} lights the {item['name']}.",
        })

    async def _read(self, session: Session, name: str, msg_id) -> None:
        if "sign" not in name.lower():
            await session.send("error", {
                "code": "not_found",
                "message": "There is nothing readable here.",
                "text": "There is nothing readable here.",
            }, msg_id)
            return
        item = session._item("sign")
        await session.send("object.read", {
            "object": {"id": item["id"], "name": item["name"]},
            "content": item["description"],
            "text": f"It is {item['description']}",
        }, msg_id)

    async def _examine(self, session: Session, name: str, msg_id) -> None:
        item = session._find_inventory(name)
        if item is None:
            key = session._find_room_item(name)
            item = session._item(key) if key else None
        if item is None:
            await session.send("error", {
                "code": "not_found",
                "message": f"You see no {name.strip()} here.",
                "text": f"You see no {name.strip()} here.",
            }, msg_id)
            return
        await session.send("notice", {
            "text": f"It is {item['description']}", "level": "info",
        }, msg_id)

    async def _look_in(self, session: Session, name: str, msg_id) -> None:
        if session.room != "forest" or "chest" not in name.lower():
            await session.send("error", {
                "code": "not_found", "message": "There is no container here.",
                "text": "There is no container here.",
            }, msg_id)
            return
        if self.world.chest_locked:
            await session.send("error", {
                "code": "locked", "message": "The Chest is locked.",
                "text": "The Chest is locked.",
            }, msg_id)
            return
        loot = [ITEMS[item_id]["name"] for item_id in self.world.chest_items]
        text = ("You see inside the Chest: " + ", ".join(loot) + "."
                if loot else "The Chest is empty.")
        await session.send("notice", {"text": text, "level": "info"}, msg_id)


async def main(port: int = PORT) -> None:
    mock = MockServer()
    async with websockets.serve(mock.handler, "localhost", port):
        print(f"Mock protocol-v2 server on ws://localhost:{port}")
        print("Run: uv run python tools/mock_v2_server.py --port "
              f"{port}")
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Protocol-v2 mock server for client authors")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port))
    except KeyboardInterrupt:
        print("\nShutting down.")

"""A minimal reference client for protocol v2.

This is a worked example, **not a template**. It is deliberately small so it can
be read end-to-end; build your own client in whatever language and UI you like.
It speaks v2 only and expects the server to advertise protocol `2`.

    # register a new character; the server shows the key once
    uv run python examples/reference_client.py --name Ada
    # log in again with the saved key (no name needed)
    uv run python examples/reference_client.py --key "$MUD_KEY"
    # against the guest mock:
    uv run python examples/reference_client.py --name Ada --uri ws://localhost:8770
"""

import argparse
import asyncio
import json

import websockets

CLEAR = "\033[0m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"


def render_message(message: dict, name: str) -> str:
    """Render one v2 server message. Pure, so it is easy to unit-test."""
    kind = message.get("type", "")
    data = message.get("data", {})

    if kind == "room.state":
        room = data.get("room") or {}
        out = f"{GREEN}{room.get('name', '')}{CLEAR}"
        description = room.get("description")
        if description:
            out += "\n" + description
        if room.get("dark"):
            return out + f"{CLEAR}\nIt is too dark to see anything.\n"
        objects = [obj.get("name", "") for obj in (room.get("objects") or [])
                   if obj.get("name")]
        if objects:
            out += f"\nHere you see: {', '.join(objects)}."
        players = [person.get("name", "")
                   for person in (room.get("players") or [])
                   if person.get("name") and person.get("name") != name]
        if players:
            out += f"\nOthers in the room: {', '.join(players)}."
        exits = [exit_.get("direction", "")
                 for exit_ in (room.get("exits") or [])
                 if exit_.get("direction")]
        if exits:
            out += "\nExits: " + ", ".join(
                direction.capitalize() for direction in exits) + "."
        return out + f"{CLEAR}\n"

    if kind in ("session.ok", "auth.ok"):
        return ""

    if kind == "notice":
        return f"{GREEN}{data.get('text', '')}{CLEAR}\n"

    if kind == "auth.error":
        return f"{RED}{data.get('message', '')}{CLEAR}\n"

    if kind == "error":
        return f"{RED}{data.get('message', '')}{CLEAR}\n"

    if kind in ("player.said", "player.emoted", "player.told",
                "room.enter", "room.leave"):
        return f"{CYAN}{data.get('text', '')}{CLEAR}\n"

    if kind in ("object.taken", "object.dropped", "object.opened",
                "object.closed", "object.put", "object.got", "object.read",
                "object.lit", "object.locked", "object.unlocked",
                "player.inventory",
                "exit.locked", "exit.unlocked"):
        return f"{GREEN}{data.get('text', '')}{CLEAR}\n"

    if kind == "shutdown":
        text = data.get("text") or data.get("reason", "Server shutting down.")
        return f"{YELLOW}{text}{CLEAR}\n"

    return ""


async def _enter(websocket, name: str | None, key: str | None,
                 auth_required: bool) -> str | None:
    """Authenticate (or guest-join) and show the first room.

    Returns the bound character name, or None when the server rejected the
    attempt. On registration the key is printed exactly once; this client
    never logs it and never sends it anywhere else.
    """
    if not auth_required:
        if key is not None:
            print(f"{YELLOW}This server does not require auth; "
                  f"ignoring --key.{CLEAR}")
        await websocket.send(json.dumps({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": name},
        }))
        return name

    if key is not None:
        request = {"mode": "login", "key": key}
    else:
        request = {"mode": "register", "name": name}
    await websocket.send(json.dumps({
        "v": 2, "type": "auth", "id": "auth", "data": request,
    }))

    reply = json.loads(await websocket.recv())
    if reply.get("type") != "auth.ok":
        print(render_message(reply, name or ""), end="")
        return None

    data = reply.get("data", {})
    character = (data.get("player") or {}).get("name") or name or "Traveler"
    if data.get("mode") == "register":
        registration_key = data.get("key")
        if registration_key:
            print(f"{YELLOW}{BOLD}Your character key is shown once. "
                  f"Store it outside the repository:{CLEAR}")
            print(f"{BOLD}{registration_key}{CLEAR}\n")
    print(f"{YELLOW}Logged in as: {character}{CLEAR}\n")

    while True:
        message = json.loads(await websocket.recv())
        if message.get("type") == "room.state":
            rendered = render_message(message, character)
            if rendered:
                print(rendered)
            return character
        rendered = render_message(message, character)
        if rendered:
            print(rendered)


async def client_handler(uri: str, name: str | None = None,
                         key: str | None = None) -> None:
    print(f"{BLUE}{BOLD}Connecting to MUD at {uri}...{CLEAR}")

    # Stream events render live; direct replies are paired by id in the loop.
    stream_events = asyncio.Queue()
    replies = asyncio.Queue()

    async with websockets.connect(uri) as websocket:
        handshake = json.loads(await websocket.recv())
        if handshake.get("type") != "hello":
            print(f"{RED}Unexpected handshake: {handshake}{CLEAR}")
            return
        handshake_data = handshake.get("data", {})
        supported = handshake_data.get("protocol", [])
        if 2 not in supported:
            print(f"{RED}This reference client speaks protocol 2 only "
                  f"(server offered {supported}).{CLEAR}")
            return

        motd = handshake_data.get("motd", "Welcome to the MUD!")
        print(f"\n{CYAN}{BOLD}--- {motd} ---{CLEAR}\n")

        character = await _enter(
            websocket, name, key, bool(handshake_data.get("auth_required")))
        if character is None:
            return
        if not handshake_data.get("auth_required"):
            print(f"{YELLOW}Logged in as: {character} (guest){CLEAR}\n")

        async def stream_dispatcher():
            while True:
                message = await stream_events.get()
                rendered = render_message(message, character)
                if rendered:
                    print(rendered)

        async def reader():
            while True:
                message = json.loads(await websocket.recv())
                if message.get("id") in ("join", "auth"):
                    stream_events.put_nowait(message)
                elif "id" in message:
                    replies.put_nowait(message)
                else:
                    stream_events.put_nowait(message)

        reader_task = asyncio.create_task(reader())
        stream_task = asyncio.create_task(stream_dispatcher())

        sequence = 1
        try:
            while True:
                raw = await asyncio.to_thread(
                    input, f"{BOLD}{character}{CLEAR} > ")
                text = raw.strip()
                if not text:
                    continue
                if text.lower() in ("exit", "quit"):
                    await websocket.send(json.dumps({
                        "v": 2, "type": "quit",
                    }))
                    print("Goodbye!")
                    break

                await websocket.send(json.dumps({
                    "v": 2, "type": "input", "id": sequence,
                    "data": {"text": text},
                }))
                sequence += 1

                message = await replies.get()
                rendered = render_message(message, character)
                if rendered:
                    print(rendered)
                else:
                    reply = message.get("data", {}).get("text", "")
                    if reply:
                        print(f"{GREEN}{reply}{CLEAR}\n")
        finally:
            reader_task.cancel()
            stream_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Minimal protocol-v2 reference client (not a template)")
    parser.add_argument("--uri", default="ws://localhost:8765",
                        help="WebSocket URI (default: ws://localhost:8765)")
    parser.add_argument("--name", default=None,
                        help="character name to register (prompts if omitted)")
    parser.add_argument("--key", default=None,
                        help="saved character key; logs in instead of "
                             "registering")
    args = parser.parse_args()

    username = args.name
    if username is None and args.key is None:
        username = input("Enter your character name: ").strip()
    if not username and args.key is None:
        username = "Traveler"

    asyncio.run(client_handler(args.uri, username, args.key))

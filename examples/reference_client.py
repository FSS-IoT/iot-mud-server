"""A minimal reference client for protocol v2.

This is a worked example, **not a template**. It is deliberately small so it can
be read end-to-end; build your own client in whatever language and UI you like.
It speaks v2 only and expects the server to advertise protocol `2`.

    # register a new character; show the key once and save a 0600 copy
    uv run python examples/reference_client.py --name Ada \
        --save-key ~/.config/iot-mud/ada.key
    # log in again from the saved key (the key is not repeated)
    uv run python examples/reference_client.py --key-file ~/.config/iot-mud/ada.key
    # against the guest mock:
    uv run python examples/reference_client.py --name Ada --uri ws://localhost:8770

Inside the prompt, `!malformed` sends one broken frame so you can check that an
error does not close the socket. It is a debugging aid, not a protocol command.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import websockets

CLEAR = "\033[0m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"


def render_hello(data: dict) -> str:
    """Print the fields SESSION.md step 1 asks a client to read."""
    protocol = ", ".join(str(version) for version in data.get("protocol", []))
    world = data.get("world") or {}
    modes = ", ".join(data.get("auth_modes") or [])
    auth = "required" if data.get("auth_required") else "not required"
    if modes:
        auth += f" ({modes})"
    lines = [
        f"{CYAN}{BOLD}--- {data.get('motd', 'Welcome to the MUD!')} ---{CLEAR}",
        f"{CYAN}Protocol: {protocol or '?'}{CLEAR}",
        (f"{CYAN}World: {world.get('name', '?')} "
         f"(epoch {world.get('epoch', '?')}){CLEAR}"),
        f"{CYAN}Auth: {auth}{CLEAR}",
        f"{CYAN}Server commit: {data.get('server_commit', 'unknown')}{CLEAR}",
    ]
    return "\n".join(lines) + "\n"


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
        people = []
        for person in room.get("players") or []:
            person_name = person.get("name")
            if not person_name or person_name == name:
                continue
            flags = [
                flag for flag in ("asleep", "stone") if person.get(flag)
            ]
            people.append(
                person_name + (f" ({', '.join(flags)})" if flags else "")
            )
        if people:
            out += f"\nOthers in the room: {', '.join(people)}."
        exits = []
        for exit_ in room.get("exits") or []:
            direction = exit_.get("direction", "")
            if not direction:
                continue
            if exit_.get("locked"):
                direction += " (locked)"
            exits.append(direction.capitalize())
        if exits:
            out += "\nExits: " + ", ".join(exits) + "."
        return out + f"{CLEAR}\n"

    if kind in ("session.ok", "auth.ok"):
        return ""

    if kind == "notice":
        return f"{GREEN}{data.get('text', '')}{CLEAR}\n"

    if kind == "auth.error":
        code = data.get("code")
        prefix = f"[{code}] " if code else ""
        return f"{RED}{prefix}{data.get('message', '')}{CLEAR}\n"

    if kind == "error":
        code = data.get("code")
        prefix = f"[{code}] " if code else ""
        text = data.get("message") or data.get("text", "")
        return f"{RED}{prefix}{text}{CLEAR}\n"

    if kind in ("player.said", "player.emoted", "player.told",
                "room.enter", "room.leave"):
        return f"{CYAN}{data.get('text', '')}{CLEAR}\n"

    if kind == "who.list":
        rows = []
        for row in data.get("players") or []:
            state = "asleep" if row.get("asleep") else "awake"
            if row.get("stone"):
                state += ", stone"
            rows.append(
                f"{row.get('name', '?')} ({row.get('room_id', '?')}, {state})"
            )
        return f"{CYAN}Who: {'; '.join(rows)}.{CLEAR}\n"

    if kind == "player.inventory":
        items = data.get("items") or []
        if items:
            names = ", ".join(
                item.get("name", "?")
                + (" (lit)" if item.get("lit") else "")
                for item in items
            )
            line = f"You are carrying: {names}."
        else:
            line = "You are not carrying anything."
        if data.get("capacity") is not None:
            line += f" ({data.get('used')}/{data.get('capacity')})"
        return f"{GREEN}{line}{CLEAR}\n"

    if kind == "npc.say":
        topic = data.get("topic")
        suffix = f" [topic: {topic}]" if topic else ""
        return f"{CYAN}{data.get('text', '')}{suffix}{CLEAR}\n"

    if kind in ("object.taken", "object.dropped", "object.opened",
                "object.closed", "object.put", "object.got", "object.read",
                "object.lit", "object.locked", "object.unlocked",
                "exit.locked", "exit.unlocked"):
        return f"{GREEN}{data.get('text', '')}{CLEAR}\n"

    if kind == "shutdown":
        text = data.get("text") or data.get("reason", "Server shutting down.")
        return f"{YELLOW}{text}{CLEAR}\n"

    return ""


def write_key(path: Path, key: str) -> None:
    """Write a character key to a new file only the owner can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    os.chmod(path, 0o600)


def read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"error: could not read key file {path}: {exc}")
    if "\n" in key:
        raise SystemExit(
            f"error: key file {path} must contain exactly one key on one line"
        )
    return key


async def _enter(websocket, name: str | None, key: str | None,
                 auth_required: bool,
                 save_key: Path | None = None) -> str | None:
    """Authenticate (or guest-join) and show the first room.

    Returns the bound character name, or None when the server rejected the
    attempt. On registration the key is printed exactly once and, when asked,
    written to `save_key` with mode 0600; this client never logs it and never
    sends it anywhere else.
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
            if save_key is not None:
                write_key(save_key, registration_key)
            print(f"{YELLOW}{BOLD}Your character key is shown once. "
                  f"Store it outside the repository:{CLEAR}")
            print(f"{BOLD}{registration_key}{CLEAR}")
            if save_key is not None:
                print(f"{YELLOW}Saved to {save_key} (mode 0600).{CLEAR}")
            print()
        print(f"{YELLOW}Registered as: {character}{CLEAR}\n")
    else:
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
                         key: str | None = None,
                         save_key: Path | None = None) -> None:
    print(f"{BLUE}{BOLD}Connecting to MUD at {uri}...{CLEAR}")

    # Stream events render live; direct replies are paired by id in the loop.
    stream_events = asyncio.Queue()
    replies = asyncio.Queue()

    async with websockets.connect(uri) as websocket:
        handshake = json.loads(await websocket.recv())
        if handshake.get("type") != "hello":
            print(f"{RED}Unexpected handshake: {handshake}{CLEAR}")
            raise SystemExit(1)
        handshake_data = handshake.get("data", {})
        supported = handshake_data.get("protocol", [])
        if 2 not in supported:
            print(f"{RED}This reference client speaks protocol 2 only "
                  f"(server offered {supported}).{CLEAR}")
            raise SystemExit(1)

        print(f"\n{render_hello(handshake_data)}")

        character = await _enter(
            websocket, name, key, bool(handshake_data.get("auth_required")),
            save_key=save_key)
        if character is None:
            raise SystemExit(1)
        if not handshake_data.get("auth_required"):
            print(f"{YELLOW}Logged in as: {character} (guest){CLEAR}\n")

        async def stream_dispatcher():
            while True:
                message = await stream_events.get()
                rendered = render_message(message, character)
                if rendered:
                    print(rendered)

        # A direct reply is printed by the input loop; hold the reader there
        # until it has, so a follow-up push (`player.inventory` after
        # `object.got`) cannot print before the event it follows.
        reply_printed = asyncio.Event()

        async def reader():
            while True:
                message = json.loads(await websocket.recv())
                if message.get("id") in ("join", "auth"):
                    stream_events.put_nowait(message)
                elif "id" in message:
                    replies.put_nowait(message)
                    await reply_printed.wait()
                    reply_printed.clear()
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

                if text.lower() == "!malformed":
                    await websocket.send("{")
                    print(f"{YELLOW}Sent one malformed frame; the server's "
                          f"error must not close this socket.{CLEAR}")
                    await asyncio.sleep(0.2)
                    continue

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
                reply_printed.set()
        finally:
            reader_task.cancel()
            stream_task.cancel()

        try:
            await asyncio.wait_for(websocket.wait_closed(), timeout=2.0)
        except (TimeoutError, websockets.exceptions.WebSocketException):
            pass
        print(f"{YELLOW}Connection closed: {websocket.close_code}{CLEAR}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Minimal protocol-v2 reference client (not a template)")
    parser.add_argument("--uri", default="ws://localhost:8765",
                        help="WebSocket URI (default: ws://localhost:8765)")
    parser.add_argument("--name", default=None,
                        help="character name to register (prompts if omitted)")
    parser.add_argument("--key", default=None,
                        help="saved character key; logs in instead of "
                             "registering (visible in shell history)")
    parser.add_argument("--key-file", type=Path, default=None,
                        help="file containing a saved key; preferred over "
                             "--key")
    parser.add_argument("--save-key", type=Path, default=None,
                        help="write a newly registered key to this file "
                             "(mode 0600)")
    args = parser.parse_args()

    if args.key is not None and args.key_file is not None:
        parser.error("--key and --key-file are mutually exclusive")
    if args.save_key is not None and (
            args.key is not None or args.key_file is not None):
        parser.error("--save-key is for registration; it cannot be combined "
                     "with --key or --key-file")
    if args.save_key is not None and args.save_key.exists():
        parser.error(
            f"--save-key {args.save_key} already exists; move it or choose "
            f"another path (the client will not overwrite a saved key)"
        )

    key = args.key
    if args.key_file is not None:
        key = read_key(args.key_file)
        if not key:
            parser.error(f"key file {args.key_file} is empty")

    username = args.name
    if username is None and key is None:
        username = input("Enter your character name: ").strip()
    if not username and key is None:
        username = "Traveler"

    asyncio.run(client_handler(args.uri, username, key, args.save_key))

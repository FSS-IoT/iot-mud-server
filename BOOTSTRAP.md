# Build your own client

This is the onboarding path for a club member. You do **not** need to read the
server's source to write a client. You need to speak the protocol and pass the
acceptance session.

The club's teaching method is the [software factory](https://fss-iot.github.io/software-factory/).
Treat your client as a small project: brief, spec, tickets, red/green, review,
ship. The protocol is the spec; `docs/SESSION.md` is the acceptance test.

## 1. Make your repo

Create your own repository wherever you keep code: GitHub, a local git repo,
or another host. Private is fine.

On GitHub, the [docs-only template](https://github.com/FSS-IoT/iot-mud-client-template)
has a "Use this template" button, or use `gh` as an example:

```bash
gh repo create <your-name>-mud-client --private --clone
cd <your-name>-mud-client
```

On a local repo or another host, create an empty repository and copy
`SESSION.md`, `PROTOCOL-VERSION`, and `LICENSE` from the template (or start with
your own README; the client is yours).

Pick any stack: Python, JavaScript/TypeScript, Go, Rust, a shell script, a
browser page. There is no required language and no required framework.

## 2. Run the server locally

You need a server to test against. Clone it anywhere you keep checkouts; it does
not have to be next to your client. The examples use a sibling directory, but
any path works — if your acceptance script takes a server path, point it here.

### Pin the server commit

Do not chase "latest" while you build. Choose one fixed server commit and
record its full 40 characters in your client README. The v2.1.4 reference
server is:

```text
d9441661b8e4971b9c82ee0e675e921c583f2e9a
```

It includes the newcomer walkthrough (#90) forward through the typed-outcome,
one-actor, v2-lifecycle, v1-retirement, and weight work (#130-#147), and is the
current reference for a new client. Build against that commit:

```bash
git clone https://github.com/FSS-IoT/iot-mud-server
cd iot-mud-server
git checkout d9441661b8e4971b9c82ee0e675e921c583f2e9a
uv sync --all-extras
uv run python -m src.mud.admin seed --db data/mud.db
./run_server.sh
```

After `hello`, compare `hello.data.server_commit` with that pin. If they
differ, read the `docs/PROTOCOL.md` changelog from the pinned release forward
to see what your client is missing; a newer v2.1 server may send fields and
events your client has never seen, so "additive" does not mean the wire did
not change. If the server reports `"unknown"`, it is a copy without `.git` and
cannot confirm the pin; use a checkout or ask the operator. The reference
client enforces the pin:

```bash
uv run python examples/reference_client.py --name Ada \
  --expect-commit d9441661b8e4971b9c82ee0e675e921c583f2e9a
```

It prints the expected and reported commit and exits `1` on a mismatch or
`"unknown"`, before authenticating.

Windows:

```powershell
uv run python -m src.mud.net.v2_server --db .scratch\dev.db --port 8766
```

The same commands work; use `--config` and `--db` with Windows paths.

The server listens on `ws://localhost:8765`. To bind a LAN/tailnet interface or
choose another port:

```bash
./run_server.sh --host 0.0.0.0 --port 9000
```

`./run_server.sh --help` lists every flag. `--db PATH` overrides the database
path, so a test server can keep its own file.

For development, use a scratch database and a port other than 8765. Stop the
default server first, then start the scratch one; that keeps a used world from
surprising you and lets a production server keep running:

```bash
uv run python -m src.mud.admin seed --db .scratch/dev.db
./run_server.sh --port 8766 --db .scratch/dev.db
```

`admin seed` creates `.scratch/` and its database. If the file already exists,
seeding refuses with "the database already holds a world"; stop the server and
replace the world explicitly:

```bash
uv run python -m src.mud.admin seed --db .scratch/dev.db --reset --yes
```

`--reset` replaces rooms, exits, objects, and NPCs, but **keeps characters and
their keys** and puts them at the start room. It is a world reset, not a fresh
database. The acceptance in §5 uses its own new file for that reason.

Pick a free port if 8766 is busy, and pass the same value to your client's
`--uri`. The server prints the URI and database it starts with. The CLI rejects
`--port 0`; choose a free number instead.

The v2 server requires auth: the first connection
registers a character and receives a character key exactly once. Keep the key
outside your repository; later connections log in with it. A reconnect to the
same database keeps the world, the characters, and the epoch.

### Mock server (optional)

For a fast client check, `tools/mock_v2_server.py` speaks the v2 handshake
and scripts the session, including the forest chest:

```bash
uv run python tools/mock_v2_server.py
# client: ws://localhost:8770
```

It is a harness, not the real server. It uses the v2.0 guest `join` path
(`auth_required: false`), so it checks your rendering and pairing but not auth.
Use it while building; the real server is the authority for the acceptance.

## 3. Read the contract

- `docs/PROTOCOL.md` — the normative v2.1 contract.
- `docs/CLIENTS.md` — keys, session/sleep/wake, epoch, and client repos.
- `docs/SESSION.md` — the exact session your client must complete.
- `docs/CONTENT.md` — the world and the commands it supports.
- `examples/reference_client.py` — a worked client in Python, if you want to
  see the message flow. It is an example, not a template.

## 4. Build the smallest client first

Do not start with the whole game. Do this in order, with a failing check before
each step:

1. Connect and read `hello`; print the MOTD, world name, `auth_required`, and
   `server_commit`.
2. Send `auth` with `mode: "register"`; show the key once, store it outside the
   repository, and render the following `room.state`. On the next run, log in
   with `mode: "login"` and the saved key.
3. Send `input look` and pair the direct reply by `id`.
4. Parse the room: name, description, exits, objects, players, and NPCs.
5. Add movement, then take/drop, then say.
6. Add the container flow (`open`, `get from`, `put`, `close`), exit locks
   (`use key on exit`), and `talk`/`ask`.
7. Handle `room.enter` / `room.leave`, `player.said`, `player.emoted`,
   `player.told`, `who.list`, `object.*`, `player.inventory`, `notice`, and
   `error`.
8. Keep the socket alive after an error or a malformed frame.
9. Handle a dark room (`room.dark: true`) without inventing contents.
10. Sleep on disconnect and wake on the next login with the retained inventory;
    render a sleeping player as asleep/stone.

Each step is a ticket in your repo. A failing check first (the software
factory's red/green station) is the habit the club is teaching.

A minimal worked client is `examples/reference_client.py`; it is one way to
speak the protocol, not a template. It prints the `hello` fields, the first
room, the key once, and one line per session/object/social/NPC event. Run two
copies against the scratch server from §2, using its port:

```bash
uv run python examples/reference_client.py --name Nova \
  --uri ws://localhost:8766 --save-key ~/.config/iot-mud/nova.key
uv run python examples/reference_client.py --name Pip \
  --uri ws://localhost:8766 --save-key ~/.config/iot-mud/pip.key
```

Nova and Pip are throwaway smoke-test characters on the development database;
the acceptance in §5 registers Ada and Ben on its own new database.

`--save-key` creates the parent directory and writes the key with mode `0600`.
It refuses to overwrite an existing file: move the old key deliberately, or
choose another path. Log in later from the file, so the key never appears in
shell history:

```bash
uv run python examples/reference_client.py --uri ws://localhost:8766 \
  --key-file ~/.config/iot-mud/nova.key
```

The client prints `Connection closed: 1000` after a clean `quit`. Inside the
prompt, `!malformed` sends one broken frame so you can see the
`[malformed_message]` error and confirm the socket stays open.

Against the mock, add `--uri ws://localhost:8770`. The mock does not require
auth, so `--save-key` is ignored there.

## 5. Pass the acceptance session

Use a **separate, new database** for the acceptance. The session takes the gold
coin, unlocks the forest chest and the tavern door, and moves items; a used
database can fail in ways that look like client bugs. Do not reuse the
development database from §4: `admin seed --reset` keeps characters and keys,
so the old smoke-test characters would still be registered there. Stop the
development server before starting the acceptance server.

```bash
uv run python -m src.mud.admin seed --db .scratch/session.db
./run_server.sh --port 8766 --db .scratch/session.db
```

Run two copies of your client (or one copy of yours and a friend's) and follow
`docs/SESSION.md` with Ada and Ben, using `--uri ws://localhost:8766`. Register
them on this database and save their keys with `--save-key`; the Nova and Pip
keys from §4 belong to the development database and will be rejected here. An
automated client can drive two connections against that fresh server;
`SESSION.md` explains how to raise the command limits if your script is faster
than a human.

For a second acceptance run, stop the server and delete the acceptance file
before seeding again (`rm -f .scratch/session.db*`; on PowerShell,
`Remove-Item -Force .scratch\session.db*`). A reset is not enough: it keeps the
characters and their keys.

Record the run in your client repo (for example `docs/ACCEPTANCE.md`) with the
server commit and date. You are done when every step passes and the checklist
at the end of `SESSION.md` is ticked.

Then connect to the club server. Its address is shared with the club, not
committed to the repo. Test with a second player before calling it shipped.

## 6. Contribute back to the server

The server is the shared product. Every member should land at least one small
server PR, so they see both sides of the contract.

1. Fork `FSS-IoT/iot-mud-server`.
2. Pick an issue, or file one describing a concrete improvement.
3. Branch: `fix/<what>` or `feat/<what>`.
4. Write the failing test first, then the change.
5. Run `uv run pytest`.
6. Open a PR. A fresh session reviews it; the reviewer does not fix it.
7. Merge after the review and green tests.

See `CONTRIBUTING.md` for the details.

## Definition of done

Your client is done when:

- it completes `docs/SESSION.md` with two players;
- it survives an error and a malformed frame without disconnecting;
- its README says how to run it and which protocol version it targets;
- it does not contain server addresses, passwords, or other secrets;
- (recommended) you have landed one server PR.

## What not to do

- Do not copy the server into your client. You speak the protocol; you do not
  embed the engine.
- Do not change the protocol privately. Propose it in the server repo so every
  client can adopt it.
- Do not require an LLM or internet access to play. The game is offline-first.
- Do not commit secrets or a database file.

## Getting help

- File an issue on the server repo with the message sequence and what you
  expected.
- Bring it to a club session.
- Read the software-factory stage pages for the part you are stuck on.

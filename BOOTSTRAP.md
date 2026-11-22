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

```bash
git clone https://github.com/FSS-IoT/iot-mud-server
cd iot-mud-server
uv sync --all-extras
uv run python -m src.mud.admin seed --db data/mud.db
./run_server.sh
```

Windows:

```powershell
uv run python -m src.mud.net.v2_server
```

The server listens on `ws://localhost:8765`. To bind a LAN/tailnet interface or
choose another port:

```bash
./run_server.sh --host 0.0.0.0 --port 9000
```

Run one server at a time. A v2.1 server requires auth: the first connection
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
- The server's `tests/test_protocol_v2.py` — a worked client in Python, if you
  want to see the message flow.

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
speak the protocol, not a template. Run two copies for the two-player steps:

```bash
uv run python examples/reference_client.py --name Ada
uv run python examples/reference_client.py --name Ben
```

Against the mock, add `--uri ws://localhost:8770`.

## 5. Pass the acceptance session

Start a **fresh server with a fresh database** for the acceptance. Seed it, or
delete the test database and let `admin seed` recreate it. The session takes
the gold coin, unlocks the forest chest and the tavern door, and moves items; a
second run against the same server can fail in ways that look like client bugs.
Keep the production database out of this: use a scratch path for acceptance.

Run two copies of your client (or one copy of yours and a friend's) and follow
`docs/SESSION.md` with Ada and Ben. An automated client can drive two
connections against that fresh server. Record the run in your client repo (for
example `docs/ACCEPTANCE.md`) with the server commit and date. You are done when
every step passes.

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

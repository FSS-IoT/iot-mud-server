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

Run one server at a time.

## 3. Read the contract

- `docs/PROTOCOL.md` — the normative v2 contract.
- `docs/SESSION.md` — the exact session your client must complete.
- The server's `tests/test_protocol_v2.py` — a worked client in Python, if you
  want to see the message flow.

## 4. Build the smallest client first

Do not start with the whole game. Do this in order, with a failing check before
each step:

1. Connect and read `hello`; print the MOTD and world name.
2. Send `join` and render `room.state`.
3. Send `input look` and pair the direct reply by `id`.
4. Parse the room: name, description, exits, objects, players.
5. Add movement, then take/drop, then say.
6. Handle `room.enter` / `room.leave`, `player.said`, `object.*`,
   `player.inventory`, `notice`, and `error`.
7. Keep the socket alive after an error or a malformed frame.
8. Handle a dark room (`room.dark: true`) without inventing contents.

Each step is a ticket in your repo. A failing check first (the software
factory's red/green station) is the habit the club is teaching.

## 5. Pass the acceptance session

Start a **fresh server** for the acceptance. The world is in memory and the
session takes the gold coin, unlocks the forest chest, and moves items; a second
run against the same server can fail in ways that look like client bugs. Stop
the old server and start a new one.

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

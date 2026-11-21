# IoT MUD server

The common server for the club IoT MUD. One authoritative engine, one wire
protocol (v2), and a WebSocket terminal. Members write their own clients
against `docs/PROTOCOL.md`; the server is the shared product.

The game runs fully offline on one laptop, with no LLM and no internet.

## Quick start

```bash
uv sync --all-extras
./run_server.sh
# ws://localhost:8765
```

Bind the LAN/tailnet interface or choose another port:

```bash
./run_server.sh --host 0.0.0.0 --port 9000
```

Windows:

```powershell
uv sync --all-extras
uv run python -m src.mud.net.v2_server
# or:
uv run python -m src.mud.net.v2_server --host 0.0.0.0 --port 9000
```

Run one server at a time: the v1 server and the v2 server both default to
port 8765.

## Test

```bash
uv run pytest
```

Stop the server first. The legacy v1 tests bind fixed ports (8765/8766/8769),
so a running server makes them fail with `address already in use`. The suite
includes the two-client v2 conformance session (`tests/test_protocol_v2.py`)
and the engine tests.

## What is here

| Path | What it is |
|---|---|
| `src/mud/engine/` | Authoritative state, commands, event bus |
| `src/mud/net/v2_server.py` | v2 WebSocket server (the common server) |
| `src/mud/net/server.py` | v1 server, kept for history and its tests |
| `tests/` | Server/engine tests; many are historical red/green artifacts. |
| `src/mud/protocol/` | v2 envelope, outbound types, engine adapter |
| `tools/mock_v2_server.py` | Protocol-only mock server for client authors |
| `examples/reference_client.py` | Minimal worked v2 client (not a template) |
| `src/mud/world/` | World loader |
| `data/worlds/starting_village.json` | The demo world |
| `docs/PROTOCOL.md` | The client contract |
| `docs/SESSION.md` | The acceptance session every client passes |
| `BOOTSTRAP.md` | Start your own client |
| `CONTRIBUTING.md` | Fork, test, review, merge |
| `NOTICE.md` | Credit and license status |

The v1 server stays in the tree for history and its tests. The interactive v1
demo (`run_demo.sh` / `scripts/client.py`) is not part of this repo; it lives in
Doc's client repo. The common server is v2.

## Writing a client

Read `BOOTSTRAP.md`. You do not need the server source; you need the protocol
and a local server to test against. Pick any stack. `docs/SESSION.md` is the
definition of done.

## Protocol

Version 2.0. The handshake advertises `protocol: [2]`; a client sends `join`,
then `input` commands, and receives typed events plus a `data.text` line. See
`docs/PROTOCOL.md` for the envelope, events, errors, and the v2.0/v2.1 split.

## Contributing

See `CONTRIBUTING.md`. The short version: fork, branch from `main`, write the
failing test first, run `uv run pytest`, open a PR, and get a fresh review.
Work lands through a PR; do not push to `main` directly.

## Credit and license

The engine, v1 server, world, and v2 adapter derive from
[`FSS-IoT/iot-mud`](https://github.com/FSS-IoT/iot-mud), originally by Doc
(`docx73`). See `NOTICE.md`.

The common server is **MIT**; see [`LICENSE`](LICENSE). The copyright notice
names Doc and the FSS-IoT club contributors. Doc's repository is MIT too, and
the common server's history joins his original commits.

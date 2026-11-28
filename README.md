# IoT MUD server

The common server for the club IoT MUD. One authoritative engine, one wire
protocol (v2), and a WebSocket terminal. Members write their own clients
against `docs/PROTOCOL.md`; the server is the shared product.

The game runs fully offline on one laptop, with no LLM and no internet.

## Quick start

```bash
uv sync --all-extras
uv run python -m src.mud.admin seed --db data/mud.db
./run_server.sh
# ws://localhost:8765
```

Bind the LAN/tailnet interface or choose another port:

```bash
./run_server.sh --host 0.0.0.0 --port 9000
```

Hosting for the club (tailnet, node sharing, ACL): see
[`docs/HOSTING.md`](docs/HOSTING.md). Client onboarding: see
[`docs/CLIENTS.md`](docs/CLIENTS.md). Operations (seed, backup, systemd):
see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

Windows:

```powershell
uv sync --all-extras
uv run python -m src.mud.net.v2_server
# or:
uv run python -m src.mud.net.v2_server --host 0.0.0.0 --port 9000
```

The v2 server defaults to port 8765.

## Portal

`portal/` is the experimental Go proxy for one client-facing tailnet port. It
authenticates nothing yet; backends still validate the character key. The
default path enters the main server. Dev servers are opt-in. See
[`portal/README.md`](portal/README.md).

## Storage

The v2 server creates and migrates `data/mud.db` on startup. Override it with
`--db PATH` or a `mud.toml` in the working directory:

```toml
[storage]
path = "data/mud.db"
```

Create or migrate the database offline with:

```bash
uv run python -m src.mud.admin init-db --db data/mud.db
```

`user_version` is the schema version. A database from a newer server refuses to
start with an actionable message.

## Test

```bash
uv run pytest
```

The suite binds ephemeral ports, so a running server on 8765 does not disturb
it. The suite
includes the two-client v2 conformance session (`tests/test_protocol_v2.py`),
the golden scenarios (`tests/golden/`), and the docs checks
(`tests/test_docs_examples.py`). CI runs `uv run pytest -m "not load"`; the load
smoke runs nightly.

## What is here

| Path | What it is |
|---|---|
| `src/mud/engine/` | Authoritative state, commands, event bus |
| `src/mud/net/v2_server.py` | v2 WebSocket server (the common server) |
| `src/mud/storage/` | SQLite schema, migrations, and the write-through store |
| `src/mud/content/` | YAML world model, validator, and effects |
| `src/mud/world/` | World loader, seeding, geometry, NPC wandering |
| `tests/` | Server/engine tests; `tests/golden/` covers every room and event |
| `src/mud/protocol/` | v2 envelope, outbound types, engine adapter |
| `tools/mock_v2_server.py` | Scripted documentation fixture (not the conformance harness) |
| `examples/reference_client.py` | Minimal worked v2 client (not a template) |
| `data/worlds/club_world/` | The 34-room product world (YAML, one file per area) |
| `docs/PROTOCOL.md` | The v2.1 client contract |
| `docs/CLIENTS.md` | Onboarding, keys, sleep/wake, and client repos |
| `docs/SESSION.md` | The acceptance session every client passes |
| `docs/CONTENT.md` | Authoring YAML rooms, objects, NPCs, and effects |
| `docs/OPERATIONS.md` | Seed, config, backup/restore, systemd, version refusal |
| `docs/HOSTING.md` | Host on a tailnet and share the node with members |
| `BOOTSTRAP.md` | Start your own client |
| `CONTRIBUTING.md` | Fork, test, review, merge |
| `NOTICE.md` | Credit and license status |

The interactive v1 demo (`run_demo.sh` / `scripts/client.py`) is not part of
this repo; it lives in Doc's client repo. The common server is v2. The frozen
five-room conformance
fixture at `data/worlds/starting_village.yaml` (and `.json`) is kept for the
legacy loader tests; it is not the product world.

## Writing a client

Read `BOOTSTRAP.md`. You do not need the server source; you need the protocol
and a local server to test against. Pick any stack. `docs/SESSION.md` is the
definition of done.

## Protocol

Version 2.1 (wire field `v` is `2`). The handshake advertises `protocol: [2]`;
a v2.1 client sends `auth` with a character key, then `input` commands, and
receives typed events plus a `data.text` line. A v2.0 guest `join` remains only
when the server is started with `auth_required = false`. See
`docs/PROTOCOL.md` for the envelope, events, errors, close codes, and the
v2.0/v2.1 changelog.

## Contributing

See `CONTRIBUTING.md`. The short version: fork, branch from `main`, write the
failing test first, run `uv run pytest`, and open a PR. Request a review when
the change is risky or cross-cutting; otherwise the required `test` and
`claim-guard` checks are the gate. Work lands through a PR; do not push to
`main` directly.

## Credit and license

The engine, v1 server, world, and v2 adapter derive from
[`FSS-IoT/iot-mud`](https://github.com/FSS-IoT/iot-mud), originally by Doc
(`docx73`). See `NOTICE.md`.

The common server is **MIT**; see [`LICENSE`](LICENSE). The copyright notice
names Doc and the FSS-IoT club contributors. Doc's repository is MIT too, and
the common server's history joins his original commits.

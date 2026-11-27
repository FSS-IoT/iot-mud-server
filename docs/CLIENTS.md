# Writing a client

The common server is the shared product. Every member writes their own client:
a terminal program, a browser page, a phone app, or a small hardware display.
The server does not ship a required client, and no client's presentation is
part of the contract.

This page is the onboarding path. Read it once, keep `PROTOCOL.md` open while
you code, and treat `SESSION.md` as the definition of done.

- [`PROTOCOL.md`](PROTOCOL.md) — the normative v2.1 wire contract.
- [`SESSION.md`](SESSION.md) — the two-character acceptance session.
- [`CONTENT.md`](CONTENT.md) — the world and its commands, if you also author.
- [`OPERATIONS.md`](OPERATIONS.md) — the host's side, including backup.
- [`../BOOTSTRAP.md`](../BOOTSTRAP.md) — the repo-and-ticket workflow.

## The shortest path

1. Run a server from the server checkout, with a database of its own:

   ```bash
   uv sync --all-extras
   uv run python -m src.mud.admin seed --db .scratch/session.db
   ./run_server.sh --port 8766 --db .scratch/session.db
   ```

   The server prints the URI it starts with. `--port` and `--db` are server
   flags, not protocol fields; use any free port and a new scratch path for
   each acceptance run. The default URI is `ws://localhost:8765`, and 8765 is
   left free for a real server. Run the seed once per database; if the file
   already exists, start the server without reseeding, or delete the file for
   a fresh run. `admin seed --reset` replaces the world but keeps characters
   and keys, so it is not a fresh acceptance.

2. Connect once with the reference client and register a character. The
   server returns the character key in `auth.ok` exactly once; the client
   prints it and can save it:

   ```bash
   uv run python examples/reference_client.py --name Ada \
     --uri ws://localhost:8766 --save-key ~/.config/iot-mud/ada.key
   ```

3. Log in from the saved key. The key is not repeated:

   ```bash
   uv run python examples/reference_client.py \
     --uri ws://localhost:8766 --key-file ~/.config/iot-mud/ada.key
   ```

4. Read `PROTOCOL.md`'s envelope and session lifecycle, then build the
   smallest client that reads `hello`, authenticates, and renders
   `room.state`.
5. Add one command at a time and pair each direct reply by its `id`. Keep the
   socket alive after an `error`.
6. Run the full `SESSION.md` acceptance with two characters. Record the server
   commit and date in your own repo.

The client's `--uri` must name the server's `--port`. A mismatch is a
connection error, not a protocol error; the server cannot tell you which port
you meant to use.

The optional mock server is a scripted documentation fixture for the first
steps, not a conformance implementation:

```bash
uv run python tools/mock_v2_server.py
```

It listens on `ws://localhost:8770` and scripts a small session. It is useful
while the handshake is new; the real server is the authority for acceptance,
and the suite's protocol harness is `tests/golden/harness.py`.

## The envelope in one paragraph

Every frame is one JSON object. A server message has `v: 2`, `type`,
`event_id`, `seq`, `ts`, and `data`; a client message has `v: 2`, `type`, an
optional `id`, and `data`. An `input` **must** carry `id`; the direct reply
echoes it. Broadcasts have no `id`. `data.text` is presentation only and may
change wording; the structured fields are the contract. Ignore unknown event
types and unknown fields. The server sends `hello` immediately on connect, so
a client that is waiting for anything else has already missed a message.

## Your character key

v2.1 requires auth by default. Registration returns `auth.ok.data.key` once:

```text
mud1-<public key id>-<32 bytes of base64url secret>
```

Treat the whole string as opaque.

- **Store it outside the repository.** A file under your home directory with
  mode `0600`, the operating system keyring, or a password manager. Never
  commit it, never paste it into an issue, and never log it. The reference
  client writes that file for you:

  ```bash
  uv run python examples/reference_client.py --name Ada \
    --uri ws://localhost:8766 --save-key ~/.config/iot-mud/ada.key
  ```

  `--save-key` creates the parent directory and writes the key with mode
  `0600`. It refuses to overwrite an existing file, so a stray re-registration
  cannot destroy a working key. On Linux, check it with
  `stat -c %a ~/.config/iot-mud/ada.key`; the answer is `600`.
- **Log in with it, not with the name.** `auth` with `mode: "login"` and
  `key`; the response has no `key` and the same character enters its stored
  room. Read the file instead of putting the key on the command line:

  ```bash
  uv run python examples/reference_client.py \
    --uri ws://localhost:8766 --key-file ~/.config/iot-mud/ada.key
  ```

  `--key "$MUD_KEY"` also works, but the value is visible in shell history and
  the process list. Prefer `--key-file`, or export the variable without
  echoing it.
- **Send it only to the server that issued it.** A key is a bearer secret for
  one character on one database. A different server will reject it.
- **A lost key is replaced offline.** Ask the operator for
  `admin rekey <name>`; the old key is revoked and the new one is printed
  once. There is no self-service rotation in v2.1.
- **Registration names** are 2-24 characters, start with a letter, and
  continue with letters, digits, `_`, or `-`; uniqueness is
  case-insensitive. A bad name is `auth.error{invalid_name}`; a taken one is
  `auth.error{name_taken}`.

Auth failures are limited per connection, never per name. A throttled client
gets `auth.error{rate_limited}` and can keep trying on another connection; it
cannot lock a character out. See `PROTOCOL.md`, "v2.1 auth".

## Session, sleep, and wake

The server keeps the character, not the socket.

- **Disconnect sleeps the character.** An authenticated client that closes the
  socket (or loses the network) leaves the character in its room as
  asleep/stone. Other players see `room.leave` with `reason: "disconnect"`,
  `asleep: true`, and `stone: true`. `who` still lists it.
- **`dropped_on_sleep` items fall into the room.** In the product world those
  are the door keys. Everything else stays in the inventory. A dropped key can
  be picked up by anyone, so a sleeper cannot strand a door key in a pocket.
- **Login wakes the character in place.** The same room, the retained
  inventory, and a fresh `room.state`. Items that dropped when it slept stay
  where they landed.
- **There is no offline queue.** A `tell` to a sleeping character is
  `error{not_awake}`; login does not replay it. Plan for that in your UI.
- **Idle timeout is a sleep.** After `[limits].idle_timeout_seconds` (default
  15 minutes) with no client message the server runs the same sleep path and
  closes the socket with `4004`. Server pushes do not reset the timer. A
  client that wants to stay awake should let the player act; do not invent a
  keepalive that the protocol does not define.
- **Quit is a clean close.** The `quit` message closes with `1000`; the room
  receives the leave. An `input` whose text is `quit` or `exit` is the same,
  but neither sends a direct reply, so do not block waiting for one.

Close codes are listed in `PROTOCOL.md`. `4003` is an unsupported protocol
version, `4004` is the idle sleep, and `4001`, `4002`, and `4005` are reserved.
A normal auth failure, a command rate limit, or an idle sleep never uses
`4005`.

## The world epoch and the server commit

Two fields tell a client whether its cached state is still valid.

- **`hello.data.world.epoch`** changes when the operator replaces the world
  with `admin seed --reset`. It survives a restart against the same database.
  Store the epoch with any cached map, room descriptions, or object list; when
  the value changes, drop the cache and rebuild it from `room.state`.
- **`hello.data.server_commit`** is the 40-character git commit the server was
  built from, or `"unknown"` for a copy without `.git`. Pin a known server
  commit in your client's README and tests; when the running server reports a
  different commit, read the `PROTOCOL.md` changelog for what changed. A
  server that does not send the field is an older v2 build: ignore it and keep
  working.

A reconnect to the same seeded database keeps the epoch. A new database, a
`--reset`, or the in-memory v2.0 guest server changes it.

## What "compatible" means

Your client is compatible when it completes `SESSION.md` with two characters,
survives an error and a malformed frame without disconnecting, and can render
the state it needs from `room.state`. It does **not** need to look like the
reference client.

At minimum it should handle:

| Area | Events |
|---|---|
| Session | `hello`, `auth.ok`, `auth.error`, `error`, `notice` |
| Room | `room.state`, `room.enter`, `room.leave` |
| Objects | `object.taken`, `object.dropped`, `object.opened`, `object.closed`, `object.put`, `object.got`, `object.read`, `object.lit`, `object.unlocked` |
| Locks | `exit.locked`, `exit.unlocked` |
| Social | `player.said`, `player.emoted`, `player.told`, `who.list`, `player.inventory` |
| NPCs | `npc.say` (required), `npc.enter`, `npc.leave` (optional ambient) |
| Effects | `effect` only when `[features].effects = true`; the default is bus/log-only |

The `docs/SESSION.md` session is the acceptance test, not a UI specification.
Map each event to whatever your client shows; the contract is the event, not
the layout.

For `player.inventory`, `capacity`, `used`, and `unit` are always present.
Render the `unit` string verbatim (`"stones"`, `"oz"`, `"tons"`, …) and never
convert; item weights are already in that display unit, so show them as sent.
Do not hardcode `"stones"` — use whatever `unit` the server sends.

## The reference client

`examples/reference_client.py` is a small, readable Python client. It is a
worked example, **not a template**:

- it registers with `--name`, shows the key once, and writes it to `--save-key`
  with mode `0600`;
- it logs in with `--key-file` (or `--key`) and never repeats the key;
- it prints the `hello` summary — protocol, world and epoch, auth mode, and
  `server_commit` — so you can compare against the pin in your README;
- it renders `room.state`, `notice`, `error`, speech, object events, `who.list`,
  `player.inventory`, `npc.say`, and `shutdown`;
- it pairs direct replies by `id`, streams broadcasts separately, prints the
  close code after `quit`, and `!malformed` sends one broken frame to check
  that an error does not close the socket.

It intentionally has no map view, no session persistence beyond the key file,
and no UI framework. Those are the parts you write.

## Your client repository

Clients live in their own repositories. The server repo does not accept client
code, and a client does not vendor the server.

- Start from the docs-only template:
  [`FSS-IoT/iot-mud-client-template`](https://github.com/FSS-IoT/iot-mud-client-template).
  It carries `SESSION.md`, a `PROTOCOL-VERSION` file, and a license, so the
  acceptance script travels with your client.
- Name the repo whatever you like (for example `<name>-mud-client`), private
  is fine, and pick any stack.
- Put these in the README: how to run it, which protocol version it targets,
  and the server commit you accepted against.
- Record the acceptance run in the repo (for example `docs/ACCEPTANCE.md`)
  with the date, the server commit, and any steps you skipped.
- Never commit a key, a server address, or a database file. A server address
  belongs in local configuration.
- If you change the protocol privately, every other client breaks. Propose the
  change in the server repo instead; the protocol is additive within v2.

## Limitations of v2.1

- **No combat.** The engine has no health, damage, or death. Stone/asleep is a
  state flag for a future combat path, nothing more.
- **No offline messages.** `tell` reaches only an awake character. There is no
  queue and login does not replay one.
- **No client-side effects by default.** Authored effects always reach the
  bus and `event_log`; a client sees `effect` only when the operator sets
  `[features].effects = true`.
- **No NPC-initiated interaction.** NPCs answer `talk` and `ask`; they never
  start a conversation, and they do not chase or fight.
- **Lock state is runtime state.** An unlocked door is not written to SQLite;
  a restart rebuilds the authored lock. A client should expect the door to be
  locked again after a restart.
- **One world per database.** The epoch identifies it. There is no sharding,
  no multi-world routing, and no guest-to-character migration.
- **Auth is a bearer key, not a password.** Anyone holding the key is the
  character. TLS is not part of v2.1; the club host uses a tailnet or LAN, and
  a public deployment needs a reviewed TLS/auth design first.
- **`data.text` is not stable.** Do not parse it, translate against it, or
  assert on exact wording in a test. Use the structured fields.
- **The server is authoritative.** No client-side movement, inventory, or
  lock prediction is reconciled by the server; send a command and render the
  reply.

## Getting unstuck

- File an issue on the server repo with the message sequence, the server
  commit, and what you expected.
- Run the same step against `docs/SESSION.md` and the reference client to
  separate a client bug from a server bug.
- Bring it to a club session. The protocol is the shared contract; a client
  bug is yours to fix, a contract gap is a server issue.

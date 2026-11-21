# IoT MUD protocol 2

**Version:** 2.0. This is the normative contract between the common server and
every member client. Additive changes stay inside 2.x; a breaking change
becomes version 3.

**Scope:** v2.0 is parity with the working server. v2.1 adds auth, SQLite,
YAML worlds, social commands, container open state, exit locks, and NPCs. Later
versions add IoT and LLM layers. See `ROADMAP.md` for the split.

## Transport

- WebSocket over plain TCP; one JSON object per text frame; UTF-8; max 64 KiB.
- Endpoint `ws://<host>:<port>`; the port is an implementation detail. The
  reference server uses 8765.
- One socket per client for its whole session.

## Envelope

Server to client:

```json
{
  "v": 2,
  "type": "room.state",
  "event_id": "01J8Z3K9Q7V2M4T6R8X0B1C2D",
  "seq": 12,
  "ts": "2026-11-21T18:04:11.213Z",
  "data": {}
}
```

Client to server:

```json
{
  "v": 2,
  "type": "input",
  "id": "c-7",
  "data": { "text": "take coin" }
}
```

Rules:

- `v` is exactly `2` in this spec.
- `event_id` is an opaque unique string. Clients may ignore it.
- `seq` is the server's per-connection counter. It starts at 1 on `hello` and
  increments for every server message on that connection. It exists for
  ordering, not request pairing.
- `ts` is an ISO 8601 UTC timestamp. Informational.
- `data` is the payload. **`data.text` is presentation only** and may change
  wording at any time; structured fields are the contract. A minimal client can
  print `data.text` and ignore everything else.
- `id` on a client message is an opaque request id (string or integer). A direct
  reply echoes it. Broadcasts have no `id`.
- Clients must ignore unknown event types and unknown fields. Arrays preserve a
  documented order (exits by direction, objects/players by name) so clients can
  render without sorting rules.

## Session lifecycle

1. **Connect.** The server sends `hello` immediately.

```json
{"v":2,"type":"hello","event_id":"h1","seq":1,"ts":"...","data":{
  "server":"iot-mud",
  "protocol":[2],
  "world":{"name":"Starting Village","epoch":"2026-11-21T18:00:00Z"},
  "auth_required":false,
  "motd":"Welcome to the IoT MUD!"
}}
```

2. **Join.** The client sends `join` with a name. This is the v2.0 guest path;
   `hello.auth_required` is `false`.

```json
{"v":2,"type":"join","id":"c-1","data":{"name":"Ada"}}
```

Success: `session.ok` (direct, echoes `id`) followed by `room.state`:

```json
{"v":2,"type":"session.ok","event_id":"s1","seq":2,"ts":"...","data":{
  "player":{"name":"Ada"},
  "world":{"name":"Starting Village","epoch":"..."},
  "mode":"guest",
  "text":"Welcome, Ada."
}}
```

Duplicate name: `error{code:"name_taken"}`, socket stays open.
Missing or non-string name: `error{code:"bad_args"}`.
A second `join` on the same socket: `error{code:"already_joined"}`.

3. **Play.** `input` carries the whole command line; the server parses it.
   Every command produces a direct reply (a typed event with the echoed `id`,
   or `error`) and, when it mutates shared state, one broadcast per other
   recipient.

4. **Quit.** `quit` closes cleanly with code `1000`; the room receives
   `room.leave` with `reason: "disconnect"`. A socket close without `quit` has
   the same effect.

5. **Reconnect.** Joining again sends a fresh `room.state`. In v2.0 the world is
   in memory: a server restart resets it and changes `world.epoch`. Clients
   compare `epoch` and drop cached maps when it changes.

### v2.1 auth (additive)

When `hello.auth_required` is `true`, the client sends `auth` instead of `join`:
`{"mode":"login"|"register","name":"...","password":"..."}`. Success is
`session.ok` with `mode: "login"|"register"`; failure is `auth.error` with a
code (`bad_credentials`, `name_taken`, `unknown_player`, `invalid_name`,
`weak_password`). The v2.0 guest path remains available only when
`auth_required` is `false`.

## Client messages

| Type | Data | Notes |
|---|---|---|
| `join` | `name` | v2.0 guest session. Only before joining. |
| `input` | `text` | The whole command line. Only after joining. |
| `quit` | — | Graceful close. |
| `auth` | `mode`, `name`, `password` | v2.1 only. |

Nothing else exists in v2. Clients do not send structured actions; parsing is
the server's job, so every client sees the same world behaviour.

## Server events

Every event carries `data.text` rendered for that recipient. The structured
fields are identical across recipients; the text is not ("You take the coin."
vs "Ada takes the coin.").

### Session

| Type | Data |
|---|---|
| `hello` | `server`, `protocol`, `world{name,epoch}`, `auth_required`, `motd` |
| `session.ok` | `player{name}`, `world{name,epoch}`, `mode`, `text` |
| `error` | `code`, `message`, `command?`, `candidates?[]` |
| `notice` | `text`, `level` (`info`\|`warning`), `object?` |
| `shutdown` | `reason`, `in_seconds` (v2.1) |

### Room and presence

| Type | Data | Recipients |
|---|---|---|
| `room.state` | `room{...}` | Actor only |
| `room.enter` | `room{id,name}`, `actor{name}`, `direction?`, `reason` (`move`\|`connect`) | Others in the new room |
| `room.leave` | `room{id,name}`, `actor{name}`, `direction?`, `reason` (`move`\|`disconnect`) | Others in the old room |

`room.state.data.room`:

```json
{
  "id": "gate",
  "name": "Village Gate",
  "description": "A heavy stone gate stands tall. To the north is the Marketplace.",
  "dark": false,
  "exits": [
    {"direction": "north", "to": "market", "locked": false}
  ],
  "objects": [
    {"id": "sign", "kind": "sign", "name": "Village Sign", "portable": true, "readable": true}
  ],
  "players": [{"name": "Ben"}],
  "npcs": []
}
```

- `objects[].id` is the instance handle; `kind` is the prototype from the world
  file. When the world has one of each, they are equal. Duplicate instances get
  distinct `id`s (`gold_coin-1`, `gold_coin-2`) so `take coin 2` is possible.
- `players` lists other people in the room, excluding the recipient. The client
  adds its own name from `session.ok`.
- `npcs` is empty in v2.0; the field is reserved.
- In a dark room (`dark: true`), `exits`, `objects`, `players`, and `npcs` are
  empty, and `data.text` says it is too dark to see. Carrying a light source
  (or a lit fixed light) produces the normal state with `dark: false`.
- `room.state` is sent on join, on `look`, and to the actor after a successful
  move. It is the resynchronization mechanism: a client must be able to rebuild
  its view from it.

### Players and social

| Type | Data | Recipients |
|---|---|---|
| `player.inventory` | `items[]{id,name}`, `capacity?`, `used?` | Actor only |
| `player.said` | `from`, `text`, `self` | Room, including actor |
| `player.emoted` | `from`, `text`, `self` | Room (v2.1) |
| `player.told` | `from`, `to`, `text`, `self` | Target and sender (v2.1) |
| `who.list` | `players[]{name,room_id}` | Actor only (v2.1) |

### Objects

| Type | Data | Recipients |
|---|---|---|
| `object.taken` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.dropped` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.opened` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.closed` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.put` | `object{...}`, `container{id,name}`, `by`, `self` | Room |
| `object.got` | `object{...}`, `container{id,name}`, `by`, `self` | Room (v2.1) |
| `object.read` | `object{id,name}`, `content` | Actor only |
| `object.unlocked` | `object{id,kind,name}`, `by`, `self` | Room |

Reserved for later, never emitted in v2.0: `object.locked`, `object.closed`,
`object.got`, `exit.locked`, `exit.unlocked`, `npc.say`, `npc.enter`,
`npc.leave`, `effect`, `device.event`, `narration.append`, `room.generated`.

**Why container locks, not exit locks:** the reference engine has keys and locks on
containers (`use <key> on <chest>`), not on room exits. Agentic-mud's design
locked exits. v2.0 keeps the working semantics; exit locks and `close` arrive
with v2.1, where the command set grows.

### Direct replies and broadcasts

- A command's **direct reply** echoes the request `id`. For a mutation, the
  actor's copy of the event is the direct reply: it has the `id`, `self: true`,
  and the actor's text. Other recipients get `self: false`, no `id`, and the
  third-person text.
- Follow-up state pushes (`player.inventory` after `take`, `room.state` after
  `session.ok`) have no `id`; they are state, not the reply.
- `notice` and `error` are private and echo the request `id`.
- A command that changes nothing shared (look, examine, read, inventory, help,
  exits) produces a private event only.
- A malformed message produces `error` and keeps the socket open. It never
  closes the connection.

## Error codes

`unknown_command`, `bad_args`, `ambiguous_target`, `not_found`, `not_here`,
`not_portable`, `container_closed`, `no_exit`, `locked`, `already_open`,
`already_closed`, `malformed_message`, `unknown_message`, `not_joined`,
`already_joined`, `name_taken`, `not_authenticated`, `rate_limited`, `internal`,
`unsupported_version`.

An `error` is non-fatal unless the code says otherwise; clients print
`message` and continue.

Close codes: `1000` normal, `1001` going away, `1002` protocol error, `1008`
policy violation, `4001` auth timeout, `4002` auth failures, `4003` unsupported
protocol version, `4004` idle timeout, `4005` rate-limit abuse. v2.0 uses
`1000`, `1001`, `1002`, `1008`, and `4003`; the rest arrive with v2.1.

## Command to event map (v2.0 parity)

| Command | Result |
|---|---|
| `join <name>` | `session.ok` + `room.state` |
| `look` | `room.state` (private) |
| `look at <x>` / `examine <x>` | `notice` (private; optional `object`) |
| `look in <container>` | `notice` (private; container contents) |
| `exits` | `notice` (private) |
| `go <dir>` / `n`/`s`/`e`/`w`/`u`/`d` | actor: `room.state`; others: `room.leave` then `room.enter` |
| `take <x>` / `get <x>` | `object.taken` (actor `self:true` + room `self:false`) + `player.inventory` |
| `drop <x>` | `object.dropped` + `player.inventory` |
| `put <x> in <y>` | `object.put` + `player.inventory` |
| `open <x>` | `object.opened` (container contents in text) |
| `use <key> on <container>` | `object.unlocked` (or `error{locked}` / `error{already_open}`) |
| `read <x>` | `object.read` (private) |
| `inventory` / `i` | `player.inventory` (private) |
| `help` / `h` | `notice` (private) |
| `say <text>` | actor: `player.said` (`self:true`); room: `player.said` (`self:false`) |
| `hello` | `notice` (private) |
| `exit` / `quit` | close `1000`; `room.leave` to the room |
| malformed / unknown | `error`; socket stays open |

## Appendix: changes from version 1

| v1 | v2 |
|---|---|
| `v: 1` | `v: 2` |
| `join` (silent) | `join` → `session.ok` + `room.state`; duplicate → `error{name_taken}` |
| `input` → `command_reply{text}` | `input` → typed event or `notice`/`error`; `data.text` still prints |
| `seq` echoes client id | `id` echoes client id; `seq` is the server counter |
| `room_state` | `room.state` (structured) |
| `player_joined` | `room.enter` |
| `player_left` | `room.leave` |
| `player_said` | `player.said` |
| no structured object events | `object.*`, `player.inventory` (container unlock is `object.unlocked`) |
| malformed input closes the socket | `error{malformed_message}`; socket stays open |

## Conformance session

The scripted acceptance session lives in `SESSION.md`. A client is compatible
when it completes that session; `tests/test_protocol_v2.py` implements the same
script against the server.

## Not in version 2.0

Auth/passwords, SQLite persistence, YAML worlds, `who`/`emote`/`tell`, `close`,
`get from`, exit locks, an explicit container `open` flag, and NPCs are v2.1.
They are listed in `ROADMAP.md`; nothing in this contract reserves their names
in a way that would break an additive release.

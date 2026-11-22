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
  "server":"iot-mud-server",
  "protocol":[2],
  "world":{"name":"Starting Village","epoch":"2026-11-21T18:00:00Z"},
  "auth_required":false,
  "motd":"Welcome to the IoT MUD!",
  "text":"Welcome to the IoT MUD!"
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
   `room.leave` with `reason: "disconnect"`. It is a session-level exception to
   the direct-reply rule: it does not send a reply. An `input` whose text is
   `quit` or `exit` does the same. A socket close without `quit` also produces
   `room.leave`.

5. **Reconnect.** Joining again sends a fresh `room.state`. In v2.0 the world is
   in memory: a server restart resets it and changes `world.epoch`. Clients
   compare `epoch` and drop cached maps when it changes.

### v2.1 auth (additive)

When `hello.auth_required` is `true`, the client sends `auth` instead of
`join`. `hello.auth_modes` lists the accepted methods; a v2.1 server sends
`["key"]`.

Register with a name; the server returns the key exactly once:

```json
{"v":2,"type":"auth","id":"c-1","data":{"mode":"register","name":"Ada"}}
```

Success is `auth.ok` (direct, echoes `id`) with `player`, `world`,
`mode: "register"`, and `key`, followed by `room.state`. Login uses the stored
key and returns no key:

```json
{"v":2,"type":"auth","id":"c-2","data":{"mode":"login","key":"mud1-..."}}
```

Success is `auth.ok` with `player`, `world`, and `mode: "login"`, followed by
`room.state`. The key is an opaque, high-entropy bearer secret: clients store
it outside the repository, never log it, and never send it to a different
server. The server stores only a key id and a one-way hash, never the key, and
returns it only in the registration response. A lost key is replaced with
`admin rekey`, which revokes the old key.

Failure is `auth.error` (direct, echoes `id`) with a code (`invalid_key`,
`name_taken`, `invalid_name`). `invalid_key` covers an unknown or revoked key,
deliberately. Auth attempts are rate-limited; names are not locked.

The v2.0 guest path remains available only when `auth_required` is `false`. A
`join` on a server that requires auth gets `not_authenticated`, and the socket
stays open.

## Client messages

| Type | Data | Notes |
|---|---|---|
| `join` | `name` | v2.0 guest session. Only before joining. `id` is optional; if present it is echoed on `session.ok`. |
| `input` | `text` | The whole command line. Only after joining. **`id` is required**; it is echoed on the direct reply, and an input without one is rejected with `error{code:"bad_args"}`. |
| `quit` | — | Graceful close; the socket closes with `1000`. No direct reply. |
| `auth` | `mode`, `name` (register), `key` (login) | v2.1 only. Only before joining. |

Nothing else exists in v2. Clients do not send structured actions; parsing is
the server's job, so every client sees the same world behaviour. An `input`
whose text is `quit` or `exit` is also a graceful close; like the `quit`
message, it is a session-level exception and does not send a direct reply.

## Server events

Every event carries `data.text` rendered for that recipient. The structured
fields are identical across recipients; the text is not ("You take the coin."
vs "Ada takes the coin.").

### Session

| Type | Data |
|---|---|
| `hello` | `server`, `protocol`, `world{name,epoch}`, `auth_required`, `auth_modes?`, `motd`, `text` |
| `session.ok` | `player{name}`, `world{name,epoch}`, `mode`, `text` |
| `auth.ok` | `player{name}`, `world{name,epoch}`, `mode` (`register`\|`login`), `key?` (v2.1; register only) |
| `auth.error` | `code`, `message`, `text` (v2.1) |
| `error` | `code`, `message`, `text`, `command?`, `candidates?[]` |
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
  An instance id is stable for the life of the object, including when it moves
  between a room and an inventory.
- `objects` and `players` are sorted by name; `exits` by direction.
- `portable` is always `true` for v2.0 content; the field is reserved for
  future non-portable objects.
- `players` lists other people in the room, excluding the recipient. The client
  adds its own name from `session.ok`.
- `npcs` is empty in v2.0; the field is reserved.
- In a dark room (`dark: true`), `exits`, `objects`, `players`, and `npcs` are
  empty, and `data.text` says it is too dark to see. A carried light source must
  be lit (`light <source>`) before it produces the normal state with
  `dark: false`; a lit fixed light does so without a command.
- A `light_source` object carries `lit: true|false`. An unlit source is dark.
- `room.state` is sent on join, on `look`, and to the actor after a successful
  move. It is the resynchronization mechanism: a client must be able to rebuild
  its view from it.

### Players and social

| Type | Data | Recipients |
|---|---|---|
| `player.inventory` | `items[]{id,name,lit?}`, `capacity?`, `used?` | Actor only |
| `player.said` | `from`, `text`, `self` | Room, including actor |
| `player.emoted` | `from`, `text`, `self` | Room (v2.1) |
| `player.told` | `from`, `to`, `text`, `self` | Target and sender (v2.1) |
| `who.list` | `players[]{name,room_id}` | Actor only (v2.1) |

`player.said.text` is the rendered line (`Ada says, "hello".`), not the raw
utterance. A structured raw-message field is a v2.1 candidate; v2.0 clients
should treat `text` as presentation.

### Objects

| Type | Data | Recipients |
|---|---|---|
| `object.taken` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.dropped` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.opened` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.put` | `object{...}`, `container{id,name}`, `by`, `self` | Room |
| `object.got` | `object{...}`, `container{id,name}`, `by`, `self` | Room (v2.1) |
| `object.read` | `object{id,name}`, `content` | Actor only |
| `object.lit` | `object{id,kind,name,lit}`, `by`, `self` | Room |
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
- A malformed message produces `error` and keeps the socket open. A missing
  `v`, a non-object `data`, and missing fields are malformed. The one exception
  is a **present but unsupported `v`**: it returns
  `error{code:"unsupported_version"}` and closes `4003`.

## Error codes

`unknown_command`, `bad_args`, `ambiguous_target`, `not_found`, `not_here`,
`not_portable`, `container_closed`, `no_exit`, `locked`, `already_open`,
`already_closed`, `malformed_message`, `unknown_message`, `not_joined`,
`already_joined`, `name_taken`, `not_authenticated`, `invalid_key`,
`invalid_name`, `rate_limited`, `internal`, `unsupported_version`.

An `error` is non-fatal unless the code says otherwise; clients print
`message` and continue.

Close codes: `1000` normal, `1001` going away, `1002` protocol error, `1008`
policy violation, `4001` auth timeout, `4002` auth failures, `4003` unsupported
protocol version, `4004` idle timeout, `4005` rate-limit abuse. v2.0 uses
`1000`, `1001`, `1002`, `1008`, and `4003`; the rest arrive with v2.1.

## Command to event map (v2.0 parity)

| Command | Result |
|---|---|
| `look` | `room.state` (private) |
| `look at <x>` / `examine <x>` | `notice` (private; optional `object`) |
| `look in <container>` | `notice` (private; container contents) |
| `exits` | `notice` (private) |
| `go <dir>` / `n`/`s`/`e`/`w`/`u`/`d` | actor: `room.state`; others: `room.leave` then `room.enter` |
| `take <x>` / `get <x>` | `object.taken` (actor `self:true` + room `self:false`) + `player.inventory` |
| `drop <x>` | `object.dropped` + `player.inventory` |
| `put <x> in <y>` | `object.put` + `player.inventory` |
| `open <x>` | `object.opened` (container contents in text; an empty unlocked container also emits it) |
| `use <key> on <container>` | `object.unlocked` (or `error{locked}` / `error{already_open}`) |
| `light <source>` | `object.lit` (or `notice` if already lit / not a light) |
| `read <x>` | `object.read` (private) |
| `inventory` / `i` | `player.inventory` (private) |
| `help` / `h` | `notice` (private) |
| `say <text>` | actor: `player.said` (`self:true`); room: `player.said` (`self:false`) |
| `hello` | `notice` (private) |
| `exit` / `quit` | close `1000`; no direct reply; `room.leave` to the room |
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

Auth/character keys, SQLite persistence, YAML worlds, `who`/`emote`/`tell`,
`close`, `get from`, exit locks, an explicit container `open` flag, and NPCs are
v2.1.
They are listed in `ROADMAP.md`; nothing in this contract reserves their names
in a way that would break an additive release.

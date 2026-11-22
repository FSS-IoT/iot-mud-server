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
  "server_commit":"0123456789abcdef0123456789abcdef01234567",
  "auth_required":true,
  "auth_modes":["key"],
  "motd":"Welcome to the IoT MUD!",
  "text":"Welcome to the IoT MUD!"
}}
```

`hello.data.server_commit` is the 40-character lowercase hex git commit the
running server was built from, so a client that pins a commit can compare it
with its own and read the [changelog](#changelog) for what it is missing. The
field is additive; v2.0 clients ignore it. The server resolves it in this
order: an explicit value injected at startup (tests and embedders),
`MUD_SERVER_COMMIT` from the environment, then `git rev-parse HEAD` at the
repository root. Only a 40-character lowercase hex commit counts; any other
value is skipped and the next source is tried. A copy without `.git` (a Nix
store path, an exported tarball) still serves and reports `"unknown"`. The
lookup is cached for the life of the process, not repeated per connection.

2. **Authenticate or join.** A v2.1 server requires auth by default
   (`hello.auth_required` is `true`): the client sends `auth` and receives
   `auth.ok` followed by `room.state` (see [v2.1 auth](#v21-auth-additive)).
   The v2.0 guest path below works only when `hello.auth_required` is `false`.

   A `join` on a server that requires auth is rejected with
   `error{code:"not_authenticated"}`, and the socket stays open so the client
   can authenticate on it. The same error answers `input` before auth.

   Guest `join`:

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
On a guest server, `input` before `join` is `error{code:"not_joined"}`.

3. **Play.** `input` carries the whole command line; the server parses it.
   Every command produces a direct reply (a typed event with the echoed `id`,
   or `error`) and, when it mutates shared state, one broadcast per other
   recipient.

4. **Quit.** `quit` closes cleanly with code `1000`; the room receives
   `room.leave` with `reason: "disconnect"`. It is a session-level exception to
   the direct-reply rule: it does not send a reply. An `input` whose text is
   `quit` or `exit` does the same. A socket close without `quit` also produces
   `room.leave`.

5. **Reconnect.** Authenticating again sends a fresh `room.state`. The world
   and its epoch live in SQLite: a client that reconnects to the same seeded
   database keeps the same `world.epoch`, so its cached map is still valid.
   `admin seed --reset` replaces the world and changes `epoch`; clients compare
   the value and drop cached maps when it changes. A v2.0 in-memory server
   resets on restart and changes the epoch every time.

### v2.1 auth (additive)

When `hello.auth_required` is `true`, the client sends `auth` instead of
`join`. This is the v2.1 server default; a server started for guest play sets
`auth_required` to `false` explicitly. `hello.auth_modes` lists the accepted
methods; a v2.1 server sends `["key"]`.

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
`room.state`; a login response never carries `key`. The request has no name:
the public key id selects the stored row and the secret is checked against the
scrypt hash in constant time, so a failed login never says whether a character
name exists. The character enters its stored room; a character with no stored
room, or one whose room is not in the loaded world, enters at the world start
room.

Login also wakes a sleeping character in place: the durable `players.asleep`
flag clears, the stone state ends, and the character keeps the inventory it
carried, except items marked `dropped_on_sleep`, which went to its room when it
slept. No offline queue is created.

The key is an opaque, high-entropy bearer secret: clients store it outside the
repository, never log it, and never send it to a different server. The server
stores only a key id and a one-way hash, never the key, and returns it only in
the registration response; it never logs a key and never echoes one back. A
lost key is replaced with `admin rekey`, which revokes the old key.

A key is `mud1-`, a public key id, a `-`, then 32 bytes of secret entropy in
base64url. Clients treat the whole string as opaque. The key id selects the
stored row; the salt and scrypt hash are the only secret-derived values on
disk.

Registration names are 2-24 characters, start with a letter, and continue with
letters, digits, `_`, or `-`; surrounding whitespace is trimmed and uniqueness
is case-insensitive. A bad name is `auth.error{invalid_name}`; a name already
registered is `auth.error{name_taken}`.

Failure is `auth.error` (direct, echoes `id`) with a code (`invalid_key`,
`name_taken`, `invalid_name`). `invalid_key` covers a malformed key, an unknown
key id, a revoked key, and a wrong secret, deliberately: the code and message
are identical in every case. Logging in while that character already has a live
session is `name_taken`.

Auth attempts are rate-limited **per connection, never per name**. Each failed
auth exchange (`invalid_key`, `invalid_name`, `name_taken`, or `bad_args`)
counts against a budget. After `[auth].max_attempts` failures (default `20`)
inside `[auth].window_seconds` (default `60`), further `auth` messages on that
connection get `auth.error{rate_limited}` without reaching the database. The
socket stays open, and the budget is empty again once the oldest counted
failure is a window old. A different connection is unaffected: a throttled
client can neither lock a character name nor stop its owner from logging in.
No name, lock, or attempt state is stored in SQLite.

The limits are set in `mud.toml`:

```toml
[auth]
max_attempts = 20
window_seconds = 60
```

`max_attempts` must be a positive integer and `window_seconds` a positive
finite number. `rate_limited` is non-fatal: the server does not close the
socket for a bad key or for reaching the limit. Close code `4005` is reserved
for deliberate policy abuse and is not used for a normal failed attempt.

The v2.0 guest path remains available only when `auth_required` is `false`. A
`join` on a server that requires auth gets `not_authenticated`, and the socket
stays open; so does `input` before auth. After a successful `auth`, `input`
works without a separate `join`.

## Client messages

| Type | Data | Notes |
|---|---|---|
| `join` | `name` | v2.0 guest session. Only before joining. `id` is optional; if present it is echoed on `session.ok`. |
| `input` | `text` | The whole command line. Only after joining or authenticating. **`id` is required**; it is echoed on the direct reply, and an input without one is rejected with `error{code:"bad_args"}`. |
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
| `hello` | `server`, `protocol`, `world{name,epoch}`, `server_commit`, `auth_required`, `auth_modes?`, `motd`, `text` |
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
| `room.leave` | `room{id,name}`, `actor{name}`, `direction?`, `reason` (`move`\|`disconnect`), `asleep?`, `stone?`, `dropped?[]` | Others in the old room |

A `room.leave` with `reason: "disconnect"` from an authenticated session is a
sleep, not a removal: `asleep` and `stone` are `true`, and `dropped` lists the
objects that left the character's inventory (`object{id,name}`). The character
stays in the room and appears in the next `room.state`.

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
    {"id": "sign", "kind": "sign", "name": "Village Sign", "portable": true, "readable": true},
    {"id": "chest_forest", "kind": "chest_forest", "name": "Chest", "portable": true, "container": true, "open": false}
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
- `portable` reflects the world file: `true` by default, `false` for an object
  a player cannot lift (`get <item> from <container>` refuses it with
  `error{not_portable}`). v2.0 content is portable, so the field's value does
  not change the v2.0 commands.
- `container: true` marks an object that holds contents; `open` is its lid
  state (v2.1, additive). A container authored with `state.open: true` starts
  open. `open`, `close`, and `get <item> from <container>` read the flag.
- `players` lists other people in the room, excluding the recipient. The client
  adds its own name from `session.ok`. An awake occupant keeps the v2.0 shape
  `{"name": "Ben"}`. A sleeping occupant adds `asleep: true` and `stone: true`:
  the character is still in the room, invincible, and not a valid combat
  target. No combat consumes the flag yet.
- `npcs` is empty in v2.0; the field is reserved.
- In a dark room (`dark: true`), `exits`, `objects`, `players`, and `npcs` are
  empty, and `data.text` says it is too dark to see. A carried light source must
  be lit (`light <source>`) before it produces the normal state with
  `dark: false`; a lit fixed light does so without a command.
- A `light_source` object carries `lit: true|false`. An unlit source is dark.
- `room.state` is sent on join, on `look`, and to the actor after a successful
  move. It is the resynchronization mechanism: a client must be able to rebuild
  its view from it.

### Optional room geometry (additive, 2.x)

A room may carry a map for clients that draw one. The field is additive: a
client that ignores it, and a room without it, behave exactly as in v2.0.
`data.text` is unchanged. A room with no map omits the field; it is never
`null`.

```json
"room": {
  "id": "gate",
  "name": "Village Gate",
  "description": "...",
  "dark": false,
  "exits": [{"direction": "north", "to": "market", "locked": false}],
  "objects": [],
  "players": [],
  "npcs": [],
  "geometry": {
    "cells": [
      "###+###",
      "#.....#",
      "#.....#",
      "#.....#",
      "#.....#",
      "#.....#",
      "#######"
    ],
    "spawn": {"north": [3, 1]},
    "props": [
      {"kind": "gatepost", "cell": [1, 5]},
      {"kind": "banner", "cell": [5, 1]}
    ]
  }
}
```

- `cells` are rows from the top, all the same length, at least 3x3. A cell is
  `[x, y]`: `x` is the column from the left, `y` the row from the top, both
  zero-based. `#` is wall, `.` floor, `+` door, `^` stairs up, `v` stairs down.
- A `+` sits on a boundary edge, and the edge names its direction: top north,
  bottom south, left west, right east. There is exactly one door per cardinal
  exit, no door without an exit, and no door in a corner. The cell just inside
  each door is floor.
- Every boundary cell is a wall or a door; stairs and props are interior.
- `^` and `v` mark the stairs for an `up`/`down` exit. A room with that exit
  has exactly one; a room without it has none. Stairs are walkable floor cells.
- `spawn` is optional. It maps an exit side to the floor cell where a client
  should place a player arriving through that exit. If a side is absent, the
  client uses the floor cell just inside that side's door. Spawn cells are in
  bounds and on floor, and name an exit the room has.
- `props` holds static decor: `{kind, cell}`, where `kind` is a non-empty
  string. A prop is in bounds, on floor, and not on a spawn cell. Props are
  decoration only; dynamic objects, players, and NPCs are not positioned here
  (`objects`, `players`, and `npcs` remain the source for those). A server that
  supports geometry always sends `props`, empty when the room has no decor;
  `spawn` is present only when the world author declared it.
- The loader validates every map at startup and refuses to serve a world with
  an invalid map. The error names the room and the offending cell.
- In a dark room (`dark: true` in the emitted `room` object, so the observer
  cannot see it), `geometry` is omitted along with the exits and contents. When
  a light makes the room visible, the normal state is sent with `dark: false`
  and may include the geometry.

Geometry changes no event type and no existing field; `v` stays `2`. It is an
additive 2.x field beyond the v2.0 baseline, not a new protocol version. A
client that does not understand it simply ignores the field, as it does for any
unknown field.

### Players and social

| Type | Data | Recipients |
|---|---|---|
| `player.inventory` | `items[]{id,name,lit?}`, `capacity?`, `used?` | Actor only |
| `player.said` | `from`, `text`, `self` | Room, including actor |
| `player.emoted` | `from`, `text`, `self` | Room, including actor (v2.1) |
| `player.told` | `from`, `to`, `text`, `self` | Target and sender (v2.1) |
| `who.list` | `players[]{name,room_id,asleep,stone}` | Actor only (v2.1) |

`who.list` is the server-wide roster, including the requester and characters
whose session ended. `room_id` is the durable room id (or `null` for a guest
with no room). `asleep` and `stone` are the same state `room.state` exposes: a
sleeping character is present and invincible. The roster reads the durable
`players` rows, so a character that slept before a restart is still listed
asleep in its last room.

`player.emoted` is `emote <action>` (alias `me`): a room-local third-person
action. The actor and every other player in the room receive the same rendered
`text` (`Ada waves at the sky.`); `self` is `true` for the actor. No player
outside the room receives it.

`player.told` is `tell <player> <text>`: a private line to an awake character
anywhere in the world. The sender receives `self: true` with
`You tell Ben, "hello there".`; the target receives `self: false` with
`Ada tells you, "hello there".`; nobody else in either room receives it. A
target that is asleep or stone is `error{not_awake}` and the tell is dropped:
the protocol has no offline queue, and login does not replay it. An unknown
name is `error{not_found}`.

`player.said.text` is the rendered line (`Ada says, "hello".`), not the raw
utterance. A structured raw-message field is a v2.1 candidate; v2.0 clients
should treat `text` as presentation.

### Sleep and stone (v2.1)

An authenticated disconnect does not remove the character. The server commits
`players.asleep = 1` and `players.last_seen`, keeps the character in its room,
and broadcasts `room.leave` with `reason: "disconnect"`, `asleep: true`,
`stone: true`, and `dropped[]`. Items authored `dropped_on_sleep: true` (the
gold key in the starting village) are committed to the room through the
write-through store before memory moves, so another player can pick them up.
Everything else stays in the character's inventory and wakes with it. No
offline queue is created.

A sleeping character is stone: present, invincible, and not a valid target for
future combat. `who.list` and `room.state` player entries expose the state.
Login clears `players.asleep`, sends `room.state`, and restores the remaining
inventory. `dropped_on_sleep` items that were dropped stay where they landed.
The v2.0 guest path (`auth_required: false`, no durable character) keeps the
remove-on-disconnect behavior.

### Objects

| Type | Data | Recipients |
|---|---|---|
| `object.taken` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.dropped` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.opened` | `object{id,kind,name}`, `by`, `self` | Room |
| `object.closed` | `object{id,kind,name}`, `by`, `self` | Room (v2.1) |
| `object.put` | `object{...}`, `container{id,name}`, `by`, `self` | Room |
| `object.got` | `object{...}`, `container{id,name}`, `by`, `self` | Room (v2.1) |
| `object.read` | `object{id,name}`, `content` | Actor only |
| `object.lit` | `object{id,kind,name,lit}`, `by`, `self` | Room |
| `object.unlocked` | `object{id,kind,name}`, `by`, `self` | Room |

`open <container>` sets the container's lid state and lists what is inside in
the reply text. The contents stay inside; since v2.1 (#76) opening no longer
spills loot into the room, so a plain `take` cannot reach it. `get <item> from
<container>` pulls one item out; `close <container>` shuts the lid.

`close <container>` closes an open container and emits `object.closed` to the
actor (`self: true`) and the room (`self: false`). Closing an already closed
container is `error{already_closed}`; a plain item is `error{bad_args}`.

`get <item> from <container>` moves one item out of an **open** container into
the actor's inventory and emits `object.got` to the actor and the room, then a
`player.inventory` state push to the actor (the same follow-up as `take`). A
closed container is `error{container_closed}`, an item that is not inside is
`error{not_here}`, a fixed (`portable: false`) object is
`error{not_portable}`, and a missing container is `error{not_found}`. The item
is resolved by display name inside the named container, so two coins can be
disambiguated with the usual `coin 1` form. A bare `get <item>` remains the
v2.0 alias for `take`.

Reserved for later, never emitted in v2.0: `object.locked`,
`exit.locked`, `exit.unlocked`, `npc.say`, `npc.enter`,
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
`invalid_name`, `not_awake`, `rate_limited`, `internal`,
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
| `look` | `room.state` (private) |
| `look at <x>` / `examine <x>` | `notice` (private; optional `object`) |
| `look in <container>` | `notice` (private; container contents) |
| `exits` | `notice` (private) |
| `go <dir>` / `n`/`s`/`e`/`w`/`u`/`d` | actor: `room.state`; others: `room.leave` then `room.enter` |
| `take <x>` / `get <x>` | `object.taken` (actor `self:true` + room `self:false`) + `player.inventory` |
| `drop <x>` | `object.dropped` + `player.inventory` |
| `put <x> in <y>` | `object.put` + `player.inventory` |
| `open <x>` | `object.opened` (contents listed in text and kept inside; an empty unlocked container also emits it; an open one is `error{already_open}`) |
| `close <x>` | `object.closed` (or `error{already_closed}` / `error{bad_args}` for a non-container) |
| `get <x> from <y>` | `object.got` (actor `self:true` + room `self:false`) + `player.inventory`; closed container `error{container_closed}`, item not inside `error{not_here}`, fixed item `error{not_portable}` |
| `use <key> on <container>` | `object.unlocked` (or `error{locked}` / `error{already_open}`) |
| `light <source>` | `object.lit` (or `notice` if already lit / not a light) |
| `read <x>` | `object.read` (private) |
| `inventory` / `i` | `player.inventory` (private) |
| `who` | `who.list` (private, server-wide, includes asleep) |
| `help` / `h` | `notice` (private) |
| `say <text>` | actor: `player.said` (`self:true`); room: `player.said` (`self:false`) |
| `emote <action>` / `me <action>` | actor: `player.emoted` (`self:true`); room: `player.emoted` (`self:false`); no other room |
| `tell <player> <text>` | sender and awake target: `player.told` (`self:true`/`false`), no room broadcast; asleep/stone target: `error{not_awake}`; unknown name: `error{not_found}` |
| `hello` | `notice` (private) |
| `exit` / `quit` | close `1000`; no direct reply; `room.leave` to the room |
| malformed / unknown | `error`; socket stays open |

## Changelog

Every change to the wire contract gets an entry here, newest version first.
`v` stays `2` while the changes are additive; a breaking change becomes
version 3. A client that pins a server commit reads this section together with
`hello.data.server_commit` to see what it may be missing. v2.1 is the
in-progress release; its entry grows as each documented addition lands.

### v2.1 — protocol 2

- `hello.data.server_commit`: the commit the server was built from, or
  `"unknown"`. Additive; v2.0 clients ignore it.
- `hello.data.auth_required` defaults to `true` in v2.1, with
  `hello.data.auth_modes`, the `auth` client message, the `auth.ok` and
  `auth.error` events, and one-time character keys. A `join` or `input` before
  auth is `error{not_authenticated}` and keeps the socket open; the v2.0 guest
  path is an explicit `auth_required: false` opt-out.
- Registration names are 2-24 characters, starting with a letter; uniqueness
  is case-insensitive. Bad names are `auth.error{invalid_name}` and duplicate
  names `auth.error{name_taken}`.
- Auth attempts are limited per connection, never by name: after
  `[auth].max_attempts` failures (default 20) in `[auth].window_seconds`
  (default 60), the connection gets `auth.error{rate_limited}` and stays open;
  another connection can still register or log in.
- Login with a character key returns `auth.ok` without the key and enters the
  character's stored room; a character with no stored room enters at the world
  start room.
- `world.epoch` is stored with the seeded world: it survives a server restart
  against the same database and changes when `admin seed --reset` replaces the
  world.
- `player.emoted`, `player.told`, `who.list`, and `shutdown`.
- `emote <action>` (alias `me`) is room-local; `tell <player> <text>` is
  private to the sender and an awake target, with no room broadcast and no
  offline queue. A sleeping/stone target is `error{not_awake}`; an unknown
  name is `error{not_found}`.
- `who` lists the server-wide durable roster (`who.list`), including the
  requester, with `asleep` and `stone`; an authenticated disconnect sleeps the
  character in place, drops `dropped_on_sleep` items into its room, and login
  wakes it with the rest of its inventory.
- Containers carry an explicit `open` state, authored as `state.open` and
  exposed as `objects[].open` (additive). `open <container>` lists the contents
  and keeps them inside (v2.0 spilled them into the room; that behavior is
  replaced). `close <container>` emits `object.closed`; `get <item> from
  <container>` emits `object.got` plus the `player.inventory` follow-up and
  requires an open container. Typed errors: `container_closed`,
  `not_portable`, `not_here` (item not inside), `already_closed`, and
  `already_open` for `open`. Opening an already open container no longer
  repeats the open.
- `room.state` player entries and `room.leave` carry `asleep`/`stone` (and
  `dropped[]` on a sleep) for a stone occupant.
- `room.state.data.room.geometry`: the optional room map, with its validation
  rules.
- Error codes `invalid_key`, `invalid_name`, `not_authenticated`,
  `not_awake`, and `rate_limited`; close codes `4001`, `4002`, `4004`, and
  `4005`.

### v2.0 — protocol 2

- Baseline: envelope, `hello`/`session.ok`, `join`/`input`/`quit`,
  `room.state`/`room.enter`/`room.leave`, `player.said`, the v2.0 `object.*`
  events, `player.inventory`, `notice`, `error`, and the command map.

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

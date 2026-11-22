# Client acceptance session

This is the script every member client should complete before it is considered
working. It is language-agnostic: your client can be a terminal app, a browser
page, a phone app, or anything else that speaks WebSocket JSON.

The server's `tests/test_protocol_v2.py` covers the core of this sequence
(presence, movement, objects, chat, errors, and the close code); this page is
the full client acceptance. Containers and locks are covered by the adapter
tests and the optional section at the end.

## Before you start

1. Run the server locally (`../BOOTSTRAP.md` shows how). Start it fresh for the
   acceptance; the session changes the world. A v2.1 server requires auth, so
   have two character names ready and somewhere outside the repository to keep
   the keys.
2. Read `PROTOCOL.md` version 2, especially "v2.1 auth".
3. Have two copies of your client ready, or two terminals, so you can see
   another player.

## Room geometry is optional

The start rooms may include `room.state.data.room.geometry`, a small grid map
for clients that draw a view (`PROTOCOL.md`, "Optional room geometry"). It is
additive: ignore the field and the acceptance session is unchanged, because
`data.text` remains the contract. A room without a map omits the field, and a
dark room omits it until a light makes the room visible.

## The session

Use two characters, **Ada** and **Ben**.

1. **Connect.** Both clients receive `hello` with `v: 2`, `data.protocol`
   containing `2`, `data.world.epoch`, `data.auth_required: true`, and
   `data.auth_modes: ["key"]`.
2. **Register.** Ada sends `auth` with
   `{"mode":"register","name":"Ada"}`; Ben sends the same with `"Ben"`. Each
   receives `auth.ok` followed by `room.state`. `auth.ok.data.key` is the
   character key: show it once, store it outside the repository, and never log
   it or send it anywhere else. Ben's state lists Ada under `room.players`;
   Ada's lists no one yet because Ben registers second. When Ben registers,
   Ada receives `room.enter`. On a later connection, send
   `{"mode":"login","key":"mud1-..."}` instead: `auth.ok` returns the same
   character without repeating the key, followed by `room.state`.
3. **Look.** Ada sends `input` `look`. Her new `room.state` now lists Ben under
   `room.players`.
4. **Move.** Ada sends `go north`. Ada receives `room.state` for the
   Marketplace. Ben, still at the gate, receives `room.leave` with
   `actor.name: "Ada"` and `reason: "move"`.
5. **Arrive.** Ben sends `go north`. Ben receives `room.state` for the
   Marketplace. Ada, already there, receives `room.enter` with
   `actor.name: "Ben"` and `reason: "move"`.
6. **Take.** Ada sends `take gold coin`. Ada receives `object.taken` with
   `self: true` and the object, then `player.inventory` with the coin. Ben
   receives `object.taken` with `self: false` and a third-person line.
   Commands resolve **display names**; `object.id` is `gold_coin`, but the
   command is `take gold coin`, not `take gold_coin`.
7. **Drop.** Ada sends `drop gold coin`. Both receive `object.dropped`; Ada
   also receives `player.inventory` with an empty list.
8. **Chat.** Ada sends `say hello`. Ada receives `player.said` with
   `self: true`; Ben receives `player.said` with `self: false` and
   `from: "Ada"`.
9. **Leave.** Ben sends `go south`. Ben receives `room.state` for the gate;
   Ada receives `room.leave`.
10. **Return.** Ben sends `go north`. Ben receives `room.state` for the
    marketplace; Ada receives `room.enter`.
11. **Bad message.** Ada sends a frame that is not JSON. She receives
    `error` with `code: "malformed_message"`, and a following `look` still
    works on the same socket. Unknown commands return
    `error` with `code: "unknown_command"` and do not close the socket.
12. **Quit.** Ada sends the `quit` message. Ben receives `room.leave` with
    `reason: "disconnect"`; Ada's socket closes with code `1000`. An
    `input quit` also closes `1000`, but (like the `quit` message) it is a
    session-level exception and does not send a direct reply; if your client
    waits for a reply, use the `quit` message. A bare socket close also produces
    `room.leave`; the close code is then the client's.

## Social (v2.1.2)

Use a third character, **Cara**, alongside Ada and Ben. `tests/test_social.py`
runs this sequence against the server.

1. **Who.** Ada sends `who`. She receives `who.list` with every character,
   including herself, as `{name, room_id, asleep, stone}`. The roster is
   server-wide, so it includes characters in other rooms and characters whose
   session ended.
2. **Emote.** Ada sends `emote waves at the sky` (or the alias
   `me waves at the sky`). Ada receives `player.emoted` with `self: true`; Ben
   and Cara, in the same room, receive it with `self: false` and the same
   `text: "Ada waves at the sky."`. A player in another room receives nothing:
   `emote` is room-local.
3. **Tell.** Ada sends `tell Ben meet me at the gate`. Ada receives
   `player.told` with `self: true` and `You tell Ben, "meet me at the gate".`;
   Ben receives `self: false` and `Ada tells you, "meet me at the gate".`;
   Cara receives nothing, even in the same room. `tell` is private and
   server-wide.
4. **Asleep target.** Cara sends `quit`; Ada and Ben receive `room.leave` with
   `reason: "disconnect"`, `asleep: true`, and `stone: true`. Ada's `who` still
   lists Cara, asleep and stone. Ada sends `tell Cara hello`; she receives
   `error` with `code: "not_awake"` and no `player.told`. Ben receives
   nothing. There is no offline queue.
5. **Wake.** Cara reconnects and logs in with her saved key. She receives
   `auth.ok` and `room.state`, and no old tell is replayed. `who` lists her
   awake again in the same room.

## Containers and locks (v2.0)

These are part of v2.0 and covered by the adapter tests; a client should render
the events even if it does not have a special UI for them:

1. From the gate: `go north` (Marketplace), `go west` (Alchemist Shop, room id
   `shop`). Commands use display names, not room ids.
2. `take torch`, `take gold key`; `light torch` → `object.lit`; `go east`,
   `go north` (the dark forest). The lit torch lights the forest; an unlit one
   would leave it dark.
3. `open chest` → `error` with `code: "locked"`.
4. `use gold key on chest` → `object.unlocked`.
5. `open chest` → `object.opened`; the Brass Lantern spills into the room.
6. `take brass lantern` → `object.taken`.
7. `put brass lantern in chest` → `object.put` (the chest is unlocked).

## Close and get from (v2.1.2)

Continue in the forest with the chest open and the Brass Lantern inside. These
commands are in memory for now; the container's authored `state.open` is what
a restart reloads from the world, not a second inventory store.

1. **Close.** Ada sends `close chest`. Ada receives `object.closed` with
   `object.id: "chest_forest"` and `self: true`; Ben, in the room, receives
   `object.closed` with `self: false`. Ada sends `close chest` again and
   receives `error` with `code: "already_closed"`.
2. **Closed container.** Ada sends `get brass lantern from chest` while the
   chest is closed. She receives `error` with `code: "container_closed"`, and
   the lantern stays inside.
3. **Open again.** Ada sends `open chest` → `object.opened`. As in v2.0, the
   lantern spills into the room; Ada sends `take brass lantern` →
   `object.taken`, then `put brass lantern in chest` → `object.put` to put it
   back.
4. **Get from.** Ada sends `get brass lantern from chest`. Ada receives
   `object.got` with `object.id: "brass_lantern"`, `container.id:
   "chest_forest"`, `self: true`, then the documented `player.inventory`
   follow-up listing the lantern. Ben receives `object.got` with `self: false`
   and `Ada gets the Brass Lantern from the Chest.`
5. **Errors.** Ada sends `get brass lantern from chest` again → `error` with
   `code: "not_here"` (it is in her inventory now). Naming a container that is
   not in the room is `error{not_found}`; a `portable: false` object is
   `error{not_portable}`. A bare `get brass lantern` still means `take`, not
   `get from`.

## What "done" means

Your client is compatible when it can:

- connect and read `hello`, including `auth_required` and `auth_modes`;
- register (show the key once) or log in with a saved key, then render
  `room.state`;
- send `input` and pair the direct reply by `id`;
- render `room.enter` / `room.leave`, `player.said`, `player.emoted`,
  `player.told`, `who.list`, `object.*`, `player.inventory`, `notice`, and
  `error`;
- keep running after an error or a malformed frame;
- quit cleanly.

It does **not** need to look like anyone else's client. The protocol is the
contract; the presentation is yours.

## Troubleshooting

- **The client hangs after `join`.** A v2.1 server rejects `join` with
  `error{code:"not_authenticated"}` and keeps the socket open: send `auth`
  first. Check that you are sending `v: 2` and that you handle `auth.ok`,
  `auth.error`, and `room.state`; a v1 client cannot speak to a v2 server.
- **`auth.error` with `name_taken`.** The character name is already
  registered. Log in with its saved key, or register a different name.
- **`take` fails with "There is no ..."** Use the object's display name, not
  its `id` (`take gold coin`, not `take gold_coin`).
- **The room is dark.** The Mysterious Forest is dark unless someone carries a
  light source. That is intentional; a dark `room.state` has empty `objects`
  and `players`.
- **A command has no reply.** Every `input` must carry an `id`; the server
  echoes it on the direct reply so you can pair them.

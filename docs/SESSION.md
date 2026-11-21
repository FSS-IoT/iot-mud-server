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
   acceptance; the session changes the world.
2. Read `PROTOCOL.md` version 2.
3. Have two copies of your client ready, or two terminals, so you can see
   another player.

## Room geometry is optional

The start rooms may include `room.state.data.room.geometry`, a small grid map
for clients that draw a view (`PROTOCOL.md`, "Optional room geometry"). It is
additive: ignore the field and the acceptance session is unchanged, because
`data.text` remains the contract. A room without a map omits the field, and a
dark room omits it until a light makes the room visible.

## The session

Use two players, **Ada** and **Ben**.

1. **Connect.** Both clients receive `hello` with `v: 2`, `data.protocol`
   containing `2`, and `data.world.epoch`.
2. **Join.** Ada sends `join` with `{"name":"Ada"}`; Ben sends `join` with
   `{"name":"Ben"}`. Each receives `session.ok` and then `room.state`. Ben's
   join state lists Ada under `room.players`; Ada's lists no one yet because
   Ben joins second. When Ben joins, Ada receives `room.enter`.
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

## What "done" means

Your client is compatible when it can:

- connect and read `hello`;
- join and render `room.state`;
- send `input` and pair the direct reply by `id`;
- render `room.enter` / `room.leave`, `player.said`, `object.*`,
  `player.inventory`, `notice`, and `error`;
- keep running after an error or a malformed frame;
- quit cleanly.

It does **not** need to look like anyone else's client. The protocol is the
contract; the presentation is yours.

## Troubleshooting

- **The client hangs after `join`.** Check that you are sending `v: 2` and
  that you handle `session.ok` and `room.state`; a v1 client cannot speak to a
  v2 server.
- **`take` fails with "There is no ..."** Use the object's display name, not
  its `id` (`take gold coin`, not `take gold_coin`).
- **The room is dark.** The Mysterious Forest is dark unless someone carries a
  light source. That is intentional; a dark `room.state` has empty `objects`
  and `players`.
- **A command has no reply.** Every `input` must carry an `id`; the server
  echoes it on the direct reply so you can pair them.

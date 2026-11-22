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
   `self: true` and the object; `take` sends no `player.inventory` follow-up.
   An explicit `inventory` still returns `player.inventory` with the coin.
   Ben receives `object.taken` with `self: false` and a third-person line.
   Commands resolve **display names**; `object.id` is `gold_coin`, but the
   command is `take gold coin`, not `take gold_coin`.
7. **Drop.** Ada sends `drop gold coin`. Both receive `object.dropped`; Ada
   receives no `player.inventory` follow-up. An explicit `inventory` still
   returns `player.inventory` with an empty list.
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

## Containers, close, and get from (v2.1.2)

The container flow is **open → get one item out → close**. Opening lists the
contents and keeps them inside; `get <item> from <container>` pulls one out,
and a plain `take` only reaches objects on the room floor. These commands are
in memory for now; the authored `state.open` is what a restart reloads from
the world, not a second inventory store. A client should render the events even
if it does not have a special container UI.

1. From the gate: `go north` (Marketplace), `go west` (Alchemist Shop, room id
   `shop`). Commands use display names, not room ids.
2. `take torch`, `take gold key`; `light torch` → `object.lit`; `go east`,
   `go north` (the dark forest). The lit torch lights the forest; an unlit one
   would leave it dark.
3. `open chest` → `error` with `code: "locked"`.
4. `use gold key on chest` → `object.unlocked`.
5. **Open.** `open chest` → `object.opened`; the text lists the Brass Lantern,
   which stays inside. Opening does **not** spill it into the room.
6. **Get from.** `get brass lantern from chest` → `object.got` with
   `object.id: "brass_lantern"`, `container.id: "chest_forest"`, `self: true`,
   then the documented `player.inventory` follow-up listing the lantern. Other
   players in the room receive `object.got` with `self: false` and
   `Ada gets the Brass Lantern from the Chest.`
7. **Put back.** `put brass lantern in chest` → `object.put` (the chest is
   unlocked and open).
8. **Close.** `close chest` → `object.closed` with `object.id:
   "chest_forest"` and `self: true`; other players in the room receive
   `object.closed` with `self: false`. `close chest` again → `error` with
   `code: "already_closed"`.
9. **Closed container.** `get brass lantern from chest` while closed →
   `error` with `code: "container_closed"`; the lantern stays inside.
10. **Reopen and get.** `open chest` → `object.opened` (the lantern is still
    inside), then `get brass lantern from chest` → `object.got` plus
    `player.inventory`. A second `get brass lantern from chest` →
    `error{not_here}` because it is in the inventory now.
11. **Other typed errors.** A container not in the room is
    `error{not_found}`; a `portable: false` object is `error{not_portable}`. A
    bare `get brass lantern` still means `take`, not `get from`.

## Exit locks (v2.1.2)

The product world locks the Old Tavern door. The lock is content: the door
names the key object `key_gold_1`, and the gold key in the Alchemist Shop is
the only key that fits. Commands use display names (`gold key`, not
`key_gold_1`). Using the key does not consume it: it stays in the inventory
and the door stays unlocked for the rest of the running world. A restart
rebuilds the authored lock (`PROTOCOL.md`, "Exit locks").

1. From the gate: `go north` (Marketplace), `go west` (Alchemist Shop).
   `take gold key` → `object.taken` with `object.id: "key_gold_1"`. If the
   container flow already ran, the same key is already in the inventory; skip
   the take.
2. `go east` back to the Marketplace. Its east exit is the tavern door:
   `room.state.exits` lists `"direction": "east"`, `"to": "tavern"`,
   `"locked": true`, and `"label": "tavern door"`.
3. **Refused move.** `go east` → `error` with `code: "locked"`; no
   `room.state` and no movement. Ben, in the same room, receives
   `exit.locked` with `exit.direction: "east"`, `exit.to: "tavern"`,
   `by: "Ada"`, and `self: false`. The actor's refusal is the typed error, not
   a second event.
4. **Unlock.** `use gold key on east` → `exit.unlocked` with `self: true`,
   `exit.direction: "east"`, `exit.to: "tavern"`, and the
   `You turn the Gold Key; the tavern door unlocks.` text. Ben receives the
   same event with `self: false`. `use gold key on tavern door` names the
   authored label, and `use gold key on tavern` names the destination room;
   both resolve to the same exit.
5. **Enter.** `go east` → `room.state` for the Old Tavern
   (`room.id: "tavern"`); Ben receives `room.leave` for the Marketplace.
6. **The other side.** The reciprocal tavern → west door is the same physical
   lock. `go west` returns to the Marketplace without a second `use`.
7. **Wrong or missing key.** A key whose object id is not `key_gold_1` does not
   open the door, no `exit.unlocked` is emitted, and `go east` remains
   `error{locked}`. With no key at all, `use gold key on east` is
   `error{not_found}` and the door stays locked. The key is not consumed, so
   keep it in the inventory for the rest of the session.

## What "done" means

Your client is compatible when it can:

- connect and read `hello`, including `auth_required` and `auth_modes`;
- register (show the key once) or log in with a saved key, then render
  `room.state`;
- send `input` and pair the direct reply by `id`;
- render `room.enter` / `room.leave`, `player.said`, `player.emoted`,
  `player.told`, `who.list`, `object.*`, `exit.locked` / `exit.unlocked`,
  `player.inventory`, `notice`, and `error`;
- render `npc.say`, and treat `npc.enter` / `npc.leave` as optional ambient
  updates: a wandering NPC may move between rooms during the session, and the
  next `room.state` already lists it in its new room;
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

# Client acceptance session

This is the script every member client should complete before it is considered
working. It is language-agnostic: your client can be a terminal app, a browser
page, a phone app, or anything else that speaks WebSocket JSON.

The server's `tests/test_protocol_v2.py` covers the core of this sequence
(presence, movement, objects, chat, errors, and the close code); this page is
the full client acceptance. Containers, exit locks, social commands, NPC
dialogue, and sleep/wake are required sections: v2.1 clients are expected to
render them.

## Before you start

1. Run the server locally (`../BOOTSTRAP.md` shows how). Seed a **new,
   separate** database for the acceptance; the session takes the gold coin,
   unlocks the forest chest and the tavern door, and moves items. A used
   database can fail in ways that look like client bugs. Stop any server
   already using the port.

   ```bash
   uv run python -m src.mud.admin seed --db .scratch/session.db
   ./run_server.sh --port 8766 --db .scratch/session.db
   ```

   The server port is not part of the protocol. Whatever you choose, pass the
   matching value to your client: `--uri ws://localhost:8766`. A v2.1 server
   requires auth, so have two character names ready and somewhere outside the
   repository to keep the keys (for the reference client,
   `--save-key ~/.config/iot-mud/ada.key`).
2. Read `PROTOCOL.md` version 2.1, especially "v2.1 auth" and "Session
   lifecycle", and `CLIENTS.md` for the key and sleep rules.
3. Have two copies of your client ready, or two terminals, so you can see
   another player.

Before the first run, record the full server commit your client targets in its
README and compare it with `hello.data.server_commit`; `BOOTSTRAP.md` gives the
v2.1.4 reference pin and the reference client checks it with
`--expect-commit <full sha>`. If the commit differs, read the `PROTOCOL.md`
changelog before the acceptance, not after.

For a second run, stop the server and use a new file (or delete it:
`rm -f .scratch/session.db*`; on PowerShell,
`Remove-Item -Force .scratch\session.db*`). Do not reseed the old file:
`admin seed --reset` replaces the world but **keeps characters and their
keys**, so Ada and Ben would still be registered and the session would start
with them asleep.

## Automated clients

The default command limits are 10 commands per second with a burst of 20
(`PROTOCOL.md`, "Command rate limit and idle timeout"). A person following this
page stays under them; a script that sends the whole route as fast as it can
gets `error{code:"rate_limited"}` and then waits forever for the next event.
Either pace the script, or start the acceptance server with a scratch config
that raises the limits:

```toml
[limits]
commands_per_second = 1000
command_burst = 1000
```

Save it as `~/.config/iot-mud/acceptance.toml` (create the directory first with
`mkdir -p ~/.config/iot-mud` if needed) and add `--config` to the server
command:

```bash
./run_server.sh --port 8766 --db .scratch/session.db \
  --config ~/.config/iot-mud/acceptance.toml
```

Keeping the config outside the checkout means `git status` stays clean; only
the scratch database lives in `.scratch/`, and `*.db` is already ignored.

The limits are a host setting, never a protocol field. Do not raise them on the
club server just to pass a client test.

## Effects are not part of the acceptance

The server's default `[features].effects = false` keeps authored effects on the
engine bus and in `event_log`; no client receives an `effect` event. A client
must ignore the event type if it ever arrives, but the acceptance run does not
require it. Do not enable the flag just to pass this session.

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
   character without repeating the key, followed by `room.state`. With the
   reference client, `--save-key PATH` writes the key with mode `0600` on
   registration, and `--key-file PATH` logs in without printing it again.
3. **Look.** Ada sends `input` `look`. Her new `room.state` now lists Ben under
   `room.players`.
4. **Move.** Ada sends `go north`. Ada receives `room.state` for the
   Marketplace. Ben, still at the gate, receives `room.leave` with
   `actor.name: "Ada"` and `reason: "move"`.
5. **Arrive.** Ben sends `go north`. Ben receives `room.state` for the
   Marketplace. Ada, already there, receives `room.enter` with
   `actor.name: "Ben"` and `reason: "move"`.
6. **Take.** Ada sends `take gold coin`. Ada receives `object.taken` with
   `self: true` and the object, then a state-only `player.inventory` (items /
   capacity / used / unit, **no `text`**). An explicit `inventory` returns
   `player.inventory` with the rendered list. Ben receives `object.taken` with
   `self: false` and a third-person line, and no inventory push.
   Commands resolve **display names**; `object.id` is `gold_coin`, but the
   command is `take gold coin`, not `take gold_coin`.
7. **Drop.** Ada sends `drop gold coin`. Both receive `object.dropped`; Ada
   then receives a state-only `player.inventory` (empty `items`, no `text`).
   An explicit `inventory` returns `player.inventory` with the rendered list.
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
    `error` with `code: "unknown_command"` and do not close the socket. In the
    reference client, type `!malformed` to send the broken frame; the error
    renders with its code, for example `[malformed_message]`.
12. **Quit.** Ada sends the `quit` message. Ben receives `room.leave` with
    `reason: "disconnect"`, `asleep: true`, and `stone: true` because Ada is an
    authenticated character; the character stays in the room. Ada's socket
    closes with code `1000`. The reference client prints
    `Connection closed: 1000`. (The v2.0 guest path removes the character
    instead; see "Sleep and wake" below for the full authenticated flow.) An
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

## NPC dialogue (v2.1.3)

Use Ysolde the Alchemist in the shop; she does not wander, so the step is
repeatable. `tests/test_npc_dialogue.py` runs this sequence.

1. From the gate: `go north` (Marketplace), `go west` (Alchemist Shop).
2. `talk Ysolde` → `npc.say` with `npc.id: "npc_alchemist"`,
   `npc.name: "Ysolde the Alchemist"`, `self: true`, and the authored greeting.
   The event carries no `topic`.
3. Ben, in the same room, receives the same `npc.say` with `self: false`.
4. `ask Ysolde about chest` → `npc.say` with `topic: "chest"` and the authored
   line. `ask Ysolde about weather` → the NPC's `default` line, with no
   `topic`: an unknown topic is not an error, so a client need not know the
   script.
5. `talk nobody` → `error{not_found}`; `ask Ysolde` without `about` →
   `error{bad_args}`. Neither emits `npc.say`.
6. Wandering NPCs (`wander: true`) may move between rooms during a long
   session. Treat `npc.enter` and `npc.leave` as optional ambient updates;
   `room.state.npcs` is the resynchronization path. Do not require an NPC to
   stay in one room.

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

## Sleep and wake (v2.1.1)

An authenticated disconnect is a sleep, not a logout. `tests/test_sleep_wake.py`
runs this sequence; use Ada and Ben in the Marketplace, with Ada carrying the
gold key from the exit-lock section.

1. **Inventory before.** Ada sends `inventory` → `player.inventory` lists the
   torch and the Gold Key (`id: "key_gold_1"`).
2. **Sleep.** Ada sends `quit`. Ben receives `room.leave` with
   `reason: "disconnect"`, `asleep: true`, `stone: true`, and `dropped`
   listing the Gold Key. Ada's socket closes `1000`.
3. **Stone.** Ben's `look` lists Ada under `room.players` with `asleep: true`
   and `stone: true`; the Gold Key is now on the room floor in `objects`.
   Ben's `who` lists Ada asleep and stone in the same room. Ada's character was
   not removed.
4. **No offline queue.** Ben sends `tell Ada hello` → `error{not_awake}`; no
   `player.told` is sent, and nothing is stored for later.
5. **Wake.** Ada reconnects and sends `auth` with `mode: "login"` and her saved
   key. She receives `auth.ok` without a key, then `room.state` for the same
   room. Her inventory still has the torch; the Gold Key is not restored
   because it dropped when she slept. Ben receives `room.enter` with
   `reason: "connect"`.
6. **Recover.** Ben sends `take gold key` and gets `object.taken`; the key is
   now his. Ada may take it back from him, or carry on; the dropped item stayed
   where it landed.
7. **Idle is the same path (optional).** Lower `[limits].idle_timeout_seconds`
   in `mud.toml`, restart the server, and do nothing on an authenticated
   socket. When the timeout passes, the room receives the same sleep
   `room.leave` and the socket closes `4004`. A later login wakes the character
   in place. Guest sessions (`auth_required = false`) are removed on disconnect
   and are not slept.

## What "done" means

Your client is compatible when it can:

- connect and read `hello`, including `auth_required` and `auth_modes`;
- register (show the key once) or log in with a saved key, then render
  `room.state`;
- send `input` and pair the direct reply by `id`;
- render `room.enter` / `room.leave`, `player.said`, `player.emoted`,
  `player.told`, `who.list`, `object.*`, `exit.locked` / `exit.unlocked`,
  `player.inventory`, `notice`, and `error`;
- run `talk` and `ask`, and render `npc.say`, with both the actor's `self: true`
  line and the room's `self: false` line;
- treat `npc.enter` / `npc.leave` as optional ambient updates: a wandering NPC
  may move between rooms during the session, and the next `room.state` already
  lists it in its new room;
- sleep on disconnect and wake on the next login without losing the retained
  inventory, and show a sleeping character as asleep/stone;
- ignore an `effect` event type it does not render; the default server sends
  none;
- keep running after an error or a malformed frame;
- quit cleanly.

It does **not** need to look like anyone else's client. The protocol is the
contract; the presentation is yours.

## You have passed when

Tick these against your own client's output. The reference client's wording is
shown in parentheses as one example; the structured fields are the contract.

- [ ] **Pin.** Your README records the full 40-character server commit, and
      `hello.data.server_commit` matches it (or you have read the changelog for
      the difference).
- [ ] **Hello.** `data.protocol` contains `2`, `data.auth_required` is `true`,
      `data.auth_modes` is `["key"]`, and the world epoch and
      `data.server_commit` are readable. (`Protocol: 2`, `Auth: required
      (key)`, `World: ... (epoch ...)`, `Server commit: ...`)
- [ ] **Register and log in.** Registration shows the key once; a later login
      with the saved key says `mode: "login"`, returns no `key`, and puts the
      character in its stored room. (`Registered as: Ada`; then `Logged in as:
      Ada`, with no `mud1-` line)
- [ ] **Movement and presence.** `go north` reaches the Marketplace; the other
      client sees `room.leave{reason:"move"}` and then `room.enter`.
- [ ] **Objects.** `take gold coin` gives `object.taken{self:true}` with no
      automatic `player.inventory`; an explicit `inventory` lists it.
- [ ] **Social.** `say`, `emote`, and `tell` produce `player.said`,
      `player.emoted`, and `player.told`; `who` lists both characters with
      room, asleep, and stone. (`Who: Ada (market, awake); Ben (market, awake).`)
- [ ] **Bad frame.** A malformed frame produces `error{malformed_message}` and
      the next `look` succeeds on the same socket. (`[malformed_message] ...`)
- [ ] **Exit locks.** The tavern door is `locked`, the gold key unlocks it with
      `exit.unlocked`, and the key is not consumed.
- [ ] **NPC dialogue.** `talk Ysolde` and `ask Ysolde about chest` produce
      `npc.say` with the NPC id and, for `ask`, `topic: "chest"`.
- [ ] **Containers.** `open chest`, `get brass lantern from chest`, `put`, and
      `close chest` produce `object.opened`, `object.got` plus
      `player.inventory`, `object.put`, and `object.closed`.
- [ ] **Sleep and wake.** `quit` sleeps the character (`asleep` and `stone`
      true, `reason: "disconnect"`), drops the gold key, and a later login wakes
      it in place; a `tell` to a sleeper is `error{not_awake}`.
- [ ] **Clean close.** `quit` closes with `1000`. (`Connection closed: 1000`)

## Troubleshooting

- **The client hangs after `join`.** A v2.1 server rejects `join` with
  `error{code:"not_authenticated"}` and keeps the socket open: send `auth`
  first. Check that you are sending `v: 2` and that you handle `auth.ok`,
  `auth.error`, and `room.state`; a v1 client cannot speak to a v2 server.
- **Connection refused, or the client talks to the wrong world.** The URI must
  match the server's `--port` (`--uri ws://localhost:8766` for the acceptance
  server). 8765 is the default; 8766 is the scratch example.
- **The seed says "the database already holds a world".** That file is used.
  For a new acceptance, delete it (`rm -f .scratch/session.db*`) or point `--db`
  at a new name. `--reset --yes` replaces the world but keeps characters and
  keys, so it does not give you a fresh acceptance database.
- **A scripted session stops with `rate_limited`.** The default burst is 20
  commands. Pace the script or start the acceptance server with the higher
  limits shown in "Automated clients".
- **`auth.error` with `name_taken`.** The character name is already
  registered. Log in with its saved key, or register a different name.
- **The key is in shell history.** Use `--key-file` (reference client) or the
  operating system keyring; `--key` is visible to `ps` and the shell history.
  If a key leaked, ask the operator for `admin rekey <name>`.
- **`take` fails with "There is no ..."** Use the object's display name, not
  its `id` (`take gold coin`, not `take gold_coin`).
- **The room is dark.** The Mysterious Forest is dark unless someone carries a
  light source. That is intentional; a dark `room.state` has empty `objects`
  and `players`.
- **A command has no reply.** Every `input` must carry an `id`; the server
  echoes it on the direct reply so you can pair them.

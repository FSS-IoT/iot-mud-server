# Authoring world content

The server loads a YAML world and seeds it into SQLite. The product world is
the directory `data/worlds/club_world/`:

```text
data/worlds/club_world/
  world.yaml            name, start_room, rng_seed
  areas/
    starting_village.yaml
    south_road.yaml
    mill.yaml
    riverside.yaml
    north_bank.yaml
    ruins.yaml
    caves.yaml
```

`world.yaml` holds metadata only:

```yaml
name: The Commons
start_room: gate
rng_seed: 20261121
```

Each area file holds one `area:` name and the rooms that belong to it. Adding
an area is a new file under `areas/` plus a connection from an existing room;
nothing else registers it. Areas can be authored in parallel because the
validator names the file, the room, and the offending id when a merge crosses
two authors.

The five rooms of the original starting village live in
`areas/starting_village.yaml`. They keep their names, descriptions, objects,
and maps; the gate gained one south door onto the south road. The old
five-room files (`data/worlds/starting_village.json` and `.yaml`) remain as the
frozen v2.1.0 conformance fixture, not the product world.

## The 34-room product world

| Area | File | Rooms |
|---|---|---|
| Starting village | `starting_village.yaml` | `gate`, `market`, `tavern`, `shop`, `forest` (5) |
| South road | `south_road.yaml` | `south_road`, `orchard`, `beehives`, `mill_bridge` (4) |
| Mill | `mill.yaml` | `mill`, `millpond`, `millrace`, `granary` (4) |
| Riverside | `riverside.yaml` | `river_path`, `riverbank`, `ford`, `river_shallows`, `heron_rock`, `reeds`, `ferry_landing` (7) |
| North bank | `north_bank.yaml` | `ferry`, `north_bank`, `toll_house`, `watchtower`, `tower_top` (5) |
| Ruins | `ruins.yaml` | `shrine`, `ruins_gate`, `ruins_court`, `chapel`, `crypt`, `fallen_tower` (6) |
| Caves | `caves.yaml` | `cave_mouth`, `cave_tunnel`, `crystal_grotto`, `underground_pool` (4) |

`gate` is the start; every room is reachable on foot. The main loops are:

- gate → market → shop / tavern / forest;
- gate → south road → orchard / mill bridge → mill → granary;
- mill bridge → river path → reeds → ferry landing → ferry → north bank →
  toll house / watchtower / shrine;
- shrine → ruins → chapel → crypt → caves;
- mill bridge → millrace (locked shortcut) and ruins court → fallen tower
  (locked side room).

The golden scenarios in `tests/golden/` walk every room and area, so a new
room or a broken connection fails the suite before a player finds it.

## Run the checks

```sh
uv run python -m src.mud.admin content check --world data/worlds/club_world
uv run pytest tests/test_world_content.py -q
```

`content check` exits 0 and prints `ok`, or exits 1 and prints one line per
problem. It never prints a traceback. The test file checks the world shape the
spec asks for: 30-40 rooms, all reachable from `start_room`, every exit
reciprocal, at least five NPCs with dialogue, at least ten interactive objects
across containers, readables, keys, and locked exits, and a golden path into
every area. It also walks the key/container progression to prove no key is
locked behind the door it opens.

## Seed the world

`admin seed` validates the YAML, writes the world into the database, and
records the world name and a new epoch. It creates the database and applies the
schema when the file does not exist:

```sh
uv run python -m src.mud.admin seed --db data/mud.db
```

Seeding an already-seeded database is refused; replace the world explicitly:

```sh
uv run python -m src.mud.admin seed --db data/mud.db --reset --yes
```

A reset is destructive to the world, not to people. It replaces rooms, exits,
objects, and NPCs, clears every character's inventory, and places every
character at the new `start_room`. Characters and their character keys are
kept, and the epoch changes, so clients drop cached maps. The epoch survives a
plain server restart against the same database. `--db` wins over
`[storage].path`; see `OPERATIONS.md` for the service paths.

## A room

```yaml
area: south_road

rooms:
  - id: south_road
    name: South Road
    description: A rutted road runs north to the village gate.
    exits:
      west: orchard
      east:
        to: mill_bridge
        locked: true
        key: key_mill
        label: millrace gate
    objects:
      - id: milestone
        name: Milestone
        description: A weathered stone marker.
        kind: readable
        readable_text: VILLAGE GATE — one mile north.
        weight: 0.0
        portable: false
```

- Ids are lowercase slugs and globally unique across rooms, objects, and NPCs.
- Declare each exit **once**. The loader creates the reciprocal
  (`north`/`south`, `east`/`west`, `up`/`down`, `in`/`out`). `one_way: true`
  opts out, but the product world has none: a player must always be able to
  walk back.
- A plain string is the short form of `{to: <room_id>}`. The detailed form
  adds `locked`, `key`, `label`, and `one_way`.
- `label` is what a player names when using a key on the door. Both sides of a
  two-way door share the lock.
- A room without an exit entry can still have exits: the reciprocals of a
  neighbour's declaration count. Every room must be reachable from
  `start_room`, so draw the connection from the side that makes the map read
  clearly.
- `dark: true` hides the description and contents until a lit light source is
  present.
- Unknown fields are errors. A typo in a field name is an authoring bug, and
  the validator names it before the world reaches a player.

## Objects

`kind` is `item` (default), `container`, or `readable`.

```yaml
- id: strongbox
  name: Strongbox
  description: A banded iron box, heavy enough to stay under the counter.
  kind: container
  weight: 8.0
  portable: false
  locked: true
  required_key: key_iron
  contains: [key_bone, toll_receipt]
```

- A container lists its contents by global object id. The contained object is
  declared in the same file and is placed inside the container, not on the
  room floor. Containers may not contain themselves, directly or
  transitively; the validator and the runtime `put` both refuse a cycle.
- A locked container names a portable key object with `required_key`. Opening
  it needs `use <key> on <container>`; `open` alone is `error{locked}`.
- `state.open: true` starts a container open; the default is closed. Opening
  and closing at runtime writes the lid flag to the container's `objects.state`
  row, so the next server start restores it (#131).
- A readable sets `readable_text`. `description` is what `look` shows; the
  readable text is what `read` returns.
- `weight` is the carry unit, authored in real-life **kilograms**; the in-game
  display unit is server-sent as `unit` (currently `"stones"`), never
  converted. `portable: false` marks scenery a player cannot lift; `get` from
  a container refuses it with `error{not_portable}`.
- `light_source: true` lets an object light a dark room. `lit: true` starts it
  lit; otherwise `light <source>` is needed. A light source must be equipped in
  a hand, on the belt, or on the head to light the room; one in the pack is
  dark. Igniting one commits `lit` to `objects.state`, so it stays lit across a
  restart (#131). A lit source cannot be unequipped or put in a container:
  `extinguish <source>` first. A lit source can be dropped; it stays lit on the
  floor and lights the room until it burns out (burn-down is a later item
  lifecycle). Taking a lit source puts it straight into a hand, belt, or head
  slot; if none is free the take is refused. A fireproof carrier that can hold
  embers is a later item capability, not a place for a burning torch.
- `equip_slots` lists where an item can go: `hand`, `head`, `body`, `back`,
  `belt`, or `feet`. `hands: 1` uses one hand; `hands: 2` uses both. Empty
  means the item cannot be equipped. A lantern can use `[hand, belt]`; a torch
  uses `[hand]`. The runtime slot (`hand_1`, `belt`, ...) is stored in
  `objects.state.slot` and restored on login (#181). `equip <item> on hand 1`
  or `hand 2` names a hand. If that hand holds an unlit item, the new item takes
  the hand and the old one goes to the pack; a lit item is refused with
  `still_lit`. A lit source can only occupy a hand, belt, or head; another slot
  is `bad_args`. A two-handed swap must name a hand (`equip sword on hand 1`).
- `dropped_on_sleep: true` makes the object fall into the room when its carrier
  disconnects and turns to stone (#72). The world marks the five keys so a
  sleeper cannot strand a door key in a pocket.

### Locks and keys

Lock, lid, and light state is durable and shared (#131). A lock turned by one
character is open for every character in the world; the same is true of a
container lid and a lit torch. Runtime changes are committed to SQLite before
the in-memory world changes: `objects.state` holds `locked`, `open`, and `lit`
for an object, and `exits.locked` holds a door's state. A restart overlays those
committed flags and object placements onto the freshly loaded YAML, so a door
unlocked before the restart stays unlocked. `admin seed --reset --yes` replaces
the world rows and therefore rebuilds every authored lock and lid.

The product world uses five keys:

| Key id | Where it starts | Opens |
|---|---|---|
| `key_gold_1` | Alchemist Shop, loose | Old Tavern door, forest chest |
| `key_mill` | Granary, loose | Millrace gate, race chest |
| `key_iron` | Ferry locker, inside an unlocked container | Toll-house strongbox, watchtower stair |
| `key_bone` | Strongbox, behind `key_iron` | Cracked tower arch (`fallen_tower`) |
| `key_silver` | Fallen Tower, loose | Sealed sarcophagus in the crypt |

A key is matched by its object `id`, not its display name. Using a key does not
consume it, and a two-way door is one physical lock: unlocking either side
unlocks both. `one_way: true` locks only the declared direction.

The chain is deliberate: no key is locked behind the door it opens. The iron
key opens the strongbox that holds the bone key that opens the tower arch; the
silver key behind that arch opens the crypt sarcophagus. `tests/test_world_content.py`
walks the chain to prove it.

## NPCs and dialogue

NPCs are scripted content. `talk` and `ask` are delivered in v2.1; wandering is
deterministic and seeded.

```yaml
npcs:
  - id: npc_miller
    name: Hollis the Miller
    description: A dust-white man with a miller's thumb and a patient frown.
    dialogue:
      greeting: "Mind the stones. They do not stop for talk."
      default: "Ask about the mill, the river, or the key."
      topics:
        mill: "My grandfather turned these stones. The race gate is mine to lock."
        key: "Mill key in the granary. It opens the race gate and the chest beside it."
    wander: true
    wander_every: 15
```

- `talk <npc>` speaks `greeting`; if there is none, it speaks `default`.
- `ask <npc> about <topic>` matches the topic case-insensitively and speaks
  that line. An unknown topic is not an error: the NPC answers with `default`,
  so a client need not know the script.
- `wander: true` moves the NPC on the 1 Hz world tick, every `wander_every`
  ticks, by an unlocked exit to a loaded room. The path is seeded from the
  world's `rng_seed` plus the NPC's stable id, so a reload replays it.
  `wander: false` never moves. The product world uses 12, 15, and 20.
- Dialogue is authored text only. No LLM runs on this path.
- NPC names resolve like object names: exact display name, then a
  case-insensitive prefix, then a substring, with `<name> <n>` for a tie.

## Effects

Effects are data for the v2.1 bus and `event_log`. A trigger is one of
`on_enter`, `on_exit`, `on_take`, `on_drop`, `on_use`, `on_open`, `on_close`,
`on_talk`, or `on_device`. A type is a dotted lowercase name (`light.dim`,
`sound.play`, `ambience.market`); params must survive a JSON round trip.

```yaml
effects:
  on_enter:
    - type: light.dim
      params:
        color: "#223322"
        intensity: 0.4
```

Effects never change gameplay: turning them off or deleting every block changes
no reply, location, inventory, lock, or NPC position. v2.1 fires `on_talk`
from a successful `talk`/`ask`, `on_enter` from a player entering a room, and
`on_open` from opening a container. The other authored triggers are parsed and
validated but not fired yet.

A successful trigger publishes one `effect` on the engine bus and appends one
`event_log` row per authored entry. With `[features].effects = false` (the
default) no client receives it; with `true` the room does. A malformed trigger,
type, or param is a `content check` error, not a runtime surprise. See
`PROTOCOL.md`, "Effects", for the payload.

## Room geometry

A room may carry a map for clients that draw one. Geometry is optional and
additive; a room without it behaves exactly as before.

```yaml
geometry:
  cells:
    - "###+###"
    - "#.....#"
    - "#.....#"
    - "#.....#"
    - "#.....#"
    - "#.....#"
    - "###+###"
  spawn:
    north: [3, 1]
  props:
    - kind: gatepost
      cell: [1, 5]
    - kind: banner
      cell: [5, 1]
```

- `cells` are rows from the top, all the same length, at least 3x3. `#` is
  wall, `.` floor, `+` door, `^` stairs up, `v` stairs down.
- A `+` sits on a boundary edge and must match an exit: top north, bottom
  south, left west, right east. There is exactly one door per cardinal exit, no
  door without an exit, and no door in a corner.
- Every boundary cell is a wall or a door; stairs and props are interior. `^`
  and `v` mark the stairs for an `up`/`down` exit and are walkable.
- `spawn` maps an exit side to the floor cell where a client places an
  arriving player. If a side is absent, the cell just inside that side's door
  is used. Spawn cells are in bounds, on floor, and name an exit the room has.
- `props` are static decor: `{kind, cell}`, in bounds, on floor, and not on a
  spawn cell. Dynamic objects, players, and NPCs are **not** positioned here;
  `room.state`'s `objects`, `players`, and `npcs` remain the source for those.
- The loader validates every map at startup and refuses a world with an
  invalid map, naming the room and the offending cell. `room.state` omits
  `geometry` in a dark room, as it omits the other contents.
- The five starting-village rooms keep their maps; new rooms may omit
  geometry entirely.

## The rules a new area must pass

1. `content check` is clean.
2. Every room is reachable from `start_room`; no exit dangles; every door is
   reciprocal.
3. Every locked door's key is obtainable without crossing that same door. Keys
   may chain: the iron key opens the strongbox that holds the bone key that
   opens the tower arch.
4. Every area has a short walk from a neighbouring area, recorded in
   `AREA_PATHS` in `tests/test_world_content.py`. A path may not cross a locked
   exit.
5. The five starting-village rooms still match the frozen fixture, apart from
   the gate's south door.
6. No id is reused across rooms, objects, and NPCs; no container cycle exists;
   every effect trigger, type, and param validates; every geometry map matches
   its exits.

The validator reports every problem in one run, each line naming the file, the
room, and the offending id where one exists. Fix all of them, then run the test
suite; `tests/golden/` proves a player can still reach every room and every
documented event.

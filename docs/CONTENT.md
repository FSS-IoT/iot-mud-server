# Authoring world content

The server loads a YAML world. The product world is the directory
`data/worlds/club_world/`:

```
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

`world.yaml` holds metadata only. Each area file holds one `area:` name and the
rooms that belong to it. Adding an area is a new file under `areas/` plus a
connection from an existing room; nothing else registers it. Areas can be
authored in parallel because the validator names the file, the room, and the
offending id when a merge crosses two authors.

The five rooms of the original starting village live in
`areas/starting_village.yaml`. They keep their names, descriptions, objects,
and maps; the gate gained one south door onto the south road. The old
five-room files (`data/worlds/starting_village.json` and `.yaml`) remain as the
frozen v2.1.0 conformance fixture, not the product world.

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
- `label` is what a player names when using a key on the door. Both sides of a
  two-way door share the lock.
- A room without an exit entry can still have exits: the reciprocals of a
  neighbour's declaration count. Every room must be reachable from
  `start_room`, so draw the connection from the side that makes the map read
  clearly.
- `dark: true` hides the description and contents until a lit light source is
  present.
- `geometry` is optional map data for clients. If a room has geometry, its
  doors must match its effective exits exactly. The five starting-village maps
  are kept; new rooms may omit geometry.

## Objects

`kind` is `item` (default), `container`, or `readable`.

- A container lists its contents by global object id:

  ```yaml
  - id: strongbox
    name: Strongbox
    kind: container
    locked: true
    required_key: key_iron
    contains: [key_bone, toll_receipt]
  ```

  The contained object is declared in the same file and is placed inside the
  container, not on the room floor. A locked container needs a portable key
  object. Containers may not contain themselves, directly or transitively.
- A readable sets `readable_text`. `description` is what `look` shows; the
  readable text is what `read` returns.
- A key is just a portable object named by a lock's `key` (exit) or
  `required_key` (container). The world uses five: `key_gold_1`, `key_mill`,
  `key_iron`, `key_bone`, `key_silver`.
- `dropped_on_sleep: true` makes the object fall into the room when its carrier
  disconnects and turns to stone (#72). The world marks the five keys so a
  sleeper cannot strand a door key in a pocket.
- `portable: false` marks scenery a player cannot lift.
- `state.open: true` starts a container open; the default is closed.

## NPCs

NPCs are content only in this milestone; #81 adds `talk`/`ask` and #82 adds
deterministic wandering. The data is already here:

```yaml
npcs:
  - id: npc_miller
    name: Hollis the Miller
    description: A dust-white man with a patient frown.
    dialogue:
      greeting: "Mind the stones. They do not stop for talk."
      default: "Ask about the mill, the river, or the key."
      topics:
        mill: "My grandfather turned these stones."
        key: "Iron key in the granary."
    wander: true
    wander_every: 15
```

`default` is the fallback for an unknown topic. `wander: false` keeps an NPC in
place; `wander_every` is the tick interval for the ones that move.

## Effects

Effects are data for the #84 bus. A trigger is one of `on_enter`, `on_exit`,
`on_take`, `on_drop`, `on_use`, `on_open`, `on_close`, `on_talk`, or
`on_device`. A type is a dotted lowercase name (`light.dim`, `sound.play`,
`ambience.market`); params must survive a JSON round trip.

```yaml
effects:
  on_enter:
    - type: light.dim
      params:
        color: "#223322"
        intensity: 0.4
```

Effects never change gameplay. A malformed trigger, type, or param is a
`content check` error, not a runtime surprise.

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

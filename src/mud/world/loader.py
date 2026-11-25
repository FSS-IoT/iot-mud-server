import json
from pathlib import Path

from src.mud.config import DEFAULT_WORLD
from src.mud.content import NPC, Entity, ExitLock, Room
from src.mud.content.models import load_world as load_content_world
from src.mud.content.validator import validate_world
from src.mud.content.world import compile_world
from src.mud.engine.core import Engine
from src.mud.world.geometry import WorldDataError, validate_geometry
from src.mud.world.wander import NpcWanderer

YAML_SUFFIXES = frozenset({".yaml", ".yml"})


async def load_world(engine: Engine, data_path: str | Path) -> None:
    """Populate the engine from a world file.

    A `.yaml`/`.yml` file or a world directory is read through the v2.1
    content model and checked by the semantic validator before any engine
    state is built. A `.json` file keeps the v2.0 loader that legacy tests
    still exercise, so both formats build the same effective world.

    Geometry is validated while the world loads: a bad map raises
    `WorldDataError` instead of being served. A room without a `geometry` key
    stays geometry-less, and the protocol simply omits the field.
    """
    path = Path(data_path)
    if path.is_dir() or path.suffix.lower() in YAML_SUFFIXES:
        await _load_yaml_world(engine, path)
        return
    await _load_json_world(engine, path)


async def _load_yaml_world(engine: Engine, data_path: Path) -> None:
    """Build the engine from the v2.1 YAML model via the compiled world.

    The content model rejects a missing, malformed, or wrongly shaped file.
    The semantic validator then rejects a world that does not hold together
    (duplicate ids, dangling or doubly declared exits, unreachable rooms,
    container cycles, missing keys, malformed effects, bad geometry). Startup
    fails with those messages rather than serving a partial world.

    `compile_world` does the construction once: the effective exit graph, the
    shared lock identity of each two-way door, the containment placements, and
    the normalized geometry. This function only installs engine entities.
    """
    loaded = load_content_world(data_path)
    errors = validate_world(loaded)
    if errors:
        raise WorldDataError("\n".join(errors))

    compiled = compile_world(loaded)

    # 1. First pass: create every object, globally. `contains` links by
    # object id, so a child may be declared in any room and resolved below.
    entity_map: dict[str, Entity] = {}
    for room_data in compiled.rooms:
        for obj in room_data.objects:
            entity = Entity(
                name=obj.name,
                description=obj.description,
                weight=obj.weight,
                readable=obj.kind == "readable",
                light_source=obj.light_source,
                item_id=obj.id,
                locked=obj.locked,
                required_key=obj.required_key,
                lit=obj.lit,
                dropped_on_sleep=obj.dropped_on_sleep,
                portable=obj.portable,
                container=obj.kind == "container",
                # The lid state is authored under `state.open`; a container
                # with no key starts closed (#76).
                open=bool(obj.state.get("open", False)),
            )
            entity.instance_id = obj.id
            # Authored triggers travel with the object into the running world
            # (#84): the engine publishes them when `on_open` and friends fire.
            entity.effects = dict(obj.effects)
            # Kept for the v2.1.3 read/effects seam. The v2.0 adapter reads
            # `description`, so the YAML carries the same text in both places.
            entity.readable_text = obj.readable_text or obj.description
            entity_map[obj.id] = entity

    # 1b. Container contents, from the compiled placements.
    for child_id, container_id in compiled.contained.items():
        entity_map[container_id].add_content(entity_map[child_id])

    # 2. Second pass: create all rooms.
    room_map: dict[str, Room] = {}
    for room_data in compiled.rooms:
        room = Room(
            name=room_data.name,
            description=room_data.description,
            dark=room_data.dark,
        )
        room.item_id = room_data.id
        # The room's authored triggers (#84). Same seam as an object or NPC;
        # `on_enter` is driven by player movement.
        room.effects = dict(room_data.effects)
        room_map[room_data.id] = room

    # 2b. Place the scripted NPCs. An NPC lives in its room's `npcs` list and
    # never in `contents`, so `take`, `drop`, capacity, and container contents
    # keep their v2.1.2 behavior (#81). The authored dialogue is copied as
    # plain strings; the engine never reads the YAML model directly.
    for room_data in compiled.rooms:
        room = room_map[room_data.id]
        for npc_data in room_data.npcs:
            npc = NPC(
                name=npc_data.name,
                description=npc_data.description,
                greeting=npc_data.dialogue.greeting,
                default=npc_data.dialogue.default,
                topics=npc_data.dialogue.topics,
                item_id=npc_data.id,
                wander=npc_data.wander,
                wander_every=npc_data.wander_every,
                effects=npc_data.effects,
            )
            npc.instance_id = npc_data.id
            room.npcs.append(npc)

    # 3. Exits from the compiled graph. One `ExitLock` instance per lock id, so
    # both directions of a door share the same physical lock and label.
    lock_instances: dict[str, ExitLock] = {}
    for edge in compiled.exits:
        room = room_map[edge.room_id]
        room.exits[edge.direction] = room_map[edge.target_id]
        if edge.lock_id is not None:
            lock = lock_instances.get(edge.lock_id)
            if lock is None:
                spec = compiled.locks[edge.lock_id]
                lock = ExitLock(key=spec.key, label=spec.label)
                lock_instances[edge.lock_id] = lock
            room.exit_locks[edge.direction] = lock

    # 4. Geometry, already normalized and validated by the compiler.
    for room_data in compiled.rooms:
        if room_data.geometry is not None:
            room_map[room_data.id].geometry = room_data.geometry

    # 5. Place objects. A contained object belongs to its container, not to
    # the room it is declared in.
    for room_data in compiled.rooms:
        room = room_map[room_data.id]
        for obj in room_data.objects:
            if obj.id not in compiled.contained:
                room.add_content(entity_map[obj.id])

    # 6. Finalize engine state.
    for room in room_map.values():
        engine._rooms[room.name] = room
    engine.start_room = room_map[compiled.start_room]

    # 7. Deterministic world motion (#82). The seed is authored content, not a
    # clock: a reload replays the same NPC paths. The wanderer is built after
    # every room and NPC exists, and moves only `Room.npcs` from here on.
    engine.rng_seed = compiled.rng_seed
    engine.wanderer = NpcWanderer(engine.rooms, compiled.rng_seed)


async def _load_json_world(engine: Engine, data_path: Path) -> None:
    """Populate the engine from the v2.0 JSON file.

    Kept for the legacy geometry tests and for worlds written before the
    YAML format. The product default is YAML (`load_demo_village`).
    """
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"World data file not found at {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    # Maps to hold created objects during construction phase
    item_map = {}
    room_map = {}

    # 1. First pass: Create all entities (Items)
    for i_data in data.get("items", []):
        item = Entity(
            name=i_data["name"],
            description=i_data["description"],
            weight=float(i_data.get("weight", 0.0)),
            readable=bool(i_data.get("readable", False)),
            light_source=bool(i_data.get("light_source", False)),
            item_id=i_data.get("id"),
            locked=bool(i_data.get("locked", False)),
            required_key=i_data.get("required_key"),
            dropped_on_sleep=bool(i_data.get("dropped_on_sleep", False)),
            portable=bool(i_data.get("portable", True)),
            # v2.0 JSON has no `kind`; contents or a lock make an item a
            # container, which is the same test the old loader implied.
            container=bool(i_data.get("contains") or i_data.get("locked")),
            open=bool(i_data.get("state", {}).get("open", False)),
        )
        item.instance_id = i_data["id"]
        item_map[i_data["id"]] = item

    # 1b. Resolve container contents (loot declared inside each item). Done in
    # its own pass so referenced items always exist first.
    for i_data in data.get("items", []):
        container = item_map.get(i_data["id"])
        if container is None:
            continue
        for contained_id in i_data.get("contains", []):
            if contained_id in item_map:
                container.add_content(item_map[contained_id])

    # 2. Second pass: Create all Rooms
    for r_data in data.get("rooms", []):
        room = Room(
            name=r_data["name"],
            description=r_data["description"],
            dark=bool(r_data.get("dark", False)),
        )
        # Keep the world-file id on the room so protocol v2 can expose a stable
        # room id instead of deriving one from the display name.
        room.item_id = r_data["id"]
        room_map[r_data["id"]] = room

    # 3. Third pass: Establish interconnections (Exits), geometry, and Inventory
    # A legacy JSON exit is either a room id or an object carrying the lock
    # fields the YAML model also has. Two directions of one locked door share
    # a single `ExitLock`, so using the key from either side unlocks it.
    locked_doors: dict[tuple[frozenset[str], str | None], ExitLock] = {}
    for r_data in data.get("rooms", []):
        current_room = room_map[r_data["id"]]

        # Handle exits (mapping direction to a room id or an exit object).
        for exit_name, value in r_data.get("exits", {}).items():
            locked = False
            key = None
            label = None
            if isinstance(value, dict):
                target_room_id = value.get("to")
                locked = bool(value.get("locked", False))
                key = value.get("key")
                label = value.get("label")
            else:
                target_room_id = value
            if not isinstance(target_room_id, str):
                continue
            if target_room_id in room_map:
                current_room.exits[exit_name] = room_map[target_room_id]
                if locked:
                    door = (
                        frozenset({r_data["id"], target_room_id}), key,
                    )
                    lock = locked_doors.get(door)
                    if lock is None:
                        lock = ExitLock(key=key, label=label)
                        locked_doors[door] = lock
                    current_room.exit_locks[exit_name] = lock

        # Validate against the resolved exits so the map can never describe a
        # door the engine cannot walk through.
        if "geometry" in r_data:
            current_room.geometry = validate_geometry(
                r_data["id"], r_data["geometry"], current_room.exits,
            )

        # Handle a fixed light source in the room, if any. The value is the id of
        # an item (built in pass 1); that item sheds light on the room so it is
        # never dark. It is added to the room's contents as its light.
        light_id = r_data.get("light_source")
        if light_id and light_id in item_map:
            current_room.light_source = item_map[light_id]

        # Handle items in room
        for item_id in r_data.get("item_ids", []):
            if item_id in item_map:
                current_room.add_content(item_map[item_id])

    # 4. Finalize Engine state
    for room in room_map.values():
        engine._rooms[room.name] = room

    start_room_id = data.get("start_location")
    if start_room_id in room_map:
        engine.start_room = room_map[start_room_id]


async def load_demo_village(engine: Engine) -> None:
    """Populate the engine from the product YAML world."""
    # Find project root relative to this file (src/mud/world/loader.py -> project root)
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[3]
    data_path = project_root / DEFAULT_WORLD

    await load_world(engine, data_path)

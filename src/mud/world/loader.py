import json
from pathlib import Path

from src.mud.config import DEFAULT_WORLD
from src.mud.content import Entity, Room
from src.mud.content.models import OPPOSITE_EXITS, Exit
from src.mud.content.models import load_world as load_content_world
from src.mud.content.validator import validate_world
from src.mud.engine.core import Engine
from src.mud.world.geometry import WorldDataError, validate_geometry

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
    """Build the engine from the v2.1 YAML model.

    The content model rejects a missing, malformed, or wrongly shaped file.
    The semantic validator then rejects a world that does not hold together
    (duplicate ids, dangling or doubly declared exits, unreachable rooms,
    container cycles, missing keys, malformed effects, bad geometry). Startup
    fails with those messages rather than serving a partial world.
    """
    loaded = load_content_world(data_path)
    errors = validate_world(loaded)
    if errors:
        raise WorldDataError("\n".join(errors))

    world = loaded.world

    # 1. First pass: create every object, globally. `contains` links by
    # object id, so a child may be declared in any room and resolved below.
    entity_map: dict[str, Entity] = {}
    contained_ids: set[str] = set()
    for room_data in world.rooms:
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
            # Kept for the v2.1.3 read/effects seam. The v2.0 adapter reads
            # `description`, so the YAML carries the same text in both places.
            entity.readable_text = obj.readable_text or obj.description
            entity_map[obj.id] = entity

    # 1b. Container contents, after every referenced object exists.
    for room_data in world.rooms:
        for obj in room_data.objects:
            container = entity_map[obj.id]
            for child_id in obj.contains:
                contained_ids.add(child_id)
                container.add_content(entity_map[child_id])

    # 2. Second pass: create all rooms.
    room_map: dict[str, Room] = {}
    for room_data in world.rooms:
        room = Room(
            name=room_data.name,
            description=room_data.description,
            dark=room_data.dark,
        )
        room.item_id = room_data.id
        room_map[room_data.id] = room

    # 3. Exits. The YAML declares each exit once; create the reciprocal here.
    # `one_way` opts out, and `setdefault` keeps an explicit target if a
    # later room happens to name the same direction (the validator rejects
    # that collision before this runs).
    for room_data in world.rooms:
        room = room_map[room_data.id]
        for direction, target in room_data.exits.items():
            if isinstance(target, Exit):
                target_id = target.to
                one_way = target.one_way
            else:
                target_id = target
                one_way = False
            destination = room_map[target_id]
            room.exits[direction] = destination
            opposite = OPPOSITE_EXITS.get(direction)
            if not one_way and opposite is not None:
                destination.exits.setdefault(opposite, room)

    # 4. Geometry, now that the effective exit map includes reciprocals.
    for room_data in world.rooms:
        if room_data.geometry is None:
            continue
        room = room_map[room_data.id]
        room.geometry = validate_geometry(
            room_data.id, room_data.geometry, room.exits,
        )

    # 5. Place objects. A contained object belongs to its container, not to
    # the room it is declared in.
    for room_data in world.rooms:
        room = room_map[room_data.id]
        for obj in room_data.objects:
            if obj.id not in contained_ids:
                room.add_content(entity_map[obj.id])

    # 6. Finalize engine state.
    for room in room_map.values():
        engine._rooms[room.name] = room
    engine._player_location = room_map[world.start_room]

    # Keep the legacy single-player placeholder for backward compatibility;
    # it is not a real item and must not be added to a room's contents.
    engine.player = Entity("Adventurer", "A traveler passing through.")


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
    for r_data in data.get("rooms", []):
        current_room = room_map[r_data["id"]]

        # Handle exits (mapping string key to actual Room object)
        for exit_name, target_room_id in r_data.get("exits", {}).items():
            if target_room_id in room_map:
                current_room.exits[exit_name] = room_map[target_room_id]

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
        engine._player_location = room_map[start_room_id]

    # Spawn legacy single-player placeholder. It is kept on the engine for
    # backward compatibility but must NOT be added to a room's contents: it is
    # not a real item, and showing up as one would make `look` report "Here you
    # see: Adventurer" — confusing phantom clutter in an otherwise empty start room.
    engine.player = Entity("Adventurer", "A traveler passing through.")


async def load_demo_village(engine: Engine) -> None:
    """Populate the engine from the product YAML world."""
    # Find project root relative to this file (src/mud/world/loader.py -> project root)
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[3]
    data_path = project_root / DEFAULT_WORLD

    await load_world(engine, data_path)

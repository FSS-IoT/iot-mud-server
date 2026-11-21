import json
from pathlib import Path
from typing import List
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

async def load_demo_village(engine: Engine) -> None:
    """
    Populates the engine from a JSON definition file.
    Implements TASK-015: Data-Driven Worlds.
    """
    # Find project root relative to this file (src/mud/world/loader.py -> project root)
    current_file = Path(__file__).resolve()
    project_root = current_file.parents[3] 
    data_path = project_root / "data" / "worlds" / "starting_village.json"
    
    if not data_path.exists():
        raise FileNotFoundError(f"World data file not found at {data_path}")

    with open(data_path, 'r') as f:
        data = json.load(f)

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
        )
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

    # 3. Third pass: Establish interconnections (Exits) and Inventory
    for r_data in data.get("rooms", []):
        current_room = room_map[r_data["id"]]

        # Handle exits (mapping string key to actual Room object)
        for exit_name, target_room_id in r_data.get("exits", {}).items():
            if target_room_id in room_map:
                current_room.exits[exit_name] = room_map[target_room_id]

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

"""The YAML world model.

World content is authored as YAML and validated before it reaches the engine.
A world is either one file:

    name: Club MUD
    start_room: gatehouse
    rng_seed: 20261112
    rooms: [...]

or a directory with `world.yaml` metadata and one file per area:

    world/
      world.yaml
      areas/
        gatehouse.yaml

An area file holds `area:` and `rooms:`. Exits are declared once; the loader
creates the reciprocal direction (`north`/`south`, `east`/`west`,
`up`/`down`, `in`/`out`) unless the exit says `one_way: true`. The validator
rejects a world that declares both directions by hand so the two cannot drift.

The model is deliberately strict: unknown fields are errors. A typo in a field
name is an authoring bug, and a parallel author should hear about it before the
world reaches a player.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# Global ids are slugs. The validator also enforces uniqueness across rooms,
# objects, and NPCs; pydantic enforces the shape of each id.
ID_PATTERN = r"^[a-z0-9_]+$"

# Exit directions the engine's movement vocabulary understands. A world may
# not invent `northeast` without teaching the engine and the protocol first.
EXIT_DIRECTIONS = frozenset(
    {"north", "south", "east", "west", "up", "down", "in", "out"}
)

# The reciprocal the loader creates for a declared exit. Directions absent
# here are one-way by nature (the loader has no opposite to create).
OPPOSITE_EXITS = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "up": "down",
    "down": "up",
    "in": "out",
    "out": "in",
}

# Triggers an effect may hang from. The effect *type* vocabulary stays open
# (`light.set`, `sound.play`, ...): devices map effects to hardware, so the
# validator checks the shape of a dotted name, not a closed list.
EFFECT_TRIGGERS = frozenset(
    {
        "on_enter",
        "on_exit",
        "on_take",
        "on_drop",
        "on_use",
        "on_open",
        "on_close",
        "on_talk",
        "on_device",
    }
)

EFFECT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

# Full body equipment slots. Hand items use one or both hand slots; the model
# authors `hand` once and the engine picks a free hand at runtime.
EQUIP_SLOTS = frozenset(
    {"hand", "head", "body", "back", "belt", "feet"}
)
EquipSlot = Literal["hand", "head", "body", "back", "belt", "feet"]


def _check_effect_triggers(value: dict[str, list[Effect]]) -> dict:
    unknown = sorted(set(value) - EFFECT_TRIGGERS)
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        allowed = ", ".join(sorted(EFFECT_TRIGGERS))
        raise ValueError(
            f"unknown effect trigger(s) {names}; expected one of {allowed}"
        )
    return value


class Effect(BaseModel):
    """One effect in a trigger list: a dotted name plus JSON-safe params."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=EFFECT_TYPE_PATTERN)
    params: dict[str, Any] = Field(default_factory=dict)


Effects = Annotated[
    dict[str, list[Effect]], AfterValidator(_check_effect_triggers)
]


class Exit(BaseModel):
    """A detailed exit. A plain string is shorthand for `{to: <room_id>}`."""

    model_config = ConfigDict(extra="forbid")

    to: str = Field(pattern=ID_PATTERN)
    locked: bool = False
    key: str | None = Field(default=None, pattern=ID_PATTERN)
    label: str | None = None
    one_way: bool = False


ExitTarget = str | Exit


class Dialogue(BaseModel):
    """Scripted NPC speech. `topics` is the `ask <npc> about <topic>` map."""

    model_config = ConfigDict(extra="forbid")

    greeting: str = ""
    default: str = ""
    topics: dict[str, str] = Field(default_factory=dict)


class GameObject(BaseModel):
    """An item, container, or readable. `contains` links by global object id."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    description: str = ""
    kind: Literal["item", "container", "readable"] = "item"
    portable: bool = True
    weight: float = Field(default=0.0, ge=0)
    readable_text: str | None = None
    locked: bool = False
    required_key: str | None = Field(default=None, pattern=ID_PATTERN)
    light_source: bool = False
    lit: bool = False
    # The item leaves a sleeping character and lands in its room (#72). The
    # gold chest key uses it; the rest of the inventory stays.
    dropped_on_sleep: bool = False
    state: dict[str, Any] = Field(default_factory=dict)
    contains: list[str] = Field(default_factory=list)
    effects: Effects = Field(default_factory=dict)
    # Where the item can be equipped. `hand` means one free hand, or both when
    # `hands: 2`. Empty means the item cannot be equipped.
    equip_slots: list[EquipSlot] = Field(default_factory=list)
    hands: Literal[1, 2] = 1

    @model_validator(mode="after")
    def _check_equipment(self) -> GameObject:
        if len(set(self.equip_slots)) != len(self.equip_slots):
            raise ValueError("equip_slots must not repeat a slot")
        if self.hands == 2 and "hand" not in self.equip_slots:
            raise ValueError("hands: 2 requires 'hand' in equip_slots")
        return self


class NPC(BaseModel):
    """A scripted NPC. Wandering is deterministic and seeded at runtime."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    description: str = ""
    dialogue: Dialogue = Field(default_factory=Dialogue)
    wander: bool = False
    wander_every: int = Field(default=15, gt=0)
    effects: Effects = Field(default_factory=dict)


class Room(BaseModel):
    """A room: its text, exits, objects, NPCs, effects, and optional map."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    description: str = ""
    area: str | None = Field(default=None, pattern=ID_PATTERN)
    dark: bool = False
    exits: dict[str, ExitTarget] = Field(default_factory=dict)
    objects: list[GameObject] = Field(default_factory=list)
    npcs: list[NPC] = Field(default_factory=list)
    effects: Effects = Field(default_factory=dict)
    # Optional authoring data for map-rendering clients. The semantic validator
    # reuses `src.mud.world.geometry` so YAML and the v2.0 JSON world agree.
    geometry: dict[str, Any] | None = None

    @field_validator("exits")
    @classmethod
    def _known_directions(cls, value: dict[str, ExitTarget]) -> dict:
        unknown = sorted(set(value) - EXIT_DIRECTIONS)
        if unknown:
            names = ", ".join(repr(name) for name in unknown)
            allowed = ", ".join(sorted(EXIT_DIRECTIONS))
            raise ValueError(
                f"unknown exit direction(s) {names}; expected one of {allowed}"
            )
        return value


class Area(BaseModel):
    """A group of rooms, one per YAML file under `areas/`."""

    model_config = ConfigDict(extra="forbid")

    area: str = Field(pattern=ID_PATTERN)
    rooms: list[Room] = Field(min_length=1)


class World(BaseModel):
    """World metadata and the flattened room list the loader consumes."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    start_room: str = Field(pattern=ID_PATTERN)
    rng_seed: int
    rooms: list[Room] = Field(default_factory=list)


class WorldFile(BaseModel):
    """The on-disk shape: a single file may inline `rooms` and/or `areas`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    start_room: str = Field(pattern=ID_PATTERN)
    rng_seed: int
    rooms: list[Room] = Field(default_factory=list)
    areas: list[Area] = Field(default_factory=list)


class ContentModelError(ValueError):
    """A file could not be read, parsed, or shaped into the world model."""


@dataclass(frozen=True)
class LoadedWorld:
    """A parsed world plus the file each room came from.

    `room_files[i]` belongs to `world.rooms[i]`. Validator messages use it so a
    parallel author knows which area file to open.
    """

    world: World
    room_files: tuple[Path, ...]
    source: Path


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContentModelError(
            f"{path}: cannot read world file: {exc}"
        ) from exc

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise ContentModelError(f"{path}: invalid YAML{where}: {exc}") from exc


def _value_at(raw: Any, loc: tuple[Any, ...]) -> Any:
    value = raw
    for part in loc:
        if isinstance(value, dict):
            if part not in value:
                return None
            value = value[part]
        elif (
            isinstance(value, list)
            and isinstance(part, int)
            and 0 <= part < len(value)
        ):
            value = value[part]
        else:
            return None
    return value


def _context(
    raw: Any, loc: tuple[Any, ...]
) -> tuple[str | None, str | None]:
    """Find the room id and nearest entity id for a pydantic error path."""
    room_id: str | None = None
    ident: str | None = None

    for index, part in enumerate(loc):
        if part != "rooms":
            continue
        if index + 1 >= len(loc) or not isinstance(loc[index + 1], int):
            continue
        room_raw = _value_at(raw, loc[: index + 2])
        if not isinstance(room_raw, dict):
            continue
        candidate = room_raw.get("id")
        if isinstance(candidate, str):
            room_id = candidate
            ident = candidate

        # A nested object or NPC error names that entity, not only the room.
        rest = loc[index + 2:]
        for offset, key in enumerate(rest):
            if key not in ("objects", "npcs"):
                continue
            if offset + 1 >= len(rest) or not isinstance(rest[offset + 1], int):
                continue
            entity_raw = _value_at(room_raw, rest[: offset + 2])
            if isinstance(entity_raw, dict):
                nested = entity_raw.get("id")
                if isinstance(nested, str):
                    ident = nested
            break
        break

    return room_id, ident


def _validation_error(
    path: Path, exc: ValidationError, raw: Any
) -> ContentModelError:
    messages: list[str] = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        room_id, ident = _context(raw, loc)
        where = ".".join(str(part) for part in loc) or "world"
        message = error.get("msg", "invalid value")

        if room_id is not None:
            messages.append(
                f"{path}: room {room_id!r} id {ident!r}: "
                f"{where}: {message}"
            )
        else:
            messages.append(f"{path}: {where}: {message}")

    return ContentModelError("\n".join(messages))


def _parse_area(path: Path, raw: Any) -> Area:
    if not isinstance(raw, dict):
        raise ContentModelError(f"{path}: area file must be a YAML mapping")
    try:
        return Area.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(path, exc, raw) from exc


def _flatten(
    rooms: list[Room],
    areas: list[Area],
    file: Path,
) -> list[tuple[Room, Path]]:
    flattened = [(room, file) for room in rooms]
    for area in areas:
        for room in area.rooms:
            if room.area is None:
                room = room.model_copy(update={"area": area.area})
            flattened.append((room, file))
    return flattened


def _load_file(path: Path) -> LoadedWorld:
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ContentModelError(f"{path}: world file must be a YAML mapping")
    try:
        document = WorldFile.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(path, exc, raw) from exc

    flattened = _flatten(document.rooms, document.areas, path)
    if not flattened:
        raise ContentModelError(f"{path}: world has no rooms")

    world = World(
        name=document.name,
        start_room=document.start_room,
        rng_seed=document.rng_seed,
        rooms=[room for room, _ in flattened],
    )
    return LoadedWorld(
        world=world,
        room_files=tuple(file for _, file in flattened),
        source=path,
    )


def _area_paths(directory: Path) -> list[Path]:
    areas_dir = directory / "areas"
    if not areas_dir.exists():
        return []
    if not areas_dir.is_dir():
        raise ContentModelError(f"{areas_dir}: expected an areas directory")

    paths = list(areas_dir.glob("*.yaml")) + list(areas_dir.glob("*.yml"))
    return sorted(paths, key=lambda candidate: candidate.name)


def _load_directory(path: Path) -> LoadedWorld:
    meta_path = path / "world.yaml"
    if not meta_path.is_file():
        raise ContentModelError(
            f"{path}: world.yaml not found; a world directory needs "
            f"world.yaml and an areas/ directory"
        )

    raw = _read_yaml(meta_path)
    if not isinstance(raw, dict):
        raise ContentModelError(f"{meta_path}: world file must be a YAML mapping")
    try:
        meta = WorldFile.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(meta_path, exc, raw) from exc

    flattened = _flatten(meta.rooms, meta.areas, meta_path)
    for area_path in _area_paths(path):
        area = _parse_area(area_path, _read_yaml(area_path))
        for room in area.rooms:
            if room.area is None:
                room = room.model_copy(update={"area": area.area})
            flattened.append((room, area_path))

    if not flattened:
        raise ContentModelError(f"{path}: world has no rooms")

    world = World(
        name=meta.name,
        start_room=meta.start_room,
        rng_seed=meta.rng_seed,
        rooms=[room for room, _ in flattened],
    )
    return LoadedWorld(
        world=world,
        room_files=tuple(file for _, file in flattened),
        source=meta_path,
    )


def load_world(path: str | Path) -> LoadedWorld:
    """Read and shape a YAML world file or directory without touching a DB.

    Raises `ContentModelError` with a file-qualified message for a missing
    file, invalid YAML, or a document that does not match the model. Semantic
    rules (ids, exits, reachability, containers, keys, effects) live in
    `validator.py`.
    """
    world_path = Path(path)
    if not world_path.exists():
        raise ContentModelError(f"{world_path}: world not found")
    if world_path.is_dir():
        return _load_directory(world_path)
    if not world_path.is_file():
        raise ContentModelError(f"{world_path}: world not found")
    return _load_file(world_path)

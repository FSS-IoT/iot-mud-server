"""Read, validate, and save YAML worlds for the experimental admin editor.

The editor is deliberately canonical: it loads the content model, hands the
browser JSON, and writes canonical YAML back. Comments and key ordering in the
authored files are not preserved; every save first copies the files it is about
to replace into ``backups/<timestamp>/`` and writes a manifest there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from src.mud.content.models import (
    Area,
    LoadedWorld,
    Room,
    World,
    WorldFile,
    load_world,
)
from src.mud.content.validator import validate_world

_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
YAML_SUFFIXES = frozenset({".yaml", ".yml"})


class WorldEditError(ValueError):
    """A draft world cannot be validated or saved."""

    def __init__(self, errors: list[str]):
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class DraftSection:
    """One area file (or inline room group) in a draft."""

    area: str | None
    file: str
    rooms: tuple[Room, ...]


@dataclass(frozen=True)
class Draft:
    """A validated in-memory world ready for canonical serialization."""

    meta: dict[str, Any]
    sections: tuple[DraftSection, ...]
    loaded: LoadedWorld


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _manifest(path: Path) -> dict[str, Any]:
    """The world files the editor reads, with hashes, for the UI header."""
    if path.is_dir():
        files = [path / "world.yaml"]
        areas = path / "areas"
        if areas.is_dir():
            files.extend(
                sorted(
                    candidate
                    for candidate in areas.iterdir()
                    if candidate.is_file()
                    and candidate.suffix.lower() in YAML_SUFFIXES
                )
            )
        root = path
    else:
        files = [path]
        root = path.parent

    entries = []
    for file in files:
        if not file.is_file():
            continue
        entries.append(
            {
                "path": _relative(file, root),
                "bytes": file.stat().st_size,
                "sha256": _sha256(file),
            }
        )
    return {"files": entries}


def _room_json(room: Room) -> dict[str, Any]:
    return room.model_dump(mode="json", exclude_none=True)


def read_world_payload(path: str | Path) -> dict[str, Any]:
    """Read a world file or directory as the editor's JSON document.

    The payload is valid even when the world has semantic errors; ``validation``
    carries them so the editor can show the problem instead of a blank page.
    """
    world_path = Path(path)
    if not world_path.exists():
        raise WorldEditError([f"{world_path}: world not found"])

    loaded = load_world(world_path)
    errors = validate_world(loaded)
    root = world_path if world_path.is_dir() else world_path.parent

    groups: dict[str, list[Room]] = {}
    for room, file in zip(loaded.world.rooms, loaded.room_files):
        rel = _relative(file, root) if world_path.is_dir() else file.name
        groups.setdefault(rel, []).append(room)

    areas = []
    for rel, rooms in groups.items():
        area = next((room.area for room in rooms if room.area), None)
        areas.append(
            {
                "area": area,
                "file": rel,
                "rooms": [_room_json(room) for room in rooms],
            }
        )

    return {
        "kind": "directory" if world_path.is_dir() else "file",
        "path": str(world_path),
        "world": {
            "name": loaded.world.name,
            "start_room": loaded.world.start_room,
            "rng_seed": loaded.world.rng_seed,
        },
        "areas": areas,
        "validation": {"ok": not errors, "errors": errors},
        "manifest": {
            **_manifest(world_path),
            "generated_at": datetime.now(UTC).isoformat(),
        },
    }


def _model_error(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "world"
        messages.append(f"world: {where}: {error.get('msg', 'invalid value')}")
    return messages


def _room_label(raw_room: Any) -> str:
    if isinstance(raw_room, dict):
        room_id = raw_room.get("id")
        if isinstance(room_id, str) and room_id:
            return f"world: room {room_id!r}"
    return "world: room"


def _room_model_error(raw_room: Any, exc: ValidationError) -> list[str]:
    label = _room_label(raw_room)
    messages = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "room"
        messages.append(
            f"{label}: {where}: {error.get('msg', 'invalid value')}")
    return messages


def _area_model_error(
    raw_rooms: list[Any], area: str, exc: ValidationError
) -> list[str]:
    messages = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        label = f"world: area {area!r}"
        if (
            len(loc) >= 2
            and loc[0] == "rooms"
            and isinstance(loc[1], int)
            and 0 <= loc[1] < len(raw_rooms)
        ):
            label = _room_label(raw_rooms[loc[1]])
            loc = loc[2:]
        where = ".".join(str(part) for part in loc) or "area"
        messages.append(
            f"{label}: {where}: {error.get('msg', 'invalid value')}")
    return messages


def _file_label(section: dict[str, Any], index: int) -> str:
    file = section.get("file")
    if isinstance(file, str) and file.strip():
        return file.strip()
    area = section.get("area")
    if isinstance(area, str) and area.strip():
        return f"areas/{area.strip()}.yaml"
    return f"areas/draft_{index}.yaml"


def _draft(payload: Any, source: Path) -> Draft:
    """Shape a browser payload into the content model and validate it."""
    if not isinstance(payload, dict):
        raise WorldEditError(["the world payload must be an object"])

    raw_meta = payload.get("world")
    if not isinstance(raw_meta, dict):
        raise WorldEditError(["world metadata is missing"])
    try:
        meta_file = WorldFile.model_validate(
            {
                "name": raw_meta.get("name"),
                "start_room": raw_meta.get("start_room"),
                "rng_seed": raw_meta.get("rng_seed"),
            }
        )
    except ValidationError as exc:
        raise WorldEditError(_model_error(exc)) from exc

    raw_areas = payload.get("areas")
    if not isinstance(raw_areas, list) or not raw_areas:
        raise WorldEditError(["the world must have at least one area"])

    sections: list[DraftSection] = []
    rooms: list[Room] = []
    room_files: list[Path] = []
    errors: list[str] = []

    for index, raw_section in enumerate(raw_areas):
        if not isinstance(raw_section, dict):
            errors.append(f"area {index}: must be an object")
            continue
        label = _file_label(raw_section, index)
        raw_rooms = raw_section.get("rooms")
        if not isinstance(raw_rooms, list) or not raw_rooms:
            errors.append(f"{label}: an area needs at least one room")
            continue

        area_name = raw_section.get("area")
        if area_name is None:
            validated_rooms: list[Room] = []
            for raw_room in raw_rooms:
                try:
                    validated_rooms.append(Room.model_validate(raw_room))
                except ValidationError as exc:
                    errors.extend(_room_model_error(raw_room, exc))
        else:
            try:
                area = Area.model_validate(
                    {"area": area_name, "rooms": raw_rooms})
            except ValidationError as exc:
                errors.extend(_area_model_error(raw_rooms, area_name, exc))
                continue
            area_name = area.area
            validated_rooms = list(area.rooms)

        if area_name is None:
            section_rooms = validated_rooms
        else:
            section_rooms = [
                room
                if room.area is not None
                else room.model_copy(update={"area": area_name})
                for room in validated_rooms
            ]
        sections.append(
            DraftSection(
                area=area_name if isinstance(area_name, str) else None,
                file=label,
                rooms=tuple(section_rooms),
            )
        )
        for room in section_rooms:
            rooms.append(room)
            room_files.append(Path(label))

    if errors:
        raise WorldEditError(errors)

    world = World(
        name=meta_file.name,
        start_room=meta_file.start_room,
        rng_seed=meta_file.rng_seed,
        rooms=rooms,
    )
    loaded = LoadedWorld(
        world=world,
        room_files=tuple(room_files),
        source=source,
    )
    semantic = validate_world(loaded)
    if semantic:
        raise WorldEditError(semantic)

    return Draft(
        meta={
            "name": world.name,
            "start_room": world.start_room,
            "rng_seed": world.rng_seed,
        },
        sections=tuple(sections),
        loaded=loaded,
    )


def validate_world_payload(
    payload: Any, path: str | Path
) -> list[str]:
    """Return every model or semantic error in a draft; empty means valid."""
    try:
        _draft(payload, Path(path))
    except WorldEditError as exc:
        return list(exc.errors)
    return []


def _room_yaml(room: Room, area: str | None) -> dict[str, Any]:
    data = room.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True)
    if area is not None and data.get("area") == area:
        data.pop("area", None)
    geometry = data.get("geometry")
    if isinstance(geometry, dict):
        # The editor always sends these two containers; keep canonical YAML
        # close to the authored style by omitting the empty ones.
        if not geometry.get("spawn"):
            geometry.pop("spawn", None)
        if not geometry.get("props"):
            geometry.pop("props", None)
    return data


def _yaml_text(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )


def _safe_area_name(section: DraftSection) -> str:
    name = Path(section.file).name
    if _SAFE_FILE.fullmatch(name) is None:
        raise WorldEditError(
            [f"{section.file}: area file names must be simple .yaml names"])
    return name


def _directory_texts(draft: Draft) -> dict[str, str]:
    """Canonical world.yaml and area files, keyed by relative path."""
    inline: list[dict[str, Any]] = []
    areas: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    seen: set[str] = set()

    for section in draft.sections:
        name = _safe_area_name(section)
        room_dicts = [_room_yaml(room, section.area) for room in section.rooms]
        if name == "world.yaml":
            inline.extend(room_dicts)
            continue
        if name in seen:
            raise WorldEditError([f"{name}: duplicate area file name"])
        seen.add(name)
        area_name = section.area
        if area_name is None:
            raise WorldEditError(
                [f"{name}: every area file needs an area name"])
        areas[name] = (area_name, room_dicts)

    meta: dict[str, Any] = dict(draft.meta)
    if inline:
        meta["rooms"] = inline

    texts = {"world.yaml": _yaml_text(meta)}
    for name, (area_name, rooms) in areas.items():
        texts[f"areas/{name}"] = _yaml_text(
            {"area": area_name, "rooms": rooms})
    return texts


def _file_text(draft: Draft) -> str:
    """Canonical single-file world: inline rooms, or areas when all named."""
    if all(section.area is not None for section in draft.sections):
        data: dict[str, Any] = dict(draft.meta)
        data["areas"] = [
            {
                "area": section.area,
                "rooms": [
                    _room_yaml(room, section.area) for room in section.rooms
                ],
            }
            for section in draft.sections
        ]
        return _yaml_text(data)

    rooms = []
    for section in draft.sections:
        for room in section.rooms:
            rooms.append(_room_yaml(room, None))
    data = dict(draft.meta)
    data["rooms"] = rooms
    return _yaml_text(data)


def _write_staging(path: Path, texts: dict[str, str]) -> Path:
    """Write canonical files to a sibling staging directory and validate it."""
    staging = Path(
        tempfile.mkdtemp(prefix=f".{path.name}.save-", dir=path.parent))
    try:
        for relative, text in texts.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        loaded = load_world(staging)
        errors = validate_world(loaded)
        if errors:
            raise WorldEditError(errors)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _write_staging_file(path: Path, text: str) -> Path:
    staging = Path(
        tempfile.mkdtemp(prefix=f".{path.name}.save-", dir=path.parent))
    try:
        target = staging / path.name
        target.write_text(text, encoding="utf-8")
        loaded = load_world(target)
        errors = validate_world(loaded)
        if errors:
            raise WorldEditError(errors)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    os.close(handle)
    staged = Path(raw)
    try:
        shutil.copy2(source, staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def _atomic_write(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    os.close(handle)
    staged = Path(raw)
    try:
        shutil.copyfile(source, staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def _backup(
    path: Path, targets: list[Path]
) -> tuple[Path, list[str], Path]:
    """Copy the files about to be replaced and write a manifest."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if path.is_dir():
        backup_root = path / "backups" / stamp
        root = path
    else:
        backup_root = path.parent / "backups" / f"{path.stem}-{stamp}"
        root = path.parent
    backup_root.mkdir(parents=True, exist_ok=True)

    entries = []
    backups: list[str] = []
    for target in targets:
        if not target.is_file():
            continue
        relative = _relative(target, root)
        destination = backup_root / relative
        _atomic_copy(target, destination)
        backups.append(str(destination))
        entries.append(
            {
                "path": relative,
                "backup": str(destination),
                "sha256": _sha256(target),
                "bytes": target.stat().st_size,
            }
        )

    manifest = backup_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": str(path),
                "created_at": datetime.now(UTC).isoformat(),
                "files": entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return backup_root, backups, manifest


def save_world_payload(path: str | Path, payload: Any) -> dict[str, Any]:
    """Validate a draft, back up the old files, and write canonical YAML.

    Every target is first written and validated in a sibling staging directory,
    so a bad draft never replaces a good file. Saves are atomic per file. The
    editor writes the whole document it was given; it does not delete area files
    the payload omits.
    """
    world_path = Path(path)
    if not world_path.exists():
        raise WorldEditError([f"{world_path}: world not found"])

    draft = _draft(payload, world_path)

    if world_path.is_dir():
        texts = _directory_texts(draft)
        staging = _write_staging(world_path, texts)
        targets = [world_path / relative for relative in texts]
        try:
            backup_root, backups, manifest = _backup(world_path, targets)
            for relative in texts:
                _atomic_write(staging / relative, world_path / relative)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    else:
        text = _file_text(draft)
        staging = _write_staging_file(world_path, text)
        try:
            backup_root, backups, manifest = _backup(
                world_path, [world_path])
            _atomic_write(staging / world_path.name, world_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "saved": [str(path) for path in targets],
        "backup_dir": str(backup_root),
        "backups": backups,
        "manifest": str(manifest),
        "world": dict(draft.meta),
        "areas": len(draft.sections),
        "rooms": len(draft.loaded.world.rooms),
    }

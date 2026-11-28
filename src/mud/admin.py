"""Offline administration commands.

Run as `python -m src.mud.admin <command>`.

`rekey` is the one command that prints a credential: the new character key
goes to stdout exactly once, after the transaction commits. Nothing logs it and
only its salt and scrypt hash are stored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.mud.auth import RekeyError, rekey_character
from src.mud.config import DEFAULT_WORLD, ConfigError, load_config
from src.mud.content.models import ContentModelError
from src.mud.content.validator import check_world
from src.mud.feedback import escape_controls
from src.mud.storage.db import (
    SCHEMA_VERSION,
    DatabaseError,
    migrate,
    open_database,
)
from src.mud.storage.store import PlayerStore, StorageWriteError
from src.mud.world.geometry import WorldDataError
from src.mud.world.seed import SeedError, SeedResult, seed_world

# Repository root: <root>/src/mud/admin.py
REPO_ROOT = Path(__file__).resolve().parents[2]

# Every IoT MUD database has these tables. A restore refuses a valid SQLite
# file that does not: a foreign database is a mistyped path, not a backup.
MUD_TABLES = frozenset(
    {
        "world_meta",
        "rooms",
        "players",
        "character_keys",
        "objects",
        "npcs",
        "exits",
        "event_log",
    }
)

DESTRUCTIVE_WARNING = (
    "warning: a reset is destructive: it replaces the world (rooms, exits, "
    "objects, and NPCs) and clears every character's inventory. Characters "
    "and their character keys are kept and placed at the new start room."
)


class OpsError(RuntimeError):
    """A backup or restore input is missing, foreign, or inconsistent."""


def _storage_args(parser: argparse.ArgumentParser) -> None:
    """The `--config` / `--db` flags every database-touching command accepts."""
    parser.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    parser.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.mud.admin",
        description="Offline IoT MUD administration",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_db = commands.add_parser(
        "init-db", help="create or migrate the SQLite database")
    init_db.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    init_db.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")

    content = commands.add_parser(
        "content", help="validate authored world content")
    content_commands = content.add_subparsers(
        dest="content_command", required=True)
    check = content_commands.add_parser(
        "check", help="validate a YAML world without touching the database")
    check.add_argument(
        "--world", type=Path, required=True,
        help="YAML world file, or a directory with world.yaml and areas/")

    seed = commands.add_parser(
        "seed", help="write the YAML world into the database")
    seed.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    seed.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    seed.add_argument(
        "--world", type=Path, default=None,
        help=f"YAML world file or directory (default: {DEFAULT_WORLD})")
    seed.add_argument(
        "--reset", action="store_true",
        help="replace an existing world; requires --yes")
    seed.add_argument(
        "--yes", action="store_true",
        help="confirm the destructive --reset")

    rekey = commands.add_parser(
        "rekey", help="issue a new character key and revoke the old ones")
    rekey.add_argument(
        "name", help="character name (case-insensitive)")
    rekey.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    rekey.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")

    capacity = commands.add_parser(
        "capacity", help="set a character's base carry capacity")
    capacity.add_argument(
        "name", help="character name (case-insensitive)")
    capacity.add_argument(
        "value", type=float, help="the new base carry capacity")
    capacity.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    capacity.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")

    backup = commands.add_parser(
        "backup", help="copy the database to a file (server must be offline)")
    backup.add_argument(
        "--to", type=Path, required=True,
        help="destination file for the backup")
    backup.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    backup.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    backup.add_argument(
        "--yes", action="store_true",
        help="overwrite an existing destination")

    restore = commands.add_parser(
        "restore", help="replace the database from a backup (server offline)")
    restore.add_argument(
        "--from", dest="source", type=Path, required=True,
        help="backup file to restore")
    restore.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    restore.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    restore.add_argument(
        "--yes", action="store_true",
        help="confirm replacing an existing target database")

    feedback = commands.add_parser(
        "feedback", help="inspect and manage player feedback reports")
    feedback_commands = feedback.add_subparsers(
        dest="feedback_command", required=True)

    feedback_list = feedback_commands.add_parser(
        "list", help="list feedback reports")
    _storage_args(feedback_list)

    feedback_show = feedback_commands.add_parser(
        "show", help="show one feedback report")
    feedback_show.add_argument("id", type=int, help="report id")
    _storage_args(feedback_show)

    feedback_export = feedback_commands.add_parser(
        "export", help="export feedback reports to a file")
    feedback_export.add_argument(
        "--format", choices=("markdown", "json"), required=True,
        help="output format")
    feedback_export.add_argument(
        "--output", type=Path, default=None,
        help="destination file (default: a generated name in the cwd)")
    _storage_args(feedback_export)

    feedback_delete = feedback_commands.add_parser(
        "delete", help="delete one feedback report")
    feedback_delete.add_argument("id", type=int, help="report id")
    feedback_delete.add_argument(
        "--yes", action="store_true",
        help="confirm deleting the report")
    _storage_args(feedback_delete)

    feedback_prune = feedback_commands.add_parser(
        "prune", help="delete reports older than a window")
    feedback_prune.add_argument(
        "--older-than", required=True, metavar="AGE",
        help="delete rows older than AGE (e.g. 90d)")
    _storage_args(feedback_prune)

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "init-db":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        path = args.db or config.storage.path
        try:
            conn = open_database(path)
        except DatabaseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        conn.close()
        print(f"initialized {path} (schema version {SCHEMA_VERSION})")
        return 0

    if args.command == "content":
        errors = check_world(args.world)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"{args.world}: ok")
        return 0

    if args.command == "seed":
        return _seed(args)

    if args.command == "rekey":
        return _rekey(args)

    if args.command == "capacity":
        return _capacity(args)

    if args.command == "backup":
        return _backup(args)

    if args.command == "restore":
        return _restore(args)

    if args.command == "feedback":
        return _feedback(args)

    return 2


def _seed(args: argparse.Namespace) -> int:
    """Seed or reset the world; return the process exit code."""
    if args.yes and not args.reset:
        print("error: --yes only confirms --reset and does nothing on its own",
              file=sys.stderr)
        return 2
    if args.reset and not args.yes:
        print(DESTRUCTIVE_WARNING, file=sys.stderr)
        print("error: --reset replaces the world; pass --yes to confirm",
              file=sys.stderr)
        return 2
    if args.reset:
        print(DESTRUCTIVE_WARNING, file=sys.stderr)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path = args.db or config.storage.path
    world_path = args.world or (REPO_ROOT / DEFAULT_WORLD)

    try:
        conn = open_database(path)
    except DatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        result = seed_world(conn, world_path, reset=args.reset)
    except (SeedError, ContentModelError, WorldDataError) as exc:
        for line in str(exc).splitlines():
            print(f"error: {line}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: could not write {path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(_seed_summary(result, path))
    return 0


def _seed_summary(result: SeedResult, path: Path) -> str:
    counts = (
        f"{result.rooms} rooms, {result.exits} exits, "
        f"{result.objects} objects, {result.npcs} NPCs"
    )
    if result.reset:
        characters = (
            f"{result.characters} character"
            f"{'' if result.characters == 1 else 's'}"
        )
        return (
            f"reset {result.name} in {path}: {counts}; kept {characters} at "
            f"start room {result.start_room!r} with inventory cleared; "
            f"epoch {result.epoch}"
        )
    return f"seeded {result.name} into {path}: {counts}; epoch {result.epoch}"


def _rekey(args: argparse.Namespace) -> int:
    """Issue a replacement key offline and print it exactly once.

    The database must already exist and hold a seeded world; a rekey is not a
    way to conjure an empty database by mistyping a path. The character's room
    and inventory are never written.
    """
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path = args.db or config.storage.path
    if not path.is_file():
        print(f"error: database not found: {path}", file=sys.stderr)
        return 1

    try:
        conn = open_database(path)
    except (DatabaseError, sqlite3.Error) as exc:
        print(f"error: could not open {path}: {exc}", file=sys.stderr)
        return 1

    try:
        if conn.execute(
            "SELECT 1 FROM world_meta WHERE key = 'start_room'"
        ).fetchone() is None:
            print(
                f"error: {path} has no world; run `admin seed` first",
                file=sys.stderr,
            )
            return 1
        result = rekey_character(conn, args.name)
    except RekeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: could not rekey in {path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # The one copy of the credential: stdout, after the commit. The summary
    # goes to stderr so stdout stays a single line a caller can capture.
    print(f"rekeyed {result.name}: {result.key}")
    revoked = len(result.revoked_key_ids)
    print(
        f"revoked {revoked} key{'' if revoked == 1 else 's'}; "
        "room and inventory unchanged; the new key is shown once and is not "
        "stored in plaintext",
        file=sys.stderr,
    )
    return 0


def _capacity(args: argparse.Namespace) -> int:
    """Set one character's base carry capacity and print the new value.

    The character is found by name (case-insensitively, as `players.name` is
    `COLLATE NOCASE`); the stored casing is kept. A missing character is an
    error and writes nothing.
    """
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path = args.db or config.storage.path
    if not path.is_file():
        print(f"error: database not found: {path}", file=sys.stderr)
        return 1

    try:
        conn = open_database(path)
    except (DatabaseError, sqlite3.Error) as exc:
        print(f"error: could not open {path}: {exc}", file=sys.stderr)
        return 1

    try:
        row = conn.execute(
            "SELECT name FROM players WHERE name = ?", (args.name,)
        ).fetchone()
        if row is None:
            print(f"error: no character named {args.name!r}", file=sys.stderr)
            return 1
        PlayerStore(conn, row["name"]).set_capacity_base(args.value)
    except (sqlite3.Error, StorageWriteError) as exc:
        print(f"error: could not set capacity in {path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"set {row['name']} capacity to {args.value}")
    return 0


def _storage_path(args: argparse.Namespace) -> Path | None:
    """Resolve `--db`, or `[storage].path`; print a config error and return None."""
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return args.db or config.storage.path


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """Open the database without creating it or writing to it."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _validate_database(path: Path, *, label: str) -> int:
    """Prove `path` is a current-or-older IoT MUD database; return its version.

    The check is read-only and ordered so the error names the real problem: a
    missing file, a file that is not SQLite, a foreign SQLite database, a
    corrupt database, or one written by a newer server.
    """
    if not path.is_file():
        raise OpsError(f"{label} not found: {path}")

    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        raise OpsError(f"could not open {path}: {exc}") from exc

    try:
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise OpsError(
                f"{path} is not a SQLite database: {exc}") from exc
    finally:
        conn.close()

    if version > SCHEMA_VERSION:
        raise OpsError(
            f"{path} has schema version {version}, newer than this server "
            f"supports ({SCHEMA_VERSION}); upgrade the server or restore an "
            "older backup"
        )

    missing = MUD_TABLES - tables
    if missing:
        raise OpsError(
            f"{path} is not an IoT MUD database "
            f"(missing tables: {', '.join(sorted(missing))})"
        )

    if integrity != "ok":
        raise OpsError(f"{path} failed SQLite integrity_check: {integrity}")

    return version


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _remove_database_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def _remove_stale_sidecars(path: Path) -> None:
    """Drop journals that belonged to the database being replaced.

    The server must be offline. A stale `-wal` replayed against a restored
    database would mix two worlds, so it goes with the old file.
    """
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def _copy_to_staging(source: Path, destination: Path) -> Path:
    """Copy `source` through SQLite's backup API into a sibling temp file."""
    handle, raw = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(handle)
    staged = Path(raw)
    try:
        source_conn = _connect_read_only(source)
        try:
            staged_conn = sqlite3.connect(staged)
            try:
                # The online backup API reads a consistent snapshot, including
                # committed rows still in the WAL.
                source_conn.backup(staged_conn)
                staged_conn.commit()
            finally:
                staged_conn.close()
        finally:
            source_conn.close()
    except BaseException:
        _remove_database_files(staged)
        raise
    return staged


def _backup(args: argparse.Namespace) -> int:
    """Copy the live database to `--to`; return the process exit code."""
    path = _storage_path(args)
    if path is None:
        return 2
    destination: Path = args.to

    if destination.resolve() == path.resolve():
        print(
            f"error: {destination} is already the source database; "
            "choose another --to path",
            file=sys.stderr,
        )
        return 2

    if not path.is_file():
        print(f"error: database not found: {path}", file=sys.stderr)
        return 1

    try:
        version = _validate_database(path, label="database")
    except (OpsError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if destination.exists() and not args.yes:
        print(
            f"error: {destination} exists; pass --yes to overwrite it",
            file=sys.stderr,
        )
        return 2

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = _copy_to_staging(path, destination)
        try:
            os.replace(staged, destination)
        except BaseException:
            _remove_database_files(staged)
            raise
        _remove_stale_sidecars(destination)
    except (sqlite3.Error, OSError) as exc:
        print(
            f"error: could not back up {path} to {destination}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"backed up {path} to {destination} (schema version {version})")
    return 0


def _restore(args: argparse.Namespace) -> int:
    """Replace the target database from `--from`; return the process exit code."""
    target = _storage_path(args)
    if target is None:
        return 2
    source: Path = args.source

    if source.resolve() == target.resolve():
        print(
            f"error: {source} is already the target database; "
            "choose another --db path",
            file=sys.stderr,
        )
        return 2

    try:
        source_version = _validate_database(source, label="backup")
    except (OpsError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if target.exists() and not args.yes:
        print(
            f"error: {target} exists; restoring replaces it, so the server "
            "must be offline; pass --yes to confirm",
            file=sys.stderr,
        )
        return 2

    if target.is_dir():
        print(
            f"error: {target} is a directory, not a database file",
            file=sys.stderr,
        )
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = _copy_to_staging(source, target)
        try:
            # An older backup is brought up to this server's schema before it
            # replaces the target; a newer one was already refused above.
            conn = sqlite3.connect(staged)
            try:
                migrate(conn, target)
                conn.commit()
            finally:
                conn.close()
            os.replace(staged, target)
        except BaseException:
            _remove_database_files(staged)
            raise
        _remove_stale_sidecars(target)
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(
            f"error: could not restore {source} to {target}: {exc}",
            file=sys.stderr,
        )
        return 1

    if source_version == SCHEMA_VERSION:
        detail = f"schema version {SCHEMA_VERSION}"
    else:
        detail = (
            f"migrated schema version {source_version} -> {SCHEMA_VERSION}"
        )
    print(f"restored {source} to {target} ({detail})")
    return 0


# ---------------------------------------------------------------------------
# Player feedback (#177)
# ---------------------------------------------------------------------------


def _feedback(args: argparse.Namespace) -> int:
    """Dispatch the nested `admin feedback` subcommands."""
    command = args.feedback_command
    if command == "list":
        return _feedback_list(args)
    if command == "show":
        return _feedback_show(args)
    if command == "export":
        return _feedback_export(args)
    if command == "delete":
        return _feedback_delete(args)
    if command == "prune":
        return _feedback_prune(args)
    return 2


def _feedback_open(args: argparse.Namespace) -> tuple[sqlite3.Connection | None, int]:
    """Open the configured database; return `(conn, exit_code)`.

    A config error already printed by `_storage_path` is exit 2; a missing or
    unreadable database is exit 1.
    """
    path = _storage_path(args)
    if path is None:
        return None, 2
    if not path.is_file():
        print(f"error: database not found: {path}", file=sys.stderr)
        return None, 1
    try:
        return open_database(path), 0
    except (DatabaseError, sqlite3.Error) as exc:
        print(f"error: could not open {path}: {exc}", file=sys.stderr)
        return None, 1


def _feedback_list(args: argparse.Namespace) -> int:
    conn, code = _feedback_open(args)
    if conn is None:
        return code
    try:
        rows = conn.execute(
            "SELECT id, created_at, player_name, category, text "
            "FROM feedback ORDER BY id").fetchall()
        if not rows:
            print("no feedback reports")
            return 0
        for row in rows:
            print(
                f"{row['id']}  {row['created_at']}  {row['player_name']}  "
                f"{row['category']}  {_preview(row['text'])}")
        return 0
    finally:
        conn.close()


def _feedback_show(args: argparse.Namespace) -> int:
    conn, code = _feedback_open(args)
    if conn is None:
        return code
    try:
        row = conn.execute(
            "SELECT id, player_name, text, category, room_id, room_name, "
            "world_epoch, server_commit, context, created_at "
            "FROM feedback WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(f"error: no feedback report {args.id}", file=sys.stderr)
            return 1

        lines = [
            f"id: {row['id']}",
            f"player: {row['player_name']}",
            f"category: {row['category']}",
            f"created_at: {row['created_at']}",
        ]
        if row["room_id"] is not None:
            room = row["room_id"]
            if row["room_name"]:
                room += f" ({row['room_name']})"
            lines.append(f"room: {room}")
        if row["world_epoch"] is not None:
            lines.append(f"world_epoch: {row['world_epoch']}")
        if row["server_commit"] is not None:
            lines.append(f"server_commit: {row['server_commit']}")
        lines.append("text:")
        lines.append(escape_controls(row["text"]))
        if row["context"]:
            lines.append("context:")
            lines.append(escape_controls(_pretty_context(row["context"])))
        _emit_report("\n".join(lines) + "\n")
        return 0
    finally:
        conn.close()


def _feedback_export(args: argparse.Namespace) -> int:
    conn, code = _feedback_open(args)
    if conn is None:
        return code
    try:
        rows = conn.execute(
            "SELECT id, player_name, text, category, room_id, room_name, "
            "world_epoch, server_commit, context, created_at "
            "FROM feedback ORDER BY id").fetchall()
    finally:
        conn.close()

    if args.format == "json":
        content = json.dumps(
            [_export_row(row) for row in rows],
            indent=2, ensure_ascii=False, sort_keys=True)
        suffix = ".json"
    else:
        content = _markdown_export(rows)
        suffix = ".md"

    path = args.output or _default_export_path(suffix)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"error: could not write {path}: {exc}", file=sys.stderr)
        return 1

    print(f"exported {len(rows)} report(s) to {path}")
    return 0


def _feedback_delete(args: argparse.Namespace) -> int:
    conn, code = _feedback_open(args)
    if conn is None:
        return code
    try:
        if not args.yes:
            print("error: pass --yes to delete a feedback report",
                  file=sys.stderr)
            return 2
        row = conn.execute(
            "SELECT id FROM feedback WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(f"error: no feedback report {args.id}", file=sys.stderr)
            return 1
        with conn:
            conn.execute("DELETE FROM feedback WHERE id = ?", (args.id,))
        # The id only: the text is never logged.
        print(f"deleted feedback {args.id}")
        return 0
    finally:
        conn.close()


def _feedback_prune(args: argparse.Namespace) -> int:
    conn, code = _feedback_open(args)
    if conn is None:
        return code
    try:
        try:
            delta = _parse_age(args.older_than)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        cutoff = _iso(datetime.now(UTC) - delta)
        with conn:
            removed = conn.execute(
                "DELETE FROM feedback WHERE created_at < ?", (cutoff,)
            ).rowcount
        print(f"pruned {removed} report(s) older than {args.older_than}")
        return 0
    finally:
        conn.close()


def _preview(text: str, limit: int = 60) -> str:
    """One escaped, truncated line for `feedback list`."""
    escaped = escape_controls(text)
    if len(escaped) <= limit:
        return escaped
    return escaped[: limit - 1] + "\u2026"


def _pretty_context(raw: str) -> str:
    """Decode and pretty-print the stored context JSON, or return it unchanged.

    The context is one JSON object written by `record_feedback`. Decoding then
    re-encoding keeps it readable for the operator; a value that is not JSON is
    shown as stored. Callers still escape the result before printing, so a C1
    control stored in command history never reaches the terminal raw.
    """
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    return raw


def _export_row(row: sqlite3.Row) -> dict:
    """A report dict with the context JSON decoded for a JSON export."""
    data = dict(row)
    if data.get("context"):
        try:
            data["context"] = json.loads(data["context"])
        except ValueError:
            pass
    return data


def _markdown_export(rows: list[sqlite3.Row]) -> str:
    """Render every report as a Markdown issue body, text fenced against
    injection.

    The fence is one backtick longer than any run inside the exported text, so
    the raw text can never close the fence and leak Markdown, links, or HTML.
    """
    texts = [row["text"] or "" for row in rows]
    fence = "`" * max(3, _longest_backtick_run("".join(texts)) + 1)
    parts = ["# IoT MUD player feedback\n"]
    for index, row in enumerate(rows, 1):
        parts.append(f"## Report {index}\n\n")
        parts.append(f"- **Player:** {row['player_name']}\n")
        parts.append(f"- **Category:** {row['category']}\n")
        parts.append(f"- **Submitted:** {row['created_at']}\n")
        if row["room_id"] is not None:
            room = row["room_id"]
            if row["room_name"]:
                room += f" ({row['room_name']})"
            parts.append(f"- **Room:** {room}\n")
        if row["world_epoch"] is not None:
            parts.append(f"- **World epoch:** {row['world_epoch']}\n")
        if row["server_commit"] is not None:
            parts.append(f"- **Server commit:** {row['server_commit']}\n")
        parts.append(f"\n{fence}\n{row['text'] or ''}\n{fence}\n\n---\n\n")
    return "".join(parts)


def _longest_backtick_run(text: str) -> int:
    """The longest run of consecutive backticks in `text`."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _default_export_path(suffix: str) -> Path:
    """A generated export filename; it never contains the feedback text."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path(f"feedback-export-{stamp}{suffix}")


def _parse_age(value: str) -> timedelta:
    """Parse an age like `90d`, `12h`, or `30m` into a timedelta."""
    match = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if match is None:
        raise ValueError(f"invalid age {value!r}; use <number><s|m|h|d|w>")
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[
        match.group(2)]
    return timedelta(seconds=int(match.group(1)) * seconds)


def _iso(dt: datetime) -> str:
    """The same trailing-Z ISO form the server stores in `created_at`."""
    return dt.isoformat().replace("+00:00", "Z")


def _emit_report(text: str) -> None:
    """Print a report, paging it through `less` when stdout is a terminal."""
    if (sys.stdout.isatty()
            and text.count("\n") > 40
            and shutil.which("less") is not None):
        subprocess.run(["less", "-R"], input=text, text=True, check=False)
    else:
        print(text, end="")


if __name__ == "__main__":
    sys.exit(main())

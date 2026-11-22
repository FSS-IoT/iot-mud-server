-- Schema version 1. `PRAGMA user_version` is set by storage/db.py.
-- Every table uses IF NOT EXISTS so a failed first migration can be retried.

CREATE TABLE IF NOT EXISTS world_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    area        TEXT,
    dark        INTEGER NOT NULL DEFAULT 0 CHECK (dark IN (0, 1)),
    geometry    TEXT,
    version     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS players (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    room_id    TEXT REFERENCES rooms(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    asleep     INTEGER NOT NULL DEFAULT 0 CHECK (asleep IN (0, 1))
);

CREATE TABLE IF NOT EXISTS character_keys (
    key_id     TEXT PRIMARY KEY,
    player_id  INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    salt       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    kind          TEXT NOT NULL DEFAULT 'item',
    portable      INTEGER NOT NULL DEFAULT 1 CHECK (portable IN (0, 1)),
    readable_text TEXT,
    room_id       TEXT REFERENCES rooms(id) ON DELETE SET NULL,
    owner_id      INTEGER REFERENCES players(id) ON DELETE SET NULL,
    container_id  TEXT REFERENCES objects(id) ON DELETE SET NULL,
    state         TEXT NOT NULL DEFAULT '{}',
    CHECK (room_id IS NULL OR owner_id IS NULL),
    CHECK (room_id IS NULL OR container_id IS NULL),
    CHECK (owner_id IS NULL OR container_id IS NULL)
);

CREATE TABLE IF NOT EXISTS exits (
    room_id   TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    direction TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    locked    INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    key_id    TEXT REFERENCES objects(id) ON DELETE SET NULL,
    label     TEXT,
    PRIMARY KEY (room_id, direction)
);

CREATE TABLE IF NOT EXISTS npcs (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    room_id      TEXT REFERENCES rooms(id) ON DELETE SET NULL,
    dialogue     TEXT NOT NULL DEFAULT '{}',
    wander       INTEGER NOT NULL DEFAULT 0 CHECK (wander IN (0, 1)),
    wander_every INTEGER NOT NULL DEFAULT 15 CHECK (wander_every > 0)
);

CREATE TABLE IF NOT EXISTS event_log (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL,
    actor    TEXT,
    room_id  TEXT,
    payload  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_keys_player
    ON character_keys(player_id);
CREATE INDEX IF NOT EXISTS idx_players_room ON players(room_id);
CREATE INDEX IF NOT EXISTS idx_objects_room ON objects(room_id);
CREATE INDEX IF NOT EXISTS idx_objects_owner ON objects(owner_id);
CREATE INDEX IF NOT EXISTS idx_objects_container ON objects(container_id);
CREATE INDEX IF NOT EXISTS idx_npcs_room ON npcs(room_id);
CREATE INDEX IF NOT EXISTS idx_event_log_room ON event_log(room_id, seq);

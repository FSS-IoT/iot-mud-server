# Operations

This page is the offline path for the shared server: backup, restore, the
systemd unit, the database/config path, and what happens when a database is
newer than the server.

**Stop the server for both backup and restore.** The backup API produces a
consistent copy, including rows committed to the WAL, but an offline server also
means one known world epoch, no writes racing the copy, and no process holding
the file a restore replaces. With the unit below:

```bash
sudo systemctl stop iot-mud
```

Start it again only after the command has finished:

```bash
sudo systemctl start iot-mud
```

## Where the files are

| Thing | Default | Override |
|---|---|---|
| Database | `data/mud.db` | `--db PATH`, or `[storage].path` in `mud.toml` |
| Config | `./mud.toml` when it exists | `--config PATH` |
| systemd state directory | `/var/lib/iot-mud` | `StateDirectory=` in the unit |
| Service database | `/var/lib/iot-mud/mud.db` | `--db` in `ExecStart` |
| Backup file | none; `--to` is required | the operator names it |

`--db` wins over `[storage].path`; a missing default `mud.toml` is fine, a
missing `--config` file is an error.

The **backup destination is an owner decision, not a built-in path**. The owner
names it before the drill. This page uses `/var/backups/iot-mud/` only as an
example; `admin backup` has no default and will not guess.

The unit runs as a dedicated `iot-mud` system user. Create it once, before the
first start:

```bash
sudo useradd --system --home-dir /var/lib/iot-mud \
  --shell /usr/sbin/nologin iot-mud
```

Run the admin commands as root against the absolute database path. Restoring as
root creates a root-owned database file, so give it back to the service user
before starting (shown under [Restore](#restore)).

## First start

Install the dependencies in the checkout once. Then seed the world before the
first start; the unit will not invent one:

```bash
cd /opt/iot-mud
uv sync --all-extras

sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin seed \
  --db /var/lib/iot-mud/mud.db

# The seed ran as root; give the state directory to the service user.
sudo chown -R iot-mud:iot-mud /var/lib/iot-mud

sudo systemctl daemon-reload
sudo systemctl enable --now iot-mud
```

`admin seed` also creates the database and applies the schema if it does not
exist yet. Use `admin seed --reset --yes` only when replacing an existing world.

## Backup

From a checkout, while the server is stopped:

```bash
uv run python -m src.mud.admin backup \
  --db data/mud.db \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

For a host installed at `/opt/iot-mud`, use that checkout's interpreter:

```bash
sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin backup \
  --db /var/lib/iot-mud/mud.db \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

A config file works in place of `--db`:

```bash
uv run python -m src.mud.admin backup \
  --config mud.toml \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

What the command does and refuses:

- It copies through SQLite's online backup API (`Connection.backup`), not a
  file copy. A committed row still sitting in `mud.db-wal` is included and the
  snapshot is internally consistent.
- It refuses a missing database, a file that is not SQLite, a foreign SQLite
  file without the MUD tables, and a database that fails `integrity_check`.
- It refuses a newer `user_version`; a newer server wrote that database.
- It refuses `--to` equal to the source database.
- It refuses to overwrite an existing `--to` file unless `--yes` is passed.
- It writes a temporary file next to the destination and renames it into place,
  so a failed copy does not leave a half-written backup under the final name.

Success prints one line:

```
backed up data/mud.db to /var/backups/iot-mud/mud-2026-11-21.db (schema version 1)
```

The backup file is one SQLite database. Copy the single file, not `-wal` or
`-shm`; a completed backup has no sidecars. Keep it outside the repository and
outside the service's `StateDirectory`.

## Restore

```bash
sudo systemctl stop iot-mud

sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin restore \
  --from /var/backups/iot-mud/mud-2026-11-21.db \
  --db /var/lib/iot-mud/mud.db \
  --yes

# The restore ran as root; the service user must be able to write the file.
sudo chown iot-mud:iot-mud /var/lib/iot-mud/mud.db

sudo systemctl start iot-mud
```

Running the restore as the service user (`sudo -u iot-mud ...`) also produces
the right owner, but then the backup file must be readable by `iot-mud`. Pick
one path and check `ls -l /var/lib/iot-mud/mud.db` before starting; root-owned
`0600` is the usual cause of an "attempt to write a readonly database" error.

Target selection is the same as every other admin command: `--db` first, then
`[storage].path` from `--config`/`mud.toml`, then the built-in default.

**Restore policy: an existing target needs `--yes`.** Restoring a backup
replaces the target database, so the command refuses to touch an existing file
without that explicit confirmation and exits 2. A fresh path does not need it.
There is no "merge": either the whole backup replaces the target or nothing
changes.

The restore refuses:

- a missing backup file;
- a file that is not SQLite;
- a valid SQLite file that is not an IoT MUD database (missing `world_meta`,
  `rooms`, `players`, `character_keys`, `objects`, `npcs`, `exits`, or
  `event_log`);
- a database that fails `integrity_check`;
- a **newer** `user_version`, with the documented message below;
- `--from` equal to the target database.

An older backup is allowed: it is migrated to this server's schema before it
replaces the target. Success names both versions:

```
restored <backup> to <target> (migrated schema version <old> -> <current>)
```

The restore is staged in the target's directory and swapped in with an atomic
rename only after the copy and migration succeed. A failed restore leaves the
existing target untouched. After the swap, stale `-wal`, `-shm`, and `-journal`
files from the replaced database are removed; replaying them against the new
database would mix two worlds. This is another reason the server must be
offline.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | The backup or restore completed. |
| `1` | A database, backup, or filesystem problem; nothing was replaced. |
| `2` | A confirmation or usage problem: missing `--yes`, source equals target, or a bad config. |

## systemd unit

The generic unit is [`deploy/iot-mud.service`](../deploy/iot-mud.service). It is
a template, not a path baked into this checkout:

1. Copy it to `/etc/systemd/system/iot-mud.service`.
2. Replace `/opt/iot-mud` with the checkout (or installed copy) on this host.
   That directory must be the project root and must have a `.venv` created by
   `uv sync --all-extras`.
3. Reload and start:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now iot-mud
   ```

The unit sets:

- `StateDirectory=iot-mud`, so systemd creates `/var/lib/iot-mud` and makes it
  writable by the service. The database is
  `/var/lib/iot-mud/mud.db`, passed explicitly with `--db`.
- `Restart=on-failure` with a two-second delay. A clean stop is not restarted.
- `WorkingDirectory=/opt/iot-mud` and a `PATH` whose first entry is that
  checkout's `.venv/bin`, so `python -m src.mud.net.v2_server` uses the project
  dependencies.
- `User=iot-mud` and `Group=iot-mud`; the account is created once as shown
  above, and the service user must be able to read the checkout.
- Read-only system paths (`ProtectSystem=strict`, `ProtectHome=read-only`) and
  no new privileges; only the state directory is writable.

The unit binds `127.0.0.1` by default. Change `--host` to the tailnet address,
or to `0.0.0.0` only behind the tailnet-only firewall described in
[`HOSTING.md`](HOSTING.md), before members connect from other machines.

Check the unit before installing it:

```bash
systemd-analyze verify deploy/iot-mud.service
```

`systemd-analyze verify` must exit 0. The test suite runs the same check and
skips cleanly when `systemd-analyze` is not installed.

### Config file instead of a hard-coded path

The unit passes `--db`, which overrides `[storage].path`. A host that prefers a
config file can create `/opt/iot-mud/mud.toml`:

```toml
[storage]
path = "/var/lib/iot-mud/mud.db"
```

and remove `--db` from `ExecStart`. Do not point `[storage].path` at a path
outside the state directory unless the unit also grants write access to it.

## A newer `user_version`

`PRAGMA user_version` is the schema version. A database written by a newer
server must not be opened by an older one: the older code does not know the
newer tables or columns, and a downgrade migration does not exist.

The server and `admin restore` refuse with the same message:

```
<path> has schema version <new>, newer than this server supports (<supported>); upgrade the server or restore an older backup
```

For example:

```
/var/lib/iot-mud/mud.db has schema version 2, newer than this server supports (1); upgrade the server or restore an older backup
```

The server refuses before it binds a port. It does not migrate down, does not
open the world, and does not write. The two legal responses are:

1. run the newer server that wrote the database; or
2. restore a backup taken before the upgrade with `admin restore`.

The same check runs on a restore source, so a newer backup cannot be used to
downgrade a host. `admin restore` and `admin backup` also refuse a foreign
database rather than copying it.

## Backup drill

The owner names the destination before this runs. The drill proves the backup
is restorable without touching the live database:

1. `sudo systemctl stop iot-mud`
2. Take the backup with `admin backup --db /var/lib/iot-mud/mud.db --to <owner-destination>`.
3. Restore that backup into a scratch path, not the live one:
   `admin restore --from <owner-destination> --db /tmp/iot-mud-drill.db`.
4. Compare the world epoch and row counts in the scratch database, then delete
   it.
5. `sudo systemctl start iot-mud`

A manual copy of `mud.db` while the server is running is not a backup: it can
miss committed WAL frames or catch a write in progress. Use `admin backup`.

## Host packaging

A host module (for example the NixOS module) must mirror this page rather than
re-implement it:

- a state directory equivalent to `StateDirectory=iot-mud` and a database at
  `/var/lib/iot-mud/mud.db` (or pass the host's path explicitly with `--db`);
- `Restart=on-failure`;
- the source/checkout path as configuration, not a literal from this
  repository;
- stop the service before backup and restore;
- keep the newer-`user_version` refusal unchanged, so operators see the same
  message on every host.

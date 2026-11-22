# Admin console frontend

Experimental Svelte 5 single-page app for the held
`experimental/admin-console` branch. It is plain Svelte + Vite + TypeScript:
no SvelteKit, no SSR, and no router.

The server-side contract, security model, and operating notes are in
[`../ADMIN.md`](../ADMIN.md). This file covers the frontend package.

## Build and check

```bash
cd admin
npm ci
npm run check     # svelte-check, zero errors/warnings
npm run build     # writes ../admin/dist, committed on the branch
```

The production bundle is committed so the server runs without Node:

```text
admin/dist/index.html
admin/dist/assets/index-*.js
admin/dist/assets/index-*.css
```

Vite hashes asset names; commit the whole `dist/` directory after a build.
`admin/node_modules/` is ignored.

## Develop

Start the MUD server with the admin console enabled on port 8766:

```bash
export MUD_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
./run_server.sh
```

Then, in another terminal:

```bash
cd admin
npm install
npm run dev
```

Open the Vite URL it prints. Vite proxies `/api` to
`http://127.0.0.1:8766`, so the browser keeps one origin and the server needs no
CORS headers. The token is entered on the static login screen and stored in the
tab's `sessionStorage`.

## Layout

| Path | Responsibility |
|---|---|
| `src/App.svelte` | Login gate, token lifecycle, tabs, status refresh |
| `src/components/Login.svelte` | Static unauthenticated token screen |
| `src/components/StatusPanel.svelte` | Commit, world/paths, counts, features, limits |
| `src/components/PlayersPanel.svelte` | Roster, live overlay, one-time rekey |
| `src/components/WorldEditor.svelte` | Document state, validation, save/apply, area tree |
| `src/components/RoomEditor.svelte` | Room fields and the section editors |
| `src/components/GeometryEditor.svelte` | SVG cell grid, spawn points, props |
| `src/components/ExitsEditor.svelte` | Direction, target, lock, key, label, one-way |
| `src/components/ObjectsEditor.svelte` | Objects, flags, state JSON, containment |
| `src/components/NpcsEditor.svelte` | Dialogue greetings, defaults, topics, wandering |
| `src/components/EffectsEditor.svelte` | Trigger groups and JSON-safe effect params |
| `src/lib/api.ts` | Bearer fetch wrapper and typed API errors |
| `src/lib/types.ts` | Wire types, normalization, default geometry, YAML payload |

The UI intentionally keeps no defeated "backup" or "reset" actions: backups are
automatic on save, and destructive database operations stay in the offline CLI.

## API used

All routes need `Authorization: Bearer <token>`; all are same-origin; JSON in,
JSON out.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Commit, world, paths, rooms, players, limits |
| `GET` | `/api/players` | SQLite roster + live sessions + key counts |
| `POST` | `/api/players/{name}/rekey` | New key once; old keys revoked |
| `GET` | `/api/world` | World metadata, areas/rooms, manifest, validation |
| `POST` | `/api/world/validate` | Validate the posted draft |
| `POST` | `/api/world/save` | Validate, back up, write canonical YAML |
| `POST` | `/api/world/apply` | Reload the live engine; `{"force": true}` if players are online |

`POST /api/world/validate` and `/save` take the draft directly:

```json
{
  "world": { "name": "The Commons", "start_room": "gate", "rng_seed": 20261121 },
  "areas": [
    { "area": "caves", "file": "areas/caves.yaml", "rooms": [] }
  ]
}
```

## Behavior notes

- **Unsaved changes** — every edit marks the document dirty; the World tab shows
  a dot and the browser warns on unload. Apply is disabled until Save.
- **Validation errors** — the top panel lists all errors; errors naming the
  selected room also appear inline in that room.
- **Geometry** — clicking a cell cycles `#` → `.` → `+` → `^` → `v`. The default
  grid derives doors and stairs from the room's exits. Save validates the map,
  so a door without an exit (or a missing door) is reported, not silently
  written.
- **Canonical YAML** — Save writes canonical YAML and loses authored comments.
  That is documented in `../ADMIN.md`; the server keeps a timestamped backup.
- **Apply** — reloads from the saved YAML, closes MUD clients with `1001`, and
  leaves the admin listener up. Characters persist in SQLite and restore their
  room/inventory on the next login.

The Svelte components were checked with the official Svelte autofixer against
Svelte 5.59.1 and with `svelte-check`; both must stay clean.

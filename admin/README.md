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
| `src/components/WorldEditor.svelte` | Document state, validation, save/apply, area tree, Tiles/Connections toggle, inspector |
| `src/components/WorldTiles.svelte` | Spatial tile map, derived levels, conflicts, palette drops, grid generation |
| `src/components/WorldMap.svelte` | Connections graph, pan/zoom/fit, node drag, connect handles, palette drops |
| `src/components/RoomEditor.svelte` | Room fields, tabs, and the section editors |
| `src/components/GeometryEditor.svelte` | SVG cell grid, spawn points, props |
| `src/components/ExitsEditor.svelte` | Direction, target, lock, key, label, one-way |
| `src/components/ObjectsEditor.svelte` | Objects, flags, state JSON, containment |
| `src/components/NpcsEditor.svelte` | Dialogue greetings, defaults, topics, wandering |
| `src/components/EffectsEditor.svelte` | Trigger groups and JSON-safe effect params |
| `src/lib/api.ts` | Bearer fetch wrapper and typed API errors |
| `src/lib/types.ts` | Wire types, normalization, default geometry, YAML payload |
| `src/lib/layout.ts` | Deterministic auto-layout and layout bounds for Connections |
| `src/lib/worldTiles.ts` | Pure footprint/placement model for the Tiles view |

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
  "layout": { "gate": { "x": 60, "y": 60 } },
  "areas": [
    { "area": "caves", "file": "areas/caves.yaml", "rooms": [] }
  ]
}
```

## Map views

The toggle above the map chooses **Tiles** (default) or **Connections**. Both
views select the same room, so the tree and inspector stay in sync.

### Tiles

- **Derived placement** — each room's footprint is its authored `geometry`, or
  `defaultGeometry(room)` when missing. Cardinal exits align the two door cells
  and share a boundary row/column; one-way exits still place the target.
  `up`/`down` stack levels; `in`/`out` and unknown directions are portal badges.
- **Exact first, portals second** — the first placement never moves. A component
  that would overlap or repeat a position is relocated to a nearby free slot
  (within six cells) or to a separate Portals band. A dashed connector from the
  exact door/edge labels every portal. Subtree geometry and its up/down levels
  stay local; no footprints overlap.
- **Draw** — walls, floors, doors, stairs, props, and spawn points. Authored
  rooms have a solid outline; generated footprints are dashed. Area colour tints
  floors and outlines; labels carry name, id, object/NPC counts, `START`, and
  validation badges.
- **Warnings** — a `portals · cannot tile in 2D` banner lists each portal exit
  with source/target buttons and the reason (`relocated`/`cycle`). Genuine data
  errors (missing source doors/stairs, self-link, unknown room) have their own
  error box. Detached rooms have a separate band; `in`/`out` links are listed
  under a details block.
- **Generate missing grids** — asks for confirmation, writes the default 9×7
  grid into every room without geometry **in the draft**, marks it dirty, and
  leaves saving to the existing Save button. The grid includes reciprocal
  doors/stairs implied by neighbouring exits (the content validator checks the
  effective exit map). The view never mutates content itself. The per-room
  `Add default grid` button also uses the reciprocal directions.
- **Controls** — click a tile/room to select it; drag a palette chip onto a room
  or arm a chip and click. Drag empty canvas to pan; wheel/`+`/`−` zoom; **Fit**
  frames all levels, the Portals bands, and the Detached band.
- **No manual positions** — derived placement ignores `layout.json`. Switch to
  Connections for free dragging and exit handles.

### Connections

- **Select** — click a node or a tree entry. An edge click jumps to that room's
  Exits tab.
- **Move** — drag a node. **Pan** empty canvas. **Zoom** with the wheel, `+`, or
  `−`. The map opens centered on `start_room`; `Fit` frames every room;
  `Auto-layout` also rewrites positions and marks the draft dirty.
- **Add room** — `Add room`, then click empty canvas. The small form takes area,
  id, and name. The tree `+ room` still generates one.
- **Connect** — drag a node's `N`/`E`/`S`/`W` handle onto another node. The
  checkbox chooses one-way or the usual implicit reciprocal. A click on a handle
  arms the same connection for keyboard/touch, then click the target.
- **Palette** — drag `Item`, `Container`, `Readable`, or `NPC` onto a node, or
  click the chip once and then the room.

Connections positions live in `layout.json` at the world root. Save sanitizes it,
writes it atomically, and backs up the previous copy; the backup `manifest.json`
lists it. Malformed or missing layout files become an empty layout; the frontend
then auto-layouts deterministically. Tiles never writes positions.

## Behavior notes

- **One draft** — the tree, both map views, palette, and inspector all edit the
  same `WorldDoc`. Moving a node, dropping a chip, or generating grids marks the
  document dirty exactly like a form field does.
- **Tiles placement** — derived from exits and geometry, never from
  `layout.json`. Generated rooms are dashed until `Generate missing grids`
  writes their default grid into the draft. Levels stack; detached rooms have
  their own band.
- **Portals** — exact map edges that cannot be shared walls are listed with
  source/target buttons and dashed connectors. Relocated components keep their
  local geometry and levels in the Portals band. No room footprint overlaps
  another; a residual internal 2D cycle is reported as a data error.
- **Unsaved changes** — every edit marks the document dirty; the World tab shows
  a dot and the browser warns on unload. Apply is disabled until Save.
- **Validation errors** — the top panel lists all errors; errors naming the
  selected room also appear inline in that room, and every affected map node
  gets a red border and count badge.
- **Inspector tabs** — Room, Geometry, Exits, Objects, NPCs, Effects. Geometry
  is still the per-room tile grid; tabs only change which section is displayed.
- **Geometry** — clicking a cell cycles `#` → `.` → `+` → `^` → `v`. The default
  grid derives doors and stairs from the room's declared exits and the implied
  reciprocals of its neighbours. Save validates the map, so a door without an
  exit (or a missing door) is reported, not silently written.
- **Exits** — declare each way once. The loader creates the reciprocal unless
  `one_way` is set; declaring both directions is a validation error. The map's
  connect gesture follows that rule and shows both arrowheads for the implicit
  reciprocal.
- **Layout (Connections)** — `layout.json` is sanitized and written atomically
  with Save. If it is absent or malformed, Connections auto-layouts from
  `start_room` and the reachable exit graph, then a grid for disconnected rooms.
  Tiles ignores it.
- **Canonical YAML** — Save writes canonical YAML and loses authored comments.
  That is documented in `../ADMIN.md`; the server keeps a timestamped backup.
- **Apply** — reloads from the saved YAML, closes MUD clients with `1001`, and
  leaves the admin listener up. Characters persist in SQLite and restore their
  room/inventory on the next login.

The Svelte components were checked with the official Svelte autofixer against
Svelte 5.59.1 and with `svelte-check`; both must stay clean.

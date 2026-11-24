export interface LayoutPoint {
  x: number;
  y: number;
}

export type Layout = Record<string, LayoutPoint>;

export interface WorldMeta {
  name: string;
  start_room: string;
  rng_seed: number;
}

export interface ManifestFile {
  path: string;
  bytes: number;
  sha256: string;
}

export interface WorldManifest {
  files: ManifestFile[];
  generated_at?: string;
}

export interface Validation {
  ok: boolean;
  errors: string[];
}

export interface EffectEntry {
  type: string;
  params: Record<string, unknown>;
}

export type Effects = Record<string, EffectEntry[]>;

export interface ExitTarget {
  to: string;
  locked: boolean;
  key: string | null;
  label: string | null;
  one_way: boolean;
}

export interface Prop {
  kind: string;
  cell: [number, number];
}

export interface Geometry {
  cells: string[];
  spawn: Record<string, [number, number]>;
  props: Prop[];
}

export type CellChar = '#' | '.' | '+' | '^' | 'v';

export interface GameObject {
  id: string;
  name: string;
  description: string;
  kind: 'item' | 'container' | 'readable';
  portable: boolean;
  weight: number;
  readable_text: string | null;
  locked: boolean;
  required_key: string | null;
  light_source: boolean;
  lit: boolean;
  dropped_on_sleep: boolean;
  state: Record<string, unknown>;
  contains: string[];
  effects: Effects;
}

export interface NPC {
  id: string;
  name: string;
  description: string;
  dialogue: {
    greeting: string;
    default: string;
    topics: Record<string, string>;
  };
  wander: boolean;
  wander_every: number;
  effects: Effects;
}

export interface Room {
  id: string;
  name: string;
  description: string;
  area: string | null;
  dark: boolean;
  exits: Record<string, ExitTarget>;
  objects: GameObject[];
  npcs: NPC[];
  effects: Effects;
  geometry: Geometry | null;
}

export interface AreaSection {
  area: string | null;
  file: string;
  rooms: Room[];
}

export interface WorldDoc {
  kind: 'directory' | 'file';
  path: string;
  world: WorldMeta;
  areas: AreaSection[];
  validation: Validation;
  manifest: WorldManifest;
  layout: Layout;
}

export type PaletteKind = 'item' | 'container' | 'readable' | 'npc';

export type RoomTab =
  | 'room'
  | 'geometry'
  | 'exits'
  | 'objects'
  | 'npcs'
  | 'effects';

export interface ConnectRequest {
  sourceId: string;
  targetId: string;
  direction: string;
  oneWay: boolean;
}

export interface AddRoomRequest {
  areaIndex: number;
  id: string;
  name: string;
  x: number;
  y: number;
}

export interface Status {
  commit: string;
  world: {
    name: string;
    epoch: string;
  };
  world_path: string;
  db_path: string | null;
  rooms: number;
  players: number;
  live_sessions: number;
  live_players: number;
  features: Record<string, boolean>;
  limits: Record<string, number>;
  auth_limits: Record<string, number>;
}

export interface PlayerRow {
  name: string;
  room_id: string | null;
  room_name?: string | null;
  asleep: boolean;
  last_seen: string | null;
  key_count: number;
  online: boolean;
}

export interface PlayersResponse {
  players: PlayerRow[];
  live_sessions: number;
  live_players: number;
}

export interface RekeyResult {
  name: string;
  key: string;
  revoked: number;
  message: string;
}

export interface SaveResult {
  saved: string[];
  backup_dir: string;
  backups: string[];
  manifest: string;
  world: WorldMeta;
  areas: number;
  rooms: number;
  layout?: Layout;
}

export interface ApplyResult {
  applied: boolean;
  closed: number;
  world: {
    name: string;
    epoch: string;
  };
  players_notified: unknown[];
}

export const EXIT_DIRECTIONS = [
  'north',
  'south',
  'east',
  'west',
  'up',
  'down',
  'in',
  'out',
] as const;

export const OPPOSITE_EXITS: Record<string, string> = {
  north: 'south',
  south: 'north',
  east: 'west',
  west: 'east',
  up: 'down',
  down: 'up',
  in: 'out',
  out: 'in',
};

export const EFFECT_TRIGGERS = [
  'on_enter',
  'on_exit',
  'on_take',
  'on_drop',
  'on_use',
  'on_open',
  'on_close',
  'on_talk',
  'on_device',
] as const;

export const CELL_CHARS: CellChar[] = ['#', '.', '+', '^', 'v'];

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function optionalText(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function normalizeEffects(value: unknown): Effects {
  const effects: Effects = {};
  for (const [trigger, entries] of Object.entries(record(value))) {
    if (!Array.isArray(entries)) continue;
    effects[trigger] = entries.map((entry) => {
      const item = record(entry);
      return { type: text(item.type), params: record(item.params) };
    });
  }
  return effects;
}

function normalizeExit(value: unknown): ExitTarget {
  if (typeof value === 'string') {
    return { to: value, locked: false, key: null, label: null, one_way: false };
  }
  const exit = record(value);
  return {
    to: text(exit.to),
    locked: bool(exit.locked),
    key: optionalText(exit.key),
    label: optionalText(exit.label),
    one_way: bool(exit.one_way),
  };
}

function normalizeGeometry(value: unknown): Geometry | null {
  const geometry = record(value);
  if (!Array.isArray(geometry.cells) || geometry.cells.length === 0) {
    return null;
  }
  const spawn: Record<string, [number, number]> = {};
  for (const [direction, cell] of Object.entries(record(geometry.spawn))) {
    if (
      Array.isArray(cell) &&
      cell.length === 2 &&
      typeof cell[0] === 'number' &&
      typeof cell[1] === 'number'
    ) {
      spawn[direction] = [cell[0], cell[1]];
    }
  }
  const props: Prop[] = Array.isArray(geometry.props)
    ? geometry.props.flatMap((value) => {
        const prop = record(value);
        const cell = prop.cell;
        if (
          !Array.isArray(cell) ||
          cell.length !== 2 ||
          typeof cell[0] !== 'number' ||
          typeof cell[1] !== 'number'
        ) {
          return [];
        }
        return [{ kind: text(prop.kind), cell: [cell[0], cell[1]] }];
      })
    : [];
  return {
    cells: geometry.cells.map((row) => String(row)),
    spawn,
    props,
  };
}

function normalizeObject(value: unknown): GameObject {
  const object = record(value);
  const kind = text(object.kind, 'item');
  return {
    id: text(object.id),
    name: text(object.name),
    description: text(object.description),
    kind:
      kind === 'container' || kind === 'readable' ? kind : 'item',
    portable: bool(object.portable, true),
    weight: number(object.weight),
    readable_text: optionalText(object.readable_text),
    locked: bool(object.locked),
    required_key: optionalText(object.required_key),
    light_source: bool(object.light_source),
    lit: bool(object.lit),
    dropped_on_sleep: bool(object.dropped_on_sleep),
    state: record(object.state),
    contains: strings(object.contains),
    effects: normalizeEffects(object.effects),
  };
}

function normalizeNpc(value: unknown): NPC {
  const npc = record(value);
  const dialogue = record(npc.dialogue);
  const topics: Record<string, string> = {};
  for (const [topic, reply] of Object.entries(record(dialogue.topics))) {
    topics[topic] = String(reply);
  }
  return {
    id: text(npc.id),
    name: text(npc.name),
    description: text(npc.description),
    dialogue: {
      greeting: text(dialogue.greeting),
      default: text(dialogue.default),
      topics,
    },
    wander: bool(npc.wander),
    wander_every: number(npc.wander_every, 15),
    effects: normalizeEffects(npc.effects),
  };
}

export function normalizeLayout(value: unknown): Layout {
  const layout: Layout = {};
  for (const [roomId, point] of Object.entries(record(value))) {
    const entry = record(point);
    const x = entry.x;
    const y = entry.y;
    if (
      !roomId ||
      typeof x !== 'number' ||
      !Number.isFinite(x) ||
      typeof y !== 'number' ||
      !Number.isFinite(y)
    ) {
      continue;
    }
    layout[roomId] = { x, y };
  }
  return layout;
}

function normalizeRoom(value: unknown): Room {
  const room = record(value);
  const exits: Record<string, ExitTarget> = {};
  for (const [direction, exit] of Object.entries(record(room.exits))) {
    exits[direction] = normalizeExit(exit);
  }
  return {
    id: text(room.id),
    name: text(room.name),
    description: text(room.description),
    area: optionalText(room.area),
    dark: bool(room.dark),
    exits,
    objects: Array.isArray(room.objects)
      ? room.objects.map(normalizeObject)
      : [],
    npcs: Array.isArray(room.npcs) ? room.npcs.map(normalizeNpc) : [],
    effects: normalizeEffects(room.effects),
    geometry: normalizeGeometry(room.geometry),
  };
}

export function normalizeWorldDoc(value: unknown): WorldDoc {
  const doc = record(value);
  const world = record(doc.world);
  const manifest = record(doc.manifest);
  const validation = record(doc.validation);
  const areas: AreaSection[] = Array.isArray(doc.areas)
    ? doc.areas.map((value) => {
        const section = record(value);
        return {
          area: optionalText(section.area),
          file: text(section.file),
          rooms: Array.isArray(section.rooms)
            ? section.rooms.map(normalizeRoom)
            : [],
        };
      })
    : [];
  return {
    kind: doc.kind === 'file' ? 'file' : 'directory',
    path: text(doc.path),
    world: {
      name: text(world.name),
      start_room: text(world.start_room),
      rng_seed: number(world.rng_seed),
    },
    areas,
    validation: {
      ok: bool(validation.ok),
      errors: strings(validation.errors),
    },
    manifest: {
      files: Array.isArray(manifest.files)
        ? manifest.files.map((value) => {
            const file = record(value);
            return {
              path: text(file.path),
              bytes: number(file.bytes),
              sha256: text(file.sha256),
            };
          })
        : [],
      generated_at: text(manifest.generated_at) || undefined,
    },
    layout: normalizeLayout(doc.layout),
  };
}

export function worldPayload(doc: WorldDoc): Record<string, unknown> {
  return {
    world: { ...doc.world },
    layout: normalizeLayout(doc.layout),
    areas: doc.areas.map((section) => ({
      area: section.area,
      file: section.file,
      rooms: section.rooms.map((room) => ({
        id: room.id,
        name: room.name,
        description: room.description,
        area: room.area,
        dark: room.dark,
        exits: Object.fromEntries(
          Object.entries(room.exits).map(([direction, exit]) => [
            direction,
            {
              to: exit.to,
              locked: exit.locked,
              key: exit.key,
              label: exit.label,
              one_way: exit.one_way,
            },
          ]),
        ),
        objects: room.objects.map((object) => ({
          id: object.id,
          name: object.name,
          description: object.description,
          kind: object.kind,
          portable: object.portable,
          weight: object.weight,
          readable_text: object.readable_text,
          locked: object.locked,
          required_key: object.required_key,
          light_source: object.light_source,
          lit: object.lit,
          dropped_on_sleep: object.dropped_on_sleep,
          state: { ...object.state },
          contains: [...object.contains],
          effects: Object.fromEntries(
            Object.entries(object.effects).map(([trigger, entries]) => [
              trigger,
              entries.map((entry) => ({
                type: entry.type,
                params: { ...entry.params },
              })),
            ]),
          ),
        })),
        npcs: room.npcs.map((npc) => ({
          id: npc.id,
          name: npc.name,
          description: npc.description,
          dialogue: {
            greeting: npc.dialogue.greeting,
            default: npc.dialogue.default,
            topics: { ...npc.dialogue.topics },
          },
          wander: npc.wander,
          wander_every: npc.wander_every,
          effects: Object.fromEntries(
            Object.entries(npc.effects).map(([trigger, entries]) => [
              trigger,
              entries.map((entry) => ({
                type: entry.type,
                params: { ...entry.params },
              })),
            ]),
          ),
        })),
        effects: Object.fromEntries(
          Object.entries(room.effects).map(([trigger, entries]) => [
            trigger,
            entries.map((entry) => ({
              type: entry.type,
              params: { ...entry.params },
            })),
          ]),
        ),
        geometry: room.geometry
          ? {
              cells: [...room.geometry.cells],
              spawn: Object.fromEntries(
                Object.entries(room.geometry.spawn).map(([direction, cell]) => [
                  direction,
                  [cell[0], cell[1]],
                ]),
              ),
              props: room.geometry.props.map((prop) => ({
                kind: prop.kind,
                cell: [prop.cell[0], prop.cell[1]],
              })),
            }
          : null,
      })),
    })),
  };
}

function slugId(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'area'
  );
}

export function uniqueId(base: string, ids: Set<string>): string {
  const root = slugId(base);
  if (!ids.has(root)) return root;
  let counter = 2;
  while (ids.has(`${root}_${counter}`)) counter += 1;
  return `${root}_${counter}`;
}

export function newRoom(area: string | null, ids: Set<string>): Room {
  return {
    id: uniqueId('new_room', ids),
    name: 'New Room',
    description: '',
    area,
    dark: false,
    exits: {},
    objects: [],
    npcs: [],
    effects: {},
    geometry: null,
  };
}

export function defaultGeometry(
  room: Room,
  extraDirections: string[] = [],
): Geometry {
  const cells: string[][] = Array.from({ length: 7 }, (_, y) =>
    Array.from({ length: 9 }, (_, x) =>
      x === 0 || y === 0 || x === 8 || y === 6 ? '#' : '.',
    ),
  );
  const spawn: Record<string, [number, number]> = {};
  const exits = new Set([...Object.keys(room.exits), ...extraDirections]);
  if (exits.has('north')) {
    cells[0][4] = '+';
    spawn.north = [4, 1];
  }
  if (exits.has('south')) {
    cells[6][4] = '+';
    spawn.south = [4, 5];
  }
  if (exits.has('west')) {
    cells[3][0] = '+';
    spawn.west = [1, 3];
  }
  if (exits.has('east')) {
    cells[3][8] = '+';
    spawn.east = [7, 3];
  }
  if (exits.has('up')) {
    cells[3][3] = '^';
    spawn.up = [3, 3];
  }
  if (exits.has('down')) {
    cells[3][5] = 'v';
    spawn.down = [5, 3];
  }
  return { cells: cells.map((row) => row.join('')), spawn, props: [] };
}

export function shortCommit(commit: string): string {
  return commit.length > 10 ? commit.slice(0, 10) : commit;
}

export function formatWhen(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function filesize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

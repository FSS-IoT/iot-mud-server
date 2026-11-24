import type { Room } from './types';
import { defaultGeometry, OPPOSITE_EXITS } from './types';

export type Cardinal = 'north' | 'east' | 'south' | 'west';
export type LinkKind = 'cardinal' | 'vertical' | 'portal';
export type PlacementKind = 'exact' | 'free' | 'band' | 'detached';
export type PortalReason = 'relocated' | 'cycle' | 'alternate';
export type ConflictReason =
  | 'missing-source-door'
  | 'missing-source-stairs'
  | 'missing-room'
  | 'self-link'
  | 'start-room'
  | 'unplaceable';

export interface GridPoint {
  x: number;
  y: number;
}

export interface RoomFootprint {
  roomId: string;
  cells: string[];
  width: number;
  height: number;
  doors: Record<Cardinal, GridPoint[]>;
  stairs: { up: GridPoint[]; down: GridPoint[] };
  spawn: Record<string, [number, number]>;
  props: { kind: string; cell: [number, number] }[];
  authored: boolean;
}

export interface PlacedRoom {
  room: Room;
  footprint: RoomFootprint;
  level: number;
  intendedLevel: number;
  x: number;
  y: number;
  detached: boolean;
  exact: boolean;
  placement: PlacementKind;
  component: number;
}

export interface TileCell {
  x: number;
  y: number;
  char: string;
  owners: string[];
}

export interface TileBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

export interface PlacementLink {
  sourceId: string;
  targetId: string;
  direction: string;
  kind: LinkKind;
  oneWay: boolean;
  levelDelta: number;
  fromDetached: boolean;
}

export interface PlacementPortal {
  sourceId: string;
  targetId: string;
  direction: string;
  reason: PortalReason;
  oneWay: boolean;
  source: PlacedRoom;
  target: PlacedRoom;
}

export interface PlacementConflict {
  reason: ConflictReason;
  rooms: string[];
  direction?: string;
  level?: number;
  expected?: GridPoint;
  actual?: GridPoint;
  message: string;
}

export interface LevelLayout {
  level: number;
  label: string;
  kind: 'level' | 'portals' | 'detached';
  rooms: PlacedRoom[];
  cells: TileCell[];
  bounds: TileBounds;
}

export interface WorldPlacement {
  levels: LevelLayout[];
  placed: PlacedRoom[];
  exactRooms: PlacedRoom[];
  portalRooms: PlacedRoom[];
  portalPlacements: Map<string, PlacedRoom>;
  detached: PlacedRoom[];
  portals: PlacementPortal[];
  inOutPortals: PlacementLink[];
  errors: PlacementConflict[];
  links: PlacementLink[];
  portalBands: LevelLayout[];
  startId: string | null;
}

export interface TileMetricsLevel {
  level: LevelLayout;
  top: number;
  headingY: number;
  height: number;
}

export interface TileMetrics {
  levels: TileMetricsLevel[];
  minX: number;
  maxX: number;
  width: number;
  height: number;
}

export interface TileCamera {
  zoom: number;
  panX: number;
  panY: number;
}

export const CARDINAL_DIRECTIONS: Cardinal[] = [
  'north',
  'east',
  'south',
  'west',
];
export const EXIT_RANK: Record<string, number> = {
  north: 0,
  south: 1,
  east: 2,
  west: 3,
  up: 4,
  down: 5,
  in: 6,
  out: 7,
};
export const DETACHED_COLUMNS = 4;
export const DETACHED_GAP = 6;
export const DETACHED_COLUMN_GAP = 4;
export const PORTAL_BAND_BASE = -1000;
export const DETACHED_BAND_LEVEL = -2000;
export const PORTAL_BAND_GAP = 6;
export const FREE_SLOT_RADIUS = 6;

/**
 * Directions each room must draw to satisfy the content geometry validator:
 * its own declared exits plus the reciprocal the loader creates. The validator
 * uses the effective exit map, so a target with no authored reciprocal still
 * needs a door/stair for its neighbour.
 */
export function effectiveDirections(
  rooms: Room[],
): Record<string, string[]> {
  const byId = new Map(rooms.map((room) => [room.id, room]));
  const directions: Record<string, string[]> = {};
  const add = (roomId: string, direction: string): void => {
    const list = directions[roomId] ?? [];
    if (!list.includes(direction)) list.push(direction);
    directions[roomId] = list;
  };
  for (const room of rooms) {
    for (const direction of Object.keys(room.exits)) {
      add(room.id, direction);
    }
  }
  for (const room of rooms) {
    for (const [direction, exit] of Object.entries(room.exits)) {
      if (exit.one_way) continue;
      const target = byId.get(exit.to);
      if (!target) continue;
      const opposite = OPPOSITE_EXITS[direction];
      if (opposite) add(target.id, opposite);
    }
  }
  return directions;
}

const DOOR_PRIORITY: Record<string, number> = {
  '#': 3,
  '.': 2,
  '+': 5,
  '^': 4,
  v: 4,
};

function isCardinal(direction: string): direction is Cardinal {
  return (
    direction === 'north' ||
    direction === 'east' ||
    direction === 'south' ||
    direction === 'west'
  );
}

function sideFor(
  x: number,
  y: number,
  width: number,
  height: number,
): Cardinal | null {
  if (y === 0) return 'north';
  if (y === height - 1) return 'south';
  if (x === 0) return 'west';
  if (x === width - 1) return 'east';
  return null;
}

function sortPoints(points: GridPoint[]): GridPoint[] {
  return points.sort((left, right) =>
    left.y === right.y ? left.x - right.x : left.y - right.y,
  );
}

/**
 * A normalized room footprint. Authored geometry is copied exactly; a room
 * without geometry gets the same 9x7 default grid the Geometry tab offers.
 */
export function footprintFor(room: Room): RoomFootprint {
  const authored = Boolean(room.geometry && room.geometry.cells.length > 0);
  const geometry =
    authored && room.geometry ? room.geometry : defaultGeometry(room);
  const rows = geometry.cells.map((row) => [...row]);
  const width = Math.max(1, ...rows.map((row) => row.length));
  const cells = rows.map((row) => {
    while (row.length < width) row.push('#');
    return row.join('');
  });
  if (cells.length === 0) cells.push('#'.repeat(width));
  const height = cells.length;

  const doors: Record<Cardinal, GridPoint[]> = {
    north: [],
    east: [],
    south: [],
    west: [],
  };
  const stairs: { up: GridPoint[]; down: GridPoint[] } = {
    up: [],
    down: [],
  };
  for (let y = 0; y < height; y += 1) {
    const row = cells[y] ?? '';
    for (let x = 0; x < width; x += 1) {
      const char = row[x] ?? '#';
      if (char === '+') {
        const side = sideFor(x, y, width, height);
        if (side) doors[side].push({ x, y });
      } else if (char === '^') {
        stairs.up.push({ x, y });
      } else if (char === 'v') {
        stairs.down.push({ x, y });
      }
    }
  }
  for (const side of CARDINAL_DIRECTIONS) sortPoints(doors[side]);
  sortPoints(stairs.up);
  sortPoints(stairs.down);

  return {
    roomId: room.id,
    cells,
    width,
    height,
    doors,
    stairs,
    spawn: Object.fromEntries(
      Object.entries(geometry.spawn).map(([direction, cell]) => [
        direction,
        [cell[0], cell[1]] as [number, number],
      ]),
    ),
    props: geometry.props.map((prop) => ({
      kind: prop.kind,
      cell: [prop.cell[0], prop.cell[1]] as [number, number],
    })),
    authored,
  };
}

function pickDoor(
  footprint: RoomFootprint,
  side: Cardinal,
): { point: GridPoint; missing: boolean } {
  const door = footprint.doors[side][0];
  if (door) return { point: door, missing: false };
  if (side === 'north' || side === 'south') {
    return {
      point: {
        x: Math.floor((footprint.width - 1) / 2),
        y: side === 'north' ? 0 : footprint.height - 1,
      },
      missing: true,
    };
  }
  return {
    point: {
      x: side === 'west' ? 0 : footprint.width - 1,
      y: Math.floor((footprint.height - 1) / 2),
    },
    missing: true,
  };
}

/**
 * A target may not declare the reciprocal exit, so it can lack the door on
 * the facing side. Add that door to the placement copy only; the authored
 * geometry is untouched. The 5 authored start-village rooms already have
 * every facing door, so their cells never change.
 */
function ensureTargetDoor(
  footprint: RoomFootprint,
  side: Cardinal,
): RoomFootprint {
  const door = pickDoor(footprint, side);
  if (!door.missing) return footprint;
  const rows = footprint.cells.map((row) => [...row]);
  const row = rows[door.point.y];
  if (row) row[door.point.x] = '+';
  const doors: Record<Cardinal, GridPoint[]> = {
    north: [...footprint.doors.north],
    east: [...footprint.doors.east],
    south: [...footprint.doors.south],
    west: [...footprint.doors.west],
  };
  doors[side].push(door.point);
  sortPoints(doors[side]);
  return {
    ...footprint,
    cells: rows.map((row) => row.join('')),
    doors,
  };
}

function cardinalOrigin(
  source: PlacedRoom,
  target: RoomFootprint,
  direction: Cardinal,
): {
  x: number;
  y: number;
  sourceMissing: boolean;
  targetMissing: boolean;
} {
  const sourceDoor = pickDoor(source.footprint, direction);
  const opposite = OPPOSITE_EXITS[direction] as Cardinal | undefined;
  const targetDoor = pickDoor(target, opposite ?? 'north');
  switch (direction) {
    case 'east':
      return {
        x: source.x + source.footprint.width - 1,
        y: source.y + sourceDoor.point.y - targetDoor.point.y,
        sourceMissing: sourceDoor.missing,
        targetMissing: targetDoor.missing,
      };
    case 'west':
      return {
        x: source.x - target.width + 1,
        y: source.y + sourceDoor.point.y - targetDoor.point.y,
        sourceMissing: sourceDoor.missing,
        targetMissing: targetDoor.missing,
      };
    case 'south':
      return {
        x: source.x + sourceDoor.point.x - targetDoor.point.x,
        y: source.y + source.footprint.height - 1,
        sourceMissing: sourceDoor.missing,
        targetMissing: targetDoor.missing,
      };
    case 'north':
      return {
        x: source.x + sourceDoor.point.x - targetDoor.point.x,
        y: source.y - target.height + 1,
        sourceMissing: sourceDoor.missing,
        targetMissing: targetDoor.missing,
      };
  }
}

function verticalOrigin(
  source: PlacedRoom,
  target: RoomFootprint,
  direction: 'up' | 'down',
): {
  x: number;
  y: number;
  level: number;
  sourceMissing: boolean;
  targetMissing: boolean;
} {
  const sourceStair =
    direction === 'up'
      ? source.footprint.stairs.up[0]
      : source.footprint.stairs.down[0];
  const targetStair =
    direction === 'up'
      ? target.stairs.down[0]
      : target.stairs.up[0];
  const level = source.level + (direction === 'up' ? 1 : -1);
  if (!sourceStair || !targetStair) {
    return {
      x: source.x,
      y: source.y,
      level,
      sourceMissing: !sourceStair,
      targetMissing: !targetStair,
    };
  }
  return {
    x: source.x + sourceStair.x - targetStair.x,
    y: source.y + sourceStair.y - targetStair.y,
    level,
    sourceMissing: false,
    targetMissing: false,
  };
}

function priority(char: string): number {
  return DOOR_PRIORITY[char] ?? 1;
}

function buildCells(rooms: PlacedRoom[]): TileCell[] {
  const map = new Map<string, TileCell>();
  for (const placed of rooms) {
    for (let y = 0; y < placed.footprint.height; y += 1) {
      const row = placed.footprint.cells[y] ?? '';
      for (let x = 0; x < placed.footprint.width; x += 1) {
        const gx = placed.x + x;
        const gy = placed.y + y;
        const key = `${gx},${gy}`;
        const char = row[x] ?? '#';
        const existing = map.get(key);
        if (!existing) {
          map.set(key, { x: gx, y: gy, char, owners: [placed.room.id] });
          continue;
        }
        if (!existing.owners.includes(placed.room.id)) {
          existing.owners.push(placed.room.id);
        }
        if (priority(char) > priority(existing.char)) existing.char = char;
      }
    }
  }
  return [...map.values()].sort(
    (left, right) => left.y - right.y || left.x - right.x,
  );
}

export function tileBounds(cells: TileCell[]): TileBounds | null {
  if (cells.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const cell of cells) {
    minX = Math.min(minX, cell.x);
    minY = Math.min(minY, cell.y);
    maxX = Math.max(maxX, cell.x);
    maxY = Math.max(maxY, cell.y);
  }
  return {
    minX,
    minY,
    maxX,
    maxY,
    width: maxX - minX + 1,
    height: maxY - minY + 1,
  };
}

function roomOrderKey(room: Room): string {
  return `${room.area ?? '~'}\u0000${room.id}`;
}

function sortRooms(rooms: PlacedRoom[]): PlacedRoom[] {
  return [...rooms].sort((left, right) => {
    if (left.detached !== right.detached) return left.detached ? 1 : -1;
    if (left.y !== right.y) return left.y - right.y;
    if (left.x !== right.x) return left.x - right.x;
    return left.room.id.localeCompare(right.room.id);
  });
}

function placementConflictKey(conflict: PlacementConflict): string {
  return [
    conflict.reason,
    [...conflict.rooms].sort().join(','),
    conflict.direction ?? '',
    conflict.level ?? '',
  ].join('|');
}

function rectanglesOverlap(
  ax: number,
  ay: number,
  aw: number,
  ah: number,
  bx: number,
  by: number,
  bw: number,
  bh: number,
  allowSharedBoundary: boolean,
): boolean {
  const overlapX = Math.min(ax + aw, bx + bw) - Math.max(ax, bx);
  const overlapY = Math.min(ay + ah, by + bh) - Math.max(ay, by);
  if (overlapX <= 0 || overlapY <= 0) return false;
  if (allowSharedBoundary) return overlapX > 1 && overlapY > 1;
  return true;
}

function findFreeSlot(
  desired: GridPoint,
  preferred: string | undefined,
  isFree: (x: number, y: number) => boolean,
): GridPoint | null {
  if (isFree(desired.x, desired.y)) return { ...desired };
  for (let radius = 1; radius <= FREE_SLOT_RADIUS; radius += 1) {
    const candidates: GridPoint[] = [];
    for (let dx = -radius; dx <= radius; dx += 1) {
      for (let dy = -radius; dy <= radius; dy += 1) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== radius) continue;
        candidates.push({ x: dx, y: dy });
      }
    }
    const prefersPositiveX = preferred === 'east';
    const prefersNegativeX = preferred === 'west';
    const prefersNegativeY = preferred === 'north';
    const prefersPositiveY = preferred === 'south';
    candidates.sort((left, right) => {
      const score = (candidate: GridPoint): number => {
        if (prefersPositiveX) return candidate.x > 0 ? 0 : 1;
        if (prefersNegativeX) return candidate.x < 0 ? 0 : 1;
        if (prefersNegativeY) return candidate.y < 0 ? 0 : 1;
        if (prefersPositiveY) return candidate.y > 0 ? 0 : 1;
        return 0;
      };
      const scoreDiff = score(left) - score(right);
      if (scoreDiff !== 0) return scoreDiff;
      if (prefersPositiveX || prefersNegativeX) {
        if (Math.abs(left.y) !== Math.abs(right.y)) {
          return Math.abs(left.y) - Math.abs(right.y);
        }
      } else if (prefersNegativeY || prefersPositiveY) {
        if (Math.abs(left.x) !== Math.abs(right.x)) {
          return Math.abs(left.x) - Math.abs(right.x);
        }
      }
      if (left.x !== right.x) return left.x - right.x;
      return left.y - right.y;
    });
    for (const candidate of candidates) {
      const x = desired.x + candidate.x;
      const y = desired.y + candidate.y;
      if (isFree(x, y)) return { x, y };
    }
  }
  return null;
}

interface ExpectedPlacement {
  x: number;
  y: number;
  level: number;
  sourceMissing: boolean;
  targetMissing: boolean;
}

interface BlockedEdge {
  sourceId: string;
  targetId: string;
  direction: string;
  expected: ExpectedPlacement;
  oneWay: boolean;
  index: number;
}

interface ComponentTreeEdge {
  roomId: string;
  parentId: string | null;
  direction: string | null;
}

/**
 * Place every room in a continuous cell grid per level.
 *
 * Exact placements keep the first spatial position and are never moved. A room
 * that cannot take its exact position is relocated with its dependent tree to
 * a nearby free slot, or to a separate Portals band. Edges that stop being
 * shared walls become explicit portal links.
 */
export function placeWorld(rooms: Room[], startRoom: string): WorldPlacement {
  const byId = new Map(rooms.map((room) => [room.id, room]));
  const footprints = new Map(
    rooms.map((room) => [room.id, footprintFor(room)]),
  );
  const roomOrder = new Map(
    rooms.map((room, index) => [room.id, index] as const),
  );
  const exact = new Map<string, PlacedRoom>();
  const placed = new Map<string, PlacedRoom>();
  const errors: PlacementConflict[] = [];
  const errorKeys = new Set<string>();
  const blocked: BlockedEdge[] = [];
  const bandComponents: {
    componentId: number;
    component: Set<string>;
    tree: ComponentTreeEdge[];
    rootEdge?: BlockedEdge;
  }[] = [];

  function addError(conflict: PlacementConflict): void {
    const key = placementConflictKey(conflict);
    if (errorKeys.has(key)) return;
    errorKeys.add(key);
    errors.push(conflict);
  }

  function footprint(room: Room): RoomFootprint {
    return footprints.get(room.id) ?? footprintFor(room);
  }

  function expectedFor(
    source: PlacedRoom,
    target: Room,
    direction: string,
  ): ExpectedPlacement | null {
    const targetFootprint = footprint(target);
    if (isCardinal(direction)) {
      const expected = cardinalOrigin(source, targetFootprint, direction);
      return {
        x: expected.x,
        y: expected.y,
        level: source.level,
        sourceMissing: expected.sourceMissing,
        targetMissing: expected.targetMissing,
      };
    }
    if (direction === 'up' || direction === 'down') {
      const expected = verticalOrigin(
        source,
        targetFootprint,
        direction,
      );
      return {
        x: expected.x,
        y: expected.y,
        level: expected.level,
        sourceMissing: expected.sourceMissing,
        targetMissing: expected.targetMissing,
      };
    }
    return null;
  }

  function exactCollision(
    target: RoomFootprint,
    level: number,
    x: number,
    y: number,
  ): PlacedRoom | null {
    for (const other of exact.values()) {
      if (other.level !== level) continue;
      if (
        rectanglesOverlap(
          other.x,
          other.y,
          other.footprint.width,
          other.footprint.height,
          x,
          y,
          target.width,
          target.height,
          true,
        )
      ) {
        return other;
      }
    }
    return null;
  }

  function makeExact(
    room: Room,
    level: number,
    x: number,
    y: number,
  ): PlacedRoom {
    return {
      room,
      footprint: footprint(room),
      level,
      intendedLevel: level,
      x,
      y,
      detached: false,
      exact: true,
      placement: 'exact',
      component: 0,
    };
  }

  const start = byId.get(startRoom);
  if (!start) {
    addError({
      reason: 'start-room',
      rooms: [],
      message: `Start room '${startRoom}' is not declared; every room is detached.`,
    });
  } else {
    const placedStart = makeExact(start, 0, 0, 0);
    exact.set(start.id, placedStart);
    placed.set(start.id, placedStart);
  }

  // Phase 1: first/exact placements. A target whose exact cell would overlap
  // is recorded as blocked and not moved here.
  const queue = start ? [start.id] : [];
  let queueIndex = 0;
  while (queueIndex < queue.length) {
    const currentId = queue[queueIndex];
    queueIndex += 1;
    const current = currentId ? exact.get(currentId) : undefined;
    if (!current) continue;
    const exits = Object.entries(current.room.exits).sort(
      ([left], [right]) =>
        (EXIT_RANK[left] ?? 99) - (EXIT_RANK[right] ?? 99),
    );
    for (const [direction, exit] of exits) {
      if (!exit.to) continue;
      const target = byId.get(exit.to);
      if (!target) {
        addError({
          reason: 'missing-room',
          rooms: [current.room.id],
          direction,
          level: current.level,
          message: `Room '${current.room.id}' exit ${direction} points to unknown room '${exit.to}'.`,
        });
        continue;
      }
      if (target.id === current.room.id) {
        addError({
          reason: 'self-link',
          rooms: [current.room.id],
          direction,
          level: current.level,
          message: `Room '${current.room.id}' exit ${direction} links to itself.`,
        });
        continue;
      }
      if (direction === 'in' || direction === 'out') continue;
      const expected = expectedFor(current, target, direction);
      if (!expected) continue;
      if (exact.has(target.id)) continue;
      const collision = exactCollision(
        footprint(target),
        expected.level,
        expected.x,
        expected.y,
      );
      if (!collision) {
        const candidate = makeExact(
          target,
          expected.level,
          expected.x,
          expected.y,
        );
        exact.set(target.id, candidate);
        placed.set(target.id, candidate);
        queue.push(target.id);
        continue;
      }
      blocked.push({
        sourceId: current.room.id,
        targetId: target.id,
        direction,
        expected,
        oneWay: exit.one_way,
        index: blocked.length,
      });
    }
  }

  // Logical reachability uses every exit, including in/out. It only decides
  // whether an unplaced room belongs in the Portals band or the Detached band.
  const reachableAny = new Set<string>();
  if (start) {
    const reachQueue = [start.id];
    reachableAny.add(start.id);
    let index = 0;
    while (index < reachQueue.length) {
      const currentId = reachQueue[index];
      index += 1;
      const current = byId.get(currentId);
      if (!current) continue;
      for (const exit of Object.values(current.exits)) {
        if (!exit.to || !byId.has(exit.to)) continue;
        if (reachableAny.has(exit.to)) continue;
        reachableAny.add(exit.to);
        reachQueue.push(exit.to);
      }
    }
  }

  const unplacedReachable = rooms
    .filter((room) => !exact.has(room.id) && reachableAny.has(room.id))
    .sort((left, right) =>
      roomOrderKey(left).localeCompare(roomOrderKey(right)),
    );
  const unplacedDetached = rooms
    .filter((room) => !exact.has(room.id) && !reachableAny.has(room.id))
    .sort((left, right) =>
      roomOrderKey(left).localeCompare(roomOrderKey(right)),
    );
  const unplacedSet = new Set(unplacedReachable.map((room) => room.id));

  // Undirected adjacency for split-off components, with a direction that can
  // be used to place a parent -> child locally.
  const undirected = new Map<
    string,
    { id: string; direction: string }[]
  >();
  for (const room of unplacedReachable) {
    for (const [direction, exit] of Object.entries(room.exits)) {
      if (!unplacedSet.has(exit.to)) continue;
      const forward = undirected.get(room.id) ?? [];
      forward.push({ id: exit.to, direction });
      undirected.set(room.id, forward);
      const opposite = OPPOSITE_EXITS[direction] ?? direction;
      const backward = undirected.get(exit.to) ?? [];
      backward.push({ id: room.id, direction: opposite });
      undirected.set(exit.to, backward);
    }
  }
  for (const list of undirected.values()) {
    list.sort((left, right) => {
      const orderDiff =
        (roomOrder.get(left.id) ?? 0) - (roomOrder.get(right.id) ?? 0);
      return orderDiff || left.direction.localeCompare(right.direction);
    });
  }

  const blockedByTarget = new Map<string, BlockedEdge>();
  for (const edge of blocked) {
    const existing = blockedByTarget.get(edge.targetId);
    if (!existing || edge.index < existing.index) {
      blockedByTarget.set(edge.targetId, edge);
    }
  }

  const components: {
    rooms: Set<string>;
    root: string;
    rootEdge?: BlockedEdge;
    tree: ComponentTreeEdge[];
  }[] = [];
  const componentVisited = new Set<string>();
  for (const room of unplacedReachable) {
    if (componentVisited.has(room.id)) continue;
    const component = new Set<string>();
    const componentQueue = [room.id];
    componentVisited.add(room.id);
    let index = 0;
    while (index < componentQueue.length) {
      const currentId = componentQueue[index];
      index += 1;
      component.add(currentId);
      for (const edge of undirected.get(currentId) ?? []) {
        if (componentVisited.has(edge.id)) continue;
        componentVisited.add(edge.id);
        componentQueue.push(edge.id);
      }
    }
    const boundaryEdges = [...component]
      .map((id) => blockedByTarget.get(id))
      .filter((edge): edge is BlockedEdge => Boolean(edge))
      .sort((left, right) => left.index - right.index);
    const rootEdge = boundaryEdges[0];
    const root = rootEdge
      ? rootEdge.targetId
      : [...component].sort(
          (left, right) =>
            (roomOrder.get(left) ?? 0) - (roomOrder.get(right) ?? 0),
        )[0];
    if (!root) continue;

    const tree: ComponentTreeEdge[] = [
      { roomId: root, parentId: null, direction: null },
    ];
    const treeVisited = new Set([root]);
    let treeIndex = 0;
    while (treeIndex < tree.length) {
      const node = tree[treeIndex];
      treeIndex += 1;
      for (const edge of undirected.get(node.roomId) ?? []) {
        if (!component.has(edge.id) || treeVisited.has(edge.id)) continue;
        treeVisited.add(edge.id);
        tree.push({
          roomId: edge.id,
          parentId: node.roomId,
          direction: edge.direction,
        });
      }
    }
    for (const id of component) {
      if (treeVisited.has(id)) continue;
      tree.push({ roomId: id, parentId: null, direction: null });
    }
    components.push({ rooms: component, root, rootEdge, tree });
  }
  components.sort((left, right) => {
    const leftEdge = left.rootEdge?.index ?? Number.MAX_SAFE_INTEGER;
    const rightEdge = right.rootEdge?.index ?? Number.MAX_SAFE_INTEGER;
    if (leftEdge !== rightEdge) return leftEdge - rightEdge;
    return (
      (roomOrder.get(left.root) ?? 0) - (roomOrder.get(right.root) ?? 0)
    );
  });

  let nextComponent = 1;

  function relocatedCollision(
    target: RoomFootprint,
    level: number,
    x: number,
    y: number,
    componentId: number,
    temp: PlacedRoom[],
  ): boolean {
    for (const other of placed.values()) {
      if (other.level !== level) continue;
      if (
        rectanglesOverlap(
          other.x,
          other.y,
          other.footprint.width,
          other.footprint.height,
          x,
          y,
          target.width,
          target.height,
          false,
        )
      ) {
        return true;
      }
    }
    for (const other of temp) {
      if (other.level !== level) continue;
      if (
        rectanglesOverlap(
          other.x,
          other.y,
          other.footprint.width,
          other.footprint.height,
          x,
          y,
          target.width,
          target.height,
          other.component === componentId,
        )
      ) {
        return true;
      }
    }
    return false;
  }

  function tryFreeComponent(
    component: Set<string>,
    tree: ComponentTreeEdge[],
    rootEdge: BlockedEdge | undefined,
    componentId: number,
  ): boolean {
    if (!rootEdge) return false;
    const source = placed.get(rootEdge.sourceId);
    if (!source) return false;
    const rootRoom = byId.get(rootEdge.targetId);
    if (!rootRoom) return false;
    const rootFootprint = footprint(rootRoom);
    const expected = expectedFor(
      source,
      rootRoom,
      rootEdge.direction,
    );
    if (!expected) return false;
    const temp: PlacedRoom[] = [];
    const rootSlot = findFreeSlot(
      { x: expected.x, y: expected.y },
      rootEdge.direction,
      (x, y) =>
        !relocatedCollision(
          rootFootprint,
          expected.level,
          x,
          y,
          componentId,
          temp,
        ),
    );
    if (!rootSlot) return false;
    temp.push({
      room: rootRoom,
      footprint: rootFootprint,
      level: expected.level,
      intendedLevel: expected.level,
      x: rootSlot.x,
      y: rootSlot.y,
      detached: false,
      exact: false,
      placement: 'free',
      component: componentId,
    });

    for (const node of tree) {
      if (node.roomId === rootEdge.targetId) continue;
      if (!node.parentId || !node.direction) return false;
      const parent = temp.find(
        (candidate) => candidate.room.id === node.parentId,
      );
      const room = byId.get(node.roomId);
      if (!parent || !room) return false;
      const expectedChild = expectedFor(
        parent,
        room,
        node.direction,
      );
      if (!expectedChild) return false;
      const childFootprint = footprint(room);
      const slot = findFreeSlot(
        { x: expectedChild.x, y: expectedChild.y },
        node.direction,
        (x, y) =>
          !relocatedCollision(
            childFootprint,
            expectedChild.level,
            x,
            y,
            componentId,
            temp,
          ),
      );
      if (!slot) return false;
      temp.push({
        room,
        footprint: childFootprint,
        level: expectedChild.level,
        intendedLevel: expectedChild.level,
        x: slot.x,
        y: slot.y,
        detached: false,
        exact: false,
        placement: 'free',
        component: componentId,
      });
    }

    for (const candidate of temp) placed.set(candidate.room.id, candidate);
    return true;
  }

  for (const component of components) {
    const componentId = nextComponent;
    nextComponent += 1;
    if (
      !tryFreeComponent(
        component.rooms,
        component.tree,
        component.rootEdge,
        componentId,
      )
    ) {
      bandComponents.push({
        componentId,
        component: component.rooms,
        tree: component.tree,
        rootEdge: component.rootEdge,
      });
    }
  }

  // Phase 2: components that cannot sit near their exact parent go to the
  // clearly separated Portals band. The band keeps each component's local
  // relative layout where it fits.
  const bandRooms: PlacedRoom[] = [];
  let bandNextX = 0;

  function bandLevelFor(intendedLevel: number): number {
    return PORTAL_BAND_BASE + intendedLevel;
  }

  function bandCollision(
    target: RoomFootprint,
    level: number,
    x: number,
    y: number,
    componentId: number,
    temp: PlacedRoom[],
  ): boolean {
    for (const other of bandRooms) {
      if (other.level !== level) continue;
      if (
        rectanglesOverlap(
          other.x,
          other.y,
          other.footprint.width,
          other.footprint.height,
          x,
          y,
          target.width,
          target.height,
          false,
        )
      ) {
        return true;
      }
    }
    for (const other of temp) {
      if (other.level !== level) continue;
      if (
        rectanglesOverlap(
          other.x,
          other.y,
          other.footprint.width,
          other.footprint.height,
          x,
          y,
          target.width,
          target.height,
          other.component === componentId,
        )
      ) {
        return true;
      }
    }
    return false;
  }

  function placeBandComponent(
    componentId: number,
    component: Set<string>,
    tree: ComponentTreeEdge[],
    rootEdge: BlockedEdge | undefined,
  ): void {
    const rootId =
      rootEdge?.targetId ??
      [...component].sort(
        (left, right) =>
          (roomOrder.get(left) ?? 0) - (roomOrder.get(right) ?? 0),
      )[0];
    const rootRoom = rootId ? byId.get(rootId) : undefined;
    if (!rootRoom) return;
    const temp: PlacedRoom[] = [];
    const rootFootprint = footprint(rootRoom);
    const rootIntended = rootEdge?.expected.level ?? 0;
    const rootLevel = bandLevelFor(rootIntended);
    const rootSlot = findFreeSlot(
      { x: bandNextX, y: 0 },
      undefined,
      (x, y) =>
        !bandCollision(
          rootFootprint,
          rootLevel,
          x,
          y,
          componentId,
          temp,
        ),
    );
    if (!rootSlot) {
      addError({
        reason: 'unplaceable',
        rooms: [...component],
        message: `Rooms ${[...component].join(', ')} could not be placed in the Portals band without overlap.`,
      });
      return;
    }
    temp.push({
      room: rootRoom,
      footprint: rootFootprint,
      level: rootLevel,
      intendedLevel: rootIntended,
      x: rootSlot.x,
      y: rootSlot.y,
      detached: false,
      exact: false,
      placement: 'band',
      component: componentId,
    });

    for (const node of tree) {
      if (node.roomId === rootId) continue;
      if (!node.parentId || !node.direction) {
        temp.length = 0;
        break;
      }
      const parent = temp.find(
        (candidate) => candidate.room.id === node.parentId,
      );
      const room = byId.get(node.roomId);
      if (!parent || !room) {
        temp.length = 0;
        break;
      }
      const expected = expectedFor(
        { ...parent, level: parent.intendedLevel },
        room,
        node.direction,
      );
      if (!expected) {
        temp.length = 0;
        break;
      }
      const childFootprint = footprint(room);
      const childLevel = bandLevelFor(expected.level);
      const slot = findFreeSlot(
        { x: expected.x, y: expected.y },
        node.direction,
        (x, y) =>
          !bandCollision(
            childFootprint,
            childLevel,
            x,
            y,
            componentId,
            temp,
          ),
      );
      if (!slot) {
        temp.length = 0;
        break;
      }
      temp.push({
        room,
        footprint: childFootprint,
        level: childLevel,
        intendedLevel: expected.level,
        x: slot.x,
        y: slot.y,
        detached: false,
        exact: false,
        placement: 'band',
        component: componentId,
      });
    }

    if (temp.length === 0) {
      // A cyclic component can still be shown; grid its rooms rather than
      // dropping them, and report the genuine layout failure.
      addError({
        reason: 'unplaceable',
        rooms: [...component],
        message: `Rooms ${[...component].join(', ')} do not close in 2D; placed separately in the Portals band.`,
      });
      const fallbackY = new Map<number, number>();
      for (const id of [...component].sort(
        (left, right) =>
          (roomOrder.get(left) ?? 0) - (roomOrder.get(right) ?? 0),
      )) {
        const room = byId.get(id);
        if (!room) continue;
        const roomFootprint = footprint(room);
        const intended = id === rootId ? rootIntended : 0;
        const level = bandLevelFor(intended);
        const y = fallbackY.get(level) ?? 0;
        const candidate: PlacedRoom = {
          room,
          footprint: roomFootprint,
          level,
          intendedLevel: intended,
          x: bandNextX,
          y,
          detached: false,
          exact: false,
          placement: 'band',
          component: componentId,
        };
        placed.set(room.id, candidate);
        bandRooms.push(candidate);
        fallbackY.set(level, y + roomFootprint.height + 2);
      }
      bandNextX =
        Math.max(
          bandNextX,
          ...bandRooms.map((room) => room.x + room.footprint.width),
        ) + PORTAL_BAND_GAP;
      return;
    }

    for (const candidate of temp) {
      placed.set(candidate.room.id, candidate);
      bandRooms.push(candidate);
    }
    bandNextX =
      Math.max(
        bandNextX,
        ...temp.map((room) => room.x + room.footprint.width),
      ) + PORTAL_BAND_GAP;
  }

  for (const component of bandComponents) {
    placeBandComponent(
      component.componentId,
      component.component,
      component.tree,
      component.rootEdge,
    );
  }


  // Unreachable rooms keep the old detached-grid treatment, as a separate
  // band after the levels and portals.
  const detachedRooms: PlacedRoom[] = [];
  if (unplacedDetached.length > 0) {
    const maxWidth = Math.max(
      1,
      ...unplacedDetached.map((room) => footprint(room).width),
    );
    const maxHeight = Math.max(
      1,
      ...unplacedDetached.map((room) => footprint(room).height),
    );
    unplacedDetached.forEach((room, index) => {
      const column = index % DETACHED_COLUMNS;
      const row = Math.floor(index / DETACHED_COLUMNS);
      const candidate: PlacedRoom = {
        room,
        footprint: footprint(room),
        level: DETACHED_BAND_LEVEL,
        intendedLevel: 0,
        x: column * (maxWidth + DETACHED_COLUMN_GAP),
        y: row * (maxHeight + DETACHED_GAP),
        detached: true,
        exact: false,
        placement: 'detached',
        component: 0,
      };
      placed.set(room.id, candidate);
      detachedRooms.push(candidate);
    });
  }

  // Missing doors/stairs are genuine authoring errors, not portal relocations.
  for (const source of placed.values()) {
    for (const [direction, exit] of Object.entries(source.room.exits)) {
      if (!exit.to) continue;
      if (isCardinal(direction)) {
        const door = pickDoor(source.footprint, direction);
        if (door.missing) {
          addError({
            reason: 'missing-source-door',
            rooms: [source.room.id],
            direction,
            level: source.level,
            message: `Room '${source.room.id}' has no ${direction} door for its ${direction} exit.`,
          });
        }
      } else if (direction === 'up' || direction === 'down') {
        const stairs =
          direction === 'up'
            ? source.footprint.stairs.up
            : source.footprint.stairs.down;
        if (stairs.length === 0) {
          addError({
            reason: 'missing-source-stairs',
            rooms: [source.room.id],
            direction,
            level: source.level,
            message: `Room '${source.room.id}' has no ${direction} stair for its ${direction} exit.`,
          });
        }
      }
    }
  }

  // Record all declared links for badges.
  const links: PlacementLink[] = [];
  for (const source of placed.values()) {
    const exits = Object.entries(source.room.exits).sort(
      ([left], [right]) =>
        (EXIT_RANK[left] ?? 99) - (EXIT_RANK[right] ?? 99),
    );
    for (const [direction, exit] of exits) {
      const target = placed.get(exit.to);
      if (!target) continue;
      const kind: LinkKind = isCardinal(direction)
        ? 'cardinal'
        : direction === 'up' || direction === 'down'
          ? 'vertical'
          : 'portal';
      links.push({
        sourceId: source.room.id,
        targetId: target.room.id,
        direction,
        kind,
        oneWay: exit.one_way,
        levelDelta: target.intendedLevel - source.intendedLevel,
        fromDetached: source.detached,
      });
    }
  }

  // Every edge that stopped being a shared-wall tile connection is a portal.
  const portals: PlacementPortal[] = [];
  for (const source of placed.values()) {
    const exits = Object.entries(source.room.exits).sort(
      ([left], [right]) =>
        (EXIT_RANK[left] ?? 99) - (EXIT_RANK[right] ?? 99),
    );
    for (const [direction, exit] of exits) {
      if (direction === 'in' || direction === 'out') continue;
      const target = placed.get(exit.to);
      if (!target) continue;
      const expected = expectedFor(source, target.room, direction);
      if (!expected) continue;
      const samePlacement =
        expected.level === target.level &&
        expected.x === target.x &&
        expected.y === target.y;
      const sameComponent =
        !source.exact &&
        !target.exact &&
        !source.detached &&
        !target.detached &&
        source.component === target.component;
      let reason: PortalReason | null = null;
      if (source.exact && !target.exact) reason = 'relocated';
      else if (source.exact && target.exact && !samePlacement) reason = 'cycle';
      else if (!source.exact && target.exact) reason = 'alternate';
      else if (sameComponent) reason = null;
      else if (!source.exact && !target.exact) reason = 'alternate';
      if (!reason) continue;
      portals.push({
        sourceId: source.room.id,
        targetId: target.room.id,
        direction,
        reason,
        oneWay: exit.one_way,
        source,
        target,
      });
    }
  }

  // The invariant the owner asked for: no interior overlap between rooms that
  // are not a shared-wall exact pair or a local relocated subtree.
  const placedRooms = [...placed.values()];
  for (let left = 0; left < placedRooms.length; left += 1) {
    for (let right = left + 1; right < placedRooms.length; right += 1) {
      const a = placedRooms[left];
      const b = placedRooms[right];
      if (!a || !b || a.level !== b.level) continue;
      const allowSharedBoundary =
        (a.exact && b.exact) ||
        (a.component !== 0 &&
          a.component === b.component &&
          !a.exact &&
          !b.exact);
      if (
        rectanglesOverlap(
          a.x,
          a.y,
          a.footprint.width,
          a.footprint.height,
          b.x,
          b.y,
          b.footprint.width,
          b.footprint.height,
          allowSharedBoundary,
        )
      ) {
        addError({
          reason: 'unplaceable',
          rooms: [a.room.id, b.room.id],
          level: a.level,
          message: `Rooms '${a.room.id}' and '${b.room.id}' still overlap on level ${a.level}; use the tree to inspect them.`,
        });
      }
    }
  }

  const portalRooms = placedRooms.filter(
    (room) => !room.exact && !room.detached,
  );
  const portalPlacements = new Map(
    portalRooms.map((room) => [room.room.id, room] as const),
  );
  const exactRooms = placedRooms.filter((room) => room.exact);
  const detached = placedRooms.filter((room) => room.detached);
  const inOutPortals = links.filter((link) => link.kind === 'portal');

  const levels: LevelLayout[] = [];
  const mainByLevel = new Map<number, PlacedRoom[]>();
  for (const room of placedRooms) {
    if (room.detached || room.placement === 'band') continue;
    const group = mainByLevel.get(room.level) ?? [];
    group.push(room);
    mainByLevel.set(room.level, group);
  }
  for (const [level, levelRooms] of mainByLevel) {
    const cells = buildCells(levelRooms);
    const bounds = tileBounds(cells);
    levels.push({
      level,
      label: levelLabel(level),
      kind: 'level',
      rooms: sortRooms(levelRooms),
      cells,
      bounds: bounds ?? {
        minX: 0,
        minY: 0,
        maxX: 0,
        maxY: 0,
        width: 1,
        height: 1,
      },
    });
  }
  levels.sort((left, right) => right.level - left.level);

  const portalBands: LevelLayout[] = [];
  const bandByIntended = new Map<number, PlacedRoom[]>();
  for (const room of bandRooms) {
    const group = bandByIntended.get(room.intendedLevel) ?? [];
    group.push(room);
    bandByIntended.set(room.intendedLevel, group);
  }
  for (const [intended, rooms] of [...bandByIntended].sort(
    (left, right) => right[0] - left[0],
  )) {
    const cells = buildCells(rooms);
    const bounds = tileBounds(cells);
    const label =
      intended === 0
        ? 'Portals · Ground'
        : `Portals · Level ${intended > 0 ? '+' : ''}${intended}`;
    const band: LevelLayout = {
      level: bandLevelFor(intended),
      label,
      kind: 'portals',
      rooms: sortRooms(rooms),
      cells,
      bounds: bounds ?? {
        minX: 0,
        minY: 0,
        maxX: 0,
        maxY: 0,
        width: 1,
        height: 1,
      },
    };
    portalBands.push(band);
    levels.push(band);
  }

  if (detachedRooms.length > 0) {
    const cells = buildCells(detachedRooms);
    const bounds = tileBounds(cells);
    levels.push({
      level: DETACHED_BAND_LEVEL,
      label: 'Detached',
      kind: 'detached',
      rooms: sortRooms(detachedRooms),
      cells,
      bounds: bounds ?? {
        minX: 0,
        minY: 0,
        maxX: 0,
        maxY: 0,
        width: 1,
        height: 1,
      },
    });
  }

  return {
    levels,
    placed: placedRooms.sort(
      (left, right) =>
        (left.intendedLevel - right.intendedLevel) ||
        (left.y - right.y) ||
        (left.x - right.x) ||
        left.room.id.localeCompare(right.room.id),
    ),
    exactRooms,
    portalRooms,
    portalPlacements,
    detached,
    portals,
    inOutPortals,
    errors,
    links,
    portalBands,
    startId: start ? start.id : null,
  };
}


export function levelLabel(level: number): string {
  if (level === 0) return 'Level 0 · Ground';
  return `Level ${level > 0 ? '+' : ''}${level}`;
}

export function tileMetrics(
  placement: WorldPlacement,
  cellSize: number,
  pad: number,
  heading: number,
  gap: number,
): TileMetrics {
  if (placement.levels.length === 0) {
    return {
      levels: [],
      minX: 0,
      maxX: 0,
      width: Math.max(1, pad * 2),
      height: Math.max(1, pad * 2),
    };
  }
  const minX = Math.min(...placement.levels.map((level) => level.bounds.minX));
  const maxX = Math.max(...placement.levels.map((level) => level.bounds.maxX));
  let top = pad;
  const levels: TileMetricsLevel[] = [];
  for (const level of placement.levels) {
    const height = level.bounds.height * cellSize;
    const headingY = top + 13;
    const levelTop = top + heading;
    levels.push({ level, top: levelTop, headingY, height });
    top = levelTop + height + gap;
  }
  return {
    levels,
    minX,
    maxX,
    width: pad * 2 + (maxX - minX + 1) * cellSize,
    height: top + pad - gap,
  };
}

export function fitCamera(
  width: number,
  height: number,
  canvasWidth: number,
  canvasHeight: number,
  pad = 28,
  maxZoom = 1.4,
): TileCamera {
  if (width <= 0 || height <= 0 || canvasWidth < 60 || canvasHeight < 60) {
    return { zoom: 1, panX: pad, panY: pad };
  }
  const zoom = Math.max(
    0.2,
    Math.min(
      maxZoom,
      (canvasWidth - pad * 2) / width,
      (canvasHeight - pad * 2) / height,
    ),
  );
  return {
    zoom,
    panX: (canvasWidth - width * zoom) / 2,
    panY: (canvasHeight - height * zoom) / 2,
  };
}

import type { Room } from './types';
import { defaultGeometry, OPPOSITE_EXITS } from './types';

export type Cardinal = 'north' | 'east' | 'south' | 'west';
export type LinkKind = 'cardinal' | 'vertical' | 'portal';
export type ConflictReason =
  | 'position'
  | 'missing-source-door'
  | 'missing-source-stairs'
  | 'missing-room'
  | 'self-link'
  | 'overlap'
  | 'start-room';

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
  x: number;
  y: number;
  detached: boolean;
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
  rooms: PlacedRoom[];
  cells: TileCell[];
  bounds: TileBounds;
  detachedIds: string[];
  detachedBounds: TileBounds | null;
}

export interface WorldPlacement {
  levels: LevelLayout[];
  placed: PlacedRoom[];
  detached: PlacedRoom[];
  conflicts: PlacementConflict[];
  portals: PlacementLink[];
  links: PlacementLink[];
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

/**
 * Place every room in one continuous cell grid per level.
 *
 * Cardinal exits align the two door cells and make the boundary wall a shared
 * column or row. Up/down exits stack levels and align stairs when both rooms
 * have them. In/out and unknown directions are portal links only.
 */
export function placeWorld(rooms: Room[], startRoom: string): WorldPlacement {
  const byId = new Map(rooms.map((room) => [room.id, room]));
  const footprints = new Map(
    rooms.map((room) => [room.id, footprintFor(room)]),
  );
  const placed = new Map<string, PlacedRoom>();
  const conflicts: PlacementConflict[] = [];
  const conflictKeys = new Set<string>();
  const links: PlacementLink[] = [];
  const portals: PlacementLink[] = [];
  const order = new Map(
    rooms.map((room, index) => [room.id, index] as const),
  );

  function addConflict(conflict: PlacementConflict): void {
    const key = placementConflictKey(conflict);
    if (conflictKeys.has(key)) return;
    conflictKeys.add(key);
    conflicts.push(conflict);
  }

  function addPortal(link: PlacementLink): void {
    links.push(link);
    portals.push(link);
  }

  const start = byId.get(startRoom);
  if (!start) {
    addConflict({
      reason: 'start-room',
      rooms: [],
      message: `Start room '${startRoom}' is not declared; every room is detached.`,
    });
  } else {
    placed.set(start.id, {
      room: start,
      footprint: footprints.get(start.id) ?? footprintFor(start),
      level: 0,
      x: 0,
      y: 0,
      detached: false,
    });
  }

  const queue = start ? [start.id] : [];
  let queueIndex = 0;
  while (queueIndex < queue.length) {
    const currentId = queue[queueIndex];
    queueIndex += 1;
    const current = currentId ? placed.get(currentId) : undefined;
    if (!current) continue;
    const exits = Object.entries(current.room.exits).sort(
      ([left], [right]) =>
        (EXIT_RANK[left] ?? 99) - (EXIT_RANK[right] ?? 99),
    );
    for (const [direction, exit] of exits) {
      if (!exit.to) continue;
      const target = byId.get(exit.to);
      if (!target) {
        addConflict({
          reason: 'missing-room',
          rooms: [current.room.id],
          direction,
          level: current.level,
          message: `Room '${current.room.id}' exit ${direction} points to unknown room '${exit.to}'.`,
        });
        continue;
      }
      if (target.id === current.room.id) {
        addConflict({
          reason: 'self-link',
          rooms: [current.room.id],
          direction,
          level: current.level,
          message: `Room '${current.room.id}' exit ${direction} links to itself.`,
        });
        continue;
      }

      const targetFootprint =
        footprints.get(target.id) ?? footprintFor(target);
      if (isCardinal(direction)) {
        const opposite = OPPOSITE_EXITS[direction] as Cardinal | undefined;
        const placedTarget = placed.get(target.id);
        const targetForPlacement =
          !placedTarget && opposite
            ? ensureTargetDoor(targetFootprint, opposite)
            : targetFootprint;
        const expected = cardinalOrigin(
          current,
          targetForPlacement,
          direction,
        );
        const link: PlacementLink = {
          sourceId: current.room.id,
          targetId: target.id,
          direction,
          kind: 'cardinal',
          oneWay: exit.one_way,
          levelDelta: 0,
          fromDetached: false,
        };
        links.push(link);
        if (expected.sourceMissing) {
          addConflict({
            reason: 'missing-source-door',
            rooms: [current.room.id],
            direction,
            level: current.level,
            expected: { x: expected.x, y: expected.y },
            message: `Room '${current.room.id}' has no ${direction} door for its ${direction} exit; aligned by the side centre.`,
          });
        }
        const existing = placed.get(target.id);
        if (existing) {
          if (existing.level !== current.level ||
              existing.x !== expected.x ||
              existing.y !== expected.y) {
            addConflict({
              reason: 'position',
              rooms: [current.room.id, target.id],
              direction,
              level: current.level,
              expected: { x: expected.x, y: expected.y },
              actual: { x: existing.x, y: existing.y },
              message: `Room '${target.id}' is already at (${existing.x}, ${existing.y}) on level ${existing.level}; ${direction} from '${current.room.id}' expects (${expected.x}, ${expected.y}) on level ${current.level}.`,
            });
          }
          continue;
        }
        placed.set(target.id, {
          room: target,
          footprint: targetForPlacement,
          level: current.level,
          x: expected.x,
          y: expected.y,
          detached: false,
        });
        queue.push(target.id);
        continue;
      }

      if (direction === 'up' || direction === 'down') {
        const expected = verticalOrigin(current, targetFootprint, direction);
        const link: PlacementLink = {
          sourceId: current.room.id,
          targetId: target.id,
          direction,
          kind: 'vertical',
          oneWay: exit.one_way,
          levelDelta: direction === 'up' ? 1 : -1,
          fromDetached: false,
        };
        links.push(link);
        if (expected.sourceMissing) {
          addConflict({
            reason: 'missing-source-stairs',
            rooms: [current.room.id],
            direction,
            level: current.level,
            message: `Room '${current.room.id}' has no ${direction === 'up' ? 'up' : 'down'} stair for its ${direction} exit; kept the same cell origin.`,
          });
        }
        const existing = placed.get(target.id);
        if (existing) {
          if (
            existing.level !== expected.level ||
            existing.x !== expected.x ||
            existing.y !== expected.y
          ) {
            addConflict({
              reason: 'position',
              rooms: [current.room.id, target.id],
              direction,
              level: current.level,
              expected: { x: expected.x, y: expected.y },
              actual: { x: existing.x, y: existing.y },
              message: `Room '${target.id}' is already at (${existing.x}, ${existing.y}) on level ${existing.level}; ${direction} from '${current.room.id}' expects (${expected.x}, ${expected.y}) on level ${expected.level}.`,
            });
          }
          continue;
        }
        placed.set(target.id, {
          room: target,
          footprint: targetFootprint,
          level: expected.level,
          x: expected.x,
          y: expected.y,
          detached: false,
        });
        queue.push(target.id);
        continue;
      }

      addPortal({
        sourceId: current.room.id,
        targetId: target.id,
        direction,
        kind: 'portal',
        oneWay: exit.one_way,
        levelDelta: 0,
        fromDetached: false,
      });
    }
  }

  const unplaced = rooms
    .filter((room) => !placed.has(room.id))
    .sort((left, right) => roomOrderKey(left).localeCompare(roomOrderKey(right)));
  const detached: PlacedRoom[] = [];
  if (unplaced.length > 0) {
    const levelZero = [...placed.values()].filter(
      (placedRoom) => placedRoom.level === 0 && !placedRoom.detached,
    );
    const mainBounds = tileBounds(buildCells(levelZero));
    const footprintsToPlace = unplaced.map((room) => ({
      room,
      footprint: footprints.get(room.id) ?? footprintFor(room),
    }));
    const maxWidth = Math.max(
      1,
      ...footprintsToPlace.map((item) => item.footprint.width),
    );
    const maxHeight = Math.max(
      1,
      ...footprintsToPlace.map((item) => item.footprint.height),
    );
    const baseX = mainBounds ? mainBounds.minX : 0;
    const baseY = mainBounds ? mainBounds.maxY + DETACHED_GAP : 0;
    footprintsToPlace.forEach(({ room, footprint }, index) => {
      const column = index % DETACHED_COLUMNS;
      const row = Math.floor(index / DETACHED_COLUMNS);
      const placedRoom: PlacedRoom = {
        room,
        footprint,
        level: 0,
        x: baseX + column * (maxWidth + DETACHED_COLUMN_GAP),
        y: baseY + row * (maxHeight + DETACHED_GAP),
        detached: true,
      };
      placed.set(room.id, placedRoom);
      detached.push(placedRoom);
      for (const [direction, exit] of Object.entries(room.exits)) {
        if (!exit.to || !byId.has(exit.to)) continue;
        const kind: LinkKind = isCardinal(direction)
          ? 'cardinal'
          : direction === 'up' || direction === 'down'
            ? 'vertical'
            : 'portal';
        const link: PlacementLink = {
          sourceId: room.id,
          targetId: exit.to,
          direction,
          kind,
          oneWay: exit.one_way,
          levelDelta:
            direction === 'up' ? 1 : direction === 'down' ? -1 : 0,
          fromDetached: true,
        };
        if (kind === 'portal') addPortal(link);
        else links.push(link);
      }
    });
  }

  for (const [level, levelRooms] of [...placed.values()].reduce(
    (grouped, placedRoom) => {
      const list = grouped.get(placedRoom.level) ?? [];
      list.push(placedRoom);
      grouped.set(placedRoom.level, list);
      return grouped;
    },
    new Map<number, PlacedRoom[]>(),
  )) {
    for (let left = 0; left < levelRooms.length; left += 1) {
      for (let right = left + 1; right < levelRooms.length; right += 1) {
        const a = levelRooms[left];
        const b = levelRooms[right];
        if (!a || !b) continue;
        const overlapX =
          Math.min(a.x + a.footprint.width, b.x + b.footprint.width) -
          Math.max(a.x, b.x);
        const overlapY =
          Math.min(a.y + a.footprint.height, b.y + b.footprint.height) -
          Math.max(a.y, b.y);
        if (overlapX > 1 && overlapY > 1) {
          addConflict({
            reason: 'overlap',
            rooms: [a.room.id, b.room.id],
            level,
            actual: { x: b.x, y: b.y },
            message: `Rooms '${a.room.id}' and '${b.room.id}' overlap on level ${level}; their geometry does not close.`,
          });
        }
      }
    }
  }

  const levels: LevelLayout[] = [];
  for (const [level, levelRooms] of [...placed.values()].reduce(
    (grouped, placedRoom) => {
      const list = grouped.get(placedRoom.level) ?? [];
      list.push(placedRoom);
      grouped.set(placedRoom.level, list);
      return grouped;
    },
    new Map<number, PlacedRoom[]>(),
  )) {
    const cells = buildCells(levelRooms);
    const bounds = tileBounds(cells);
    const detachedRooms = levelRooms.filter((room) => room.detached);
    const detachedBounds = tileBounds(buildCells(detachedRooms));
    levels.push({
      level,
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
      detachedIds: detachedRooms.map((room) => room.room.id),
      detachedBounds,
    });
  }
  levels.sort((left, right) => right.level - left.level);

  return {
    levels,
    placed: [...placed.values()].sort(
      (left, right) =>
        left.level - right.level ||
        left.y - right.y ||
        left.x - right.x ||
        left.room.id.localeCompare(right.room.id),
    ),
    detached,
    conflicts,
    portals,
    links,
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

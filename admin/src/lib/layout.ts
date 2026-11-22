import type { Layout, LayoutPoint, Room } from './types';

export const NODE_WIDTH = 208;
export const NODE_HEIGHT = 96;
export const COLUMN_STEP = 286;
export const ROW_STEP = 132;
export const LAYOUT_MARGIN = 60;
export const GRID_COLUMNS = 4;

export interface LayoutBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

function orderKey(room: Room): string {
  return `${room.area ?? '~'}\u0000${room.id}`;
}

function adjacency(rooms: Room[]): Map<string, Set<string>> {
  const known = new Set(rooms.map((room) => room.id));
  const graph = new Map<string, Set<string>>(
    rooms.map((room) => [room.id, new Set<string>()]),
  );
  for (const room of rooms) {
    const links = graph.get(room.id);
    if (!links) continue;
    for (const exit of Object.values(room.exits)) {
      if (!exit.to || exit.to === room.id || !known.has(exit.to)) continue;
      links.add(exit.to);
      if (!exit.one_way) graph.get(exit.to)?.add(room.id);
    }
  }
  return graph;
}

function layered(
  start: string,
  graph: Map<string, Set<string>>,
  order: Map<string, string>,
): string[][] {
  const layers: string[][] = [];
  const seen = new Set<string>();
  let frontier = graph.has(start) ? [start] : [];
  seen.add(start);
  while (frontier.length > 0) {
    const layer = [...frontier].sort((left, right) =>
      (order.get(left) ?? left).localeCompare(order.get(right) ?? right),
    );
    layers.push(layer);
    const next: string[] = [];
    for (const roomId of layer) {
      for (const target of graph.get(roomId) ?? []) {
        if (seen.has(target)) continue;
        seen.add(target);
        next.push(target);
      }
    }
    frontier = next;
  }
  return layers;
}

/**
 * Deterministic editor layout: reachable rooms are layered by exit distance
 * from the start room; rooms the start cannot reach fall into a grid below.
 */
export function autoLayout(rooms: Room[], startRoom: string): Layout {
  const graph = adjacency(rooms);
  const order = new Map(rooms.map((room) => [room.id, orderKey(room)]));
  const layout: Layout = {};
  const layers = layered(startRoom, graph, order);
  let lastY = LAYOUT_MARGIN;

  layers.forEach((layer, column) => {
    layer.forEach((roomId, row) => {
      layout[roomId] = {
        x: LAYOUT_MARGIN + column * COLUMN_STEP,
        y: LAYOUT_MARGIN + row * ROW_STEP,
      };
      lastY = Math.max(lastY, LAYOUT_MARGIN + row * ROW_STEP);
    });
  });

  const disconnected = rooms
    .filter((room) => !(room.id in layout))
    .sort((left, right) => orderKey(left).localeCompare(orderKey(right)));
  const gridStart = layers.length > 0 ? lastY + ROW_STEP : LAYOUT_MARGIN;
  disconnected.forEach((room, index) => {
    const column = index % GRID_COLUMNS;
    const row = Math.floor(index / GRID_COLUMNS);
    layout[room.id] = {
      x: LAYOUT_MARGIN + column * COLUMN_STEP,
      y: gridStart + row * ROW_STEP,
    };
  });

  return layout;
}

export function layoutBounds(
  layout: Layout,
  rooms: Room[],
): LayoutBounds | null {
  const ids = new Set(rooms.map((room) => room.id));
  const points = Object.entries(layout)
    .filter(([roomId]) => ids.has(roomId))
    .map(([, point]) => point);
  if (points.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    maxX = Math.max(maxX, point.x + NODE_WIDTH);
    maxY = Math.max(maxY, point.y + NODE_HEIGHT);
  }
  return {
    minX,
    minY,
    maxX,
    maxY,
    width: maxX - minX,
    height: maxY - minY,
  };
}

/** A free-enough spot for a room added without a canvas position. */
export function positionForNewRoom(layout: Layout, rooms: Room[]): LayoutPoint {
  const ids = new Set(rooms.map((room) => room.id));
  let maxY = -Infinity;
  for (const [roomId, point] of Object.entries(layout)) {
    if (!ids.has(roomId)) continue;
    maxY = Math.max(maxY, point.y);
  }
  if (!Number.isFinite(maxY)) return { x: LAYOUT_MARGIN, y: LAYOUT_MARGIN };
  return { x: LAYOUT_MARGIN, y: maxY + ROW_STEP };
}

<script lang="ts">
  import type {
    AddRoomRequest,
    ConnectRequest,
    Layout,
    LayoutPoint,
    PaletteKind,
    Room,
    RoomTab,
  } from '../lib/types';
  import { OPPOSITE_EXITS } from '../lib/types';
  import {
    autoLayout,
    layoutBounds,
    NODE_HEIGHT,
    NODE_WIDTH,
  } from '../lib/layout';

  interface Props {
    rooms: Room[];
    layout: Layout;
    startRoom: string;
    selectedId: string | null;
    roomErrors: Record<string, number>;
    areaChoices: { index: number; label: string }[];
    defaultAreaIndex: number;
    suggestedRoomId: string;
    onselect: (roomId: string, tab?: RoomTab) => void;
    onlayout: (layout: Layout) => void;
    oncreateroom: (request: AddRoomRequest) => string | null;
    onpalette: (kind: PaletteKind, roomId: string) => void;
    onconnect: (request: ConnectRequest) => string | null;
  }

  let {
    rooms,
    layout,
    startRoom,
    selectedId,
    roomErrors,
    areaChoices,
    defaultAreaIndex,
    suggestedRoomId,
    onselect,
    onlayout,
    oncreateroom,
    onpalette,
    onconnect,
  }: Props = $props();

  interface EdgeDirection {
    roomId: string;
    direction: string;
    oneWay: boolean;
    locked: boolean;
  }

  interface MapEdge {
    key: string;
    a: string;
    b: string;
    directions: EdgeDirection[];
    oneWay: boolean;
    locked: boolean;
  }

  interface RenderedEdge extends MapEdge {
    pA: LayoutPoint;
    pB: LayoutPoint;
  }

  interface NodeDrag {
    roomId: string;
    pointerId: number;
    startClientX: number;
    startClientY: number;
    originalX: number;
    originalY: number;
    moved: boolean;
  }

  interface PanDrag {
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startPanX: number;
    startPanY: number;
    moved: boolean;
  }

  interface ConnectionDrag {
    sourceId: string;
    direction: string;
    originX: number;
    originY: number;
    x: number;
    y: number;
    startX: number;
    startY: number;
    moved: boolean;
  }

  interface PendingRoom {
    x: number;
    y: number;
    screenX: number;
    screenY: number;
  }

  interface Camera {
    zoom: number;
    panX: number;
    panY: number;
  }

  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 2.5;
  const PALETTE_MIME = 'application/x-mud-palette';
  const HANDLES = [
    { direction: 'north', x: NODE_WIDTH / 2, y: 0, label: 'N' },
    { direction: 'east', x: NODE_WIDTH, y: NODE_HEIGHT / 2, label: 'E' },
    { direction: 'south', x: NODE_WIDTH / 2, y: NODE_HEIGHT, label: 'S' },
    { direction: 'west', x: 0, y: NODE_HEIGHT / 2, label: 'W' },
  ];
  const PALETTE: { kind: PaletteKind; label: string }[] = [
    { kind: 'item', label: 'Item' },
    { kind: 'container', label: 'Container' },
    { kind: 'readable', label: 'Readable' },
    { kind: 'npc', label: 'NPC' },
  ];
  const AREA_HUES = [172, 202, 266, 32, 328, 92, 12, 232];

  let canvasWidth = $state(900);
  let canvasHeight = $state(560);
  let cameraState = $state<Camera | null>(null);
  let svgEl: SVGSVGElement | null = null;

  let notice = $state('');
  let addMode = $state(false);
  let pending = $state<PendingRoom | null>(null);
  let formArea = $state(0);
  let formId = $state('');
  let formName = $state('New Room');
  let formError = $state('');

  let paletteArmed = $state<PaletteKind | null>(null);
  let paletteDragging = $state<PaletteKind | null>(null);
  let dropTarget = $state<string | null>(null);
  let oneWayConnect = $state(false);
  let armedConnection = $state<{ sourceId: string; direction: string } | null>(
    null,
  );
  let connection = $state<ConnectionDrag | null>(null);

  let panDrag: PanDrag | null = null;
  let nodeDrag: NodeDrag | null = null;
  let suppressClick = false;

  const positions = $derived.by(() => ({
    ...autoLayout(rooms, startRoom),
    ...layout,
  }));
  const defaultCamera = $derived.by(() =>
    initialCamera(positions, canvasWidth, canvasHeight),
  );
  const camera = $derived(cameraState ?? defaultCamera);
  const zoom = $derived(camera.zoom);
  const panX = $derived(camera.panX);
  const panY = $derived(camera.panY);
  const edges = $derived.by(() => buildEdges(rooms));
  const renderedEdges = $derived.by(() => {
    const rendered: RenderedEdge[] = [];
    for (const edge of edges) {
      const a = positions[edge.a];
      const b = positions[edge.b];
      if (!a || !b) continue;
      rendered.push({
        ...edge,
        pA: clipToNode(a, b),
        pB: clipToNode(b, a),
      });
    }
    return rendered;
  });
  const areaLegend = $derived.by(() => {
    const names: string[] = [];
    const legend: { name: string; color: string }[] = [];
    for (const room of rooms) {
      const name = room.area ?? 'world.yaml';
      if (names.includes(name)) continue;
      names.push(name);
      legend.push({ name, color: areaColor(room.area).stroke });
    }
    return legend;
  });

  function areaColor(area: string | null): { stroke: string; fill: string } {
    let hash = 0;
    for (const char of area ?? 'world') {
      hash = (hash * 31 + char.charCodeAt(0)) | 0;
    }
    const hue = AREA_HUES[Math.abs(hash) % AREA_HUES.length] ?? 172;
    return {
      stroke: `hsl(${hue} 58% 58%)`,
      fill: `hsl(${hue} 48% 52% / 0.13)`,
    };
  }

  function clipText(value: string, limit: number): string {
    return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
  }

  function roomName(roomId: string): string {
    return rooms.find((room) => room.id === roomId)?.name || roomId;
  }

  function paletteArticle(kind: PaletteKind): string {
    return kind === 'item' || kind === 'npc' ? 'an' : 'a';
  }

  function buildEdges(list: Room[]): MapEdge[] {
    const order: Record<string, number> = {};
    list.forEach((room, index) => {
      order[room.id] = index;
    });
    const groups: Record<string, MapEdge> = {};
    for (const room of list) {
      for (const [direction, exit] of Object.entries(room.exits)) {
        if (!exit.to || exit.to === room.id || !(exit.to in order)) continue;
        const left = (order[room.id] ?? 0) < (order[exit.to] ?? 0);
        const a = left ? room.id : exit.to;
        const b = left ? exit.to : room.id;
        const key = `${a}\u0000${b}`;
        let edge = groups[key];
        if (!edge) {
          edge = {
            key,
            a,
            b,
            directions: [],
            oneWay: false,
            locked: false,
          };
          groups[key] = edge;
        }
        if (
          !edge.directions.some(
            (item) =>
              item.roomId === room.id && item.direction === direction,
          )
        ) {
          edge.directions.push({
            roomId: room.id,
            direction,
            oneWay: exit.one_way,
            locked: exit.locked,
          });
        }
      }
    }
    return Object.values(groups)
      .map((edge) => ({
        ...edge,
        oneWay: edge.directions.every((item) => item.oneWay),
        locked: edge.directions.some((item) => item.locked),
      }))
      .sort((left, right) => left.key.localeCompare(right.key));
  }

  function clipToNode(start: LayoutPoint, other: LayoutPoint): LayoutPoint {
    const cx = start.x + NODE_WIDTH / 2;
    const cy = start.y + NODE_HEIGHT / 2;
    const dx = other.x + NODE_WIDTH / 2 - cx;
    const dy = other.y + NODE_HEIGHT / 2 - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const scaleX =
      dx === 0 ? Number.POSITIVE_INFINITY : NODE_WIDTH / 2 / Math.abs(dx);
    const scaleY =
      dy === 0 ? Number.POSITIVE_INFINITY : NODE_HEIGHT / 2 / Math.abs(dy);
    const scale = Math.min(scaleX, scaleY);
    return { x: cx + dx * scale, y: cy + dy * scale };
  }

  function arrowPoints(tip: LayoutPoint, from: LayoutPoint, size = 8): string {
    const angle = Math.atan2(tip.y - from.y, tip.x - from.x);
    const left = {
      x: tip.x - size * Math.cos(angle - 0.5),
      y: tip.y - size * Math.sin(angle - 0.5),
    };
    const right = {
      x: tip.x - size * Math.cos(angle + 0.5),
      y: tip.y - size * Math.sin(angle + 0.5),
    };
    return `${tip.x},${tip.y} ${left.x},${left.y} ${right.x},${right.y}`;
  }

  function labelPoint(
    from: LayoutPoint,
    to: LayoutPoint,
    ratio = 0.3,
  ): LayoutPoint {
    return {
      x: from.x + (to.x - from.x) * ratio,
      y: from.y + (to.y - from.y) * ratio,
    };
  }

  function edgeLabel(edge: RenderedEdge, item: EdgeDirection): LayoutPoint {
    const fromA = item.roomId === edge.a;
    return fromA
      ? labelPoint(edge.pA, edge.pB)
      : labelPoint(edge.pB, edge.pA);
  }

  function reciprocalLabel(
    edge: RenderedEdge,
    item: EdgeDirection,
  ): string | null {
    if (edge.directions.length !== 1 || edge.oneWay) return null;
    return OPPOSITE_EXITS[item.direction] ?? null;
  }

  function reciprocalPoint(
    edge: RenderedEdge,
    item: EdgeDirection,
  ): LayoutPoint {
    return item.roomId === edge.a
      ? labelPoint(edge.pB, edge.pA)
      : labelPoint(edge.pA, edge.pB);
  }

  function edgeBadge(edge: MapEdge): string {
    const parts: string[] = [];
    if (edge.locked) parts.push('locked');
    if (edge.oneWay) parts.push('one-way');
    return parts.join(' · ');
  }

  function edgeDescription(edge: MapEdge): string {
    return edge.directions
      .map((item) => {
        const flags = [
          item.locked ? 'locked' : '',
          item.oneWay ? 'one-way' : '',
        ].filter(Boolean);
        return `${roomName(item.roomId)} ${item.direction} → ${roomName(
          item.roomId === edge.a ? edge.b : edge.a,
        )}${flags.length > 0 ? ` (${flags.join(', ')})` : ''}`;
      })
      .join('; ');
  }

  function attachSvg(node: SVGSVGElement): () => void {
    svgEl = node;
    return () => {
      svgEl = null;
    };
  }

  function toWorld(event: MouseEvent | PointerEvent | WheelEvent): LayoutPoint {
    const rect = svgEl?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: (event.clientX - rect.left - panX) / zoom,
      y: (event.clientY - rect.top - panY) / zoom,
    };
  }

  function clampZoom(value: number): number {
    return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
  }

  function cameraFor(next: Layout, width: number, height: number): Camera {
    const fallback = { zoom: 1, panX: 36, panY: 36 };
    if (width < 80 || height < 80) return fallback;
    const bounds = layoutBounds(next, rooms);
    if (!bounds) return fallback;
    const pad = 52;
    const nextZoom = clampZoom(
      Math.min(
        1.4,
        (width - pad * 2) / Math.max(1, bounds.width),
        (height - pad * 2) / Math.max(1, bounds.height),
      ),
    );
    return {
      zoom: nextZoom,
      panX: (width - bounds.width * nextZoom) / 2 - bounds.minX * nextZoom,
      panY: (height - bounds.height * nextZoom) / 2 - bounds.minY * nextZoom,
    };
  }

  function initialCamera(
    next: Layout,
    width: number,
    height: number,
  ): Camera {
    const fitted = cameraFor(next, width, height);
    if (fitted.zoom >= 0.85) return fitted;
    const focus = next[startRoom] ?? { x: 0, y: 0 };
    const initialZoom = 0.85;
    return {
      zoom: initialZoom,
      panX: width / 2 - (focus.x + NODE_WIDTH / 2) * initialZoom,
      panY: height / 2 - (focus.y + NODE_HEIGHT / 2) * initialZoom,
    };
  }

  function zoomAtPoint(x: number, y: number, factor: number): void {
    const next = clampZoom(zoom * factor);
    if (next === zoom) return;
    const worldX = (x - panX) / zoom;
    const worldY = (y - panY) / zoom;
    cameraState = {
      zoom: next,
      panX: x - worldX * next,
      panY: y - worldY * next,
    };
  }

  function zoomAtCenter(factor: number): void {
    zoomAtPoint(canvasWidth / 2, canvasHeight / 2, factor);
  }

  function onwheel(event: WheelEvent): void {
    event.preventDefault();
    const rect = svgEl?.getBoundingClientRect();
    if (!rect) return;
    zoomAtPoint(
      event.clientX - rect.left,
      event.clientY - rect.top,
      event.deltaY < 0 ? 1.12 : 1 / 1.12,
    );
  }

  function fitLayout(next: Layout): void {
    cameraState = cameraFor(next, canvasWidth, canvasHeight);
  }

  function fit(): void {
    fitLayout(positions);
  }

  function autoLayoutNow(): void {
    const next = autoLayout(rooms, startRoom);
    onlayout(next);
    fitLayout(next);
    notice = 'Auto-layout recomputed. Save to keep these positions.';
  }

  function startPan(event: PointerEvent): void {
    if (event.button !== 0 || pending) return;
    panDrag = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startPanX: panX,
      startPanY: panY,
      moved: false,
    };
    svgEl?.setPointerCapture(event.pointerId);
  }

  function startNodeDrag(
    event: PointerEvent,
    roomId: string,
  ): void {
    const target = event.target as Element | null;
    if (connection || target?.closest('.node-handle')) return;
    event.stopPropagation();
    suppressClick = false;
    if (event.button !== 0 || paletteArmed || armedConnection) return;
    const point = positions[roomId];
    if (!point) return;
    onselect(roomId);
    nodeDrag = {
      roomId,
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      originalX: point.x,
      originalY: point.y,
      moved: false,
    };
    svgEl?.setPointerCapture(event.pointerId);
  }

  function handleNodePointerMove(event: PointerEvent): void {
    if (!nodeDrag) return;
    const dx = (event.clientX - nodeDrag.startClientX) / zoom;
    const dy = (event.clientY - nodeDrag.startClientY) / zoom;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) nodeDrag.moved = true;
    if (!nodeDrag.moved) return;
    onlayout({
      ...layout,
      [nodeDrag.roomId]: {
        x: Math.round(nodeDrag.originalX + dx),
        y: Math.round(nodeDrag.originalY + dy),
      },
    });
  }

  function activateRoom(roomId: string): void {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    if (paletteArmed) {
      const kind = paletteArmed;
      paletteArmed = null;
      onpalette(kind, roomId);
      notice = `Added ${paletteArticle(kind)} ${kind === 'npc' ? 'NPC' : kind} to ${roomName(
        roomId,
      )}.`;
      return;
    }
    if (armedConnection) {
      completeConnection(armedConnection, roomId);
      return;
    }
    onselect(roomId);
  }

  function onNodeClick(event: MouseEvent, roomId: string): void {
    const target = event.target as Element | null;
    if (target?.closest('.node-handle')) return;
    activateRoom(roomId);
  }

  function onNodeKeydown(event: KeyboardEvent, roomId: string): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const target = event.target as Element | null;
    if (target?.closest('.node-handle')) return;
    event.preventDefault();
    suppressClick = false;
    activateRoom(roomId);
  }

  function startConnection(
    event: PointerEvent,
    roomId: string,
    direction: string,
    handleX: number,
    handleY: number,
  ): void {
    event.stopPropagation();
    event.preventDefault();
    if (event.button !== 0) return;
    const point = positions[roomId];
    if (!point) return;
    nodeDrag = null;
    panDrag = null;
    armedConnection = null;
    connection = {
      sourceId: roomId,
      direction,
      originX: point.x + handleX,
      originY: point.y + handleY,
      x: point.x + handleX,
      y: point.y + handleY,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    svgEl?.setPointerCapture(event.pointerId);
    notice = `Drag to a room, or click one, to set the ${direction} exit.`;
  }

  function armConnection(event: KeyboardEvent, roomId: string, direction: string): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    event.stopPropagation();
    armedConnection = { sourceId: roomId, direction };
    connection = null;
    paletteArmed = null;
    notice = `Click a target room to connect ${roomName(roomId)} ${direction}.`;
  }

  function stopHandleClick(event: MouseEvent): void {
    event.stopPropagation();
  }

  function completeConnection(
    link: { sourceId: string; direction: string },
    targetId: string,
  ): void {
    const sourceRoom = rooms.find((room) => room.id === link.sourceId);
    const targetRoom = rooms.find((room) => room.id === targetId);
    if (!sourceRoom || !targetRoom) return;
    if (targetId === link.sourceId) {
      notice = 'A room cannot link to itself.';
      armedConnection = null;
      connection = null;
      return;
    }
    const error = onconnect({
      sourceId: link.sourceId,
      targetId,
      direction: link.direction,
      oneWay: oneWayConnect,
    });
    armedConnection = null;
    connection = null;
    if (error) {
      notice = error;
      return;
    }
    notice = `Connected ${sourceRoom.name} ${link.direction} to ${targetRoom.name}${
      oneWayConnect ? ' (one-way)' : ' and its reciprocal'
    }.`;
    onselect(link.sourceId, 'exits');
  }

  function handlePointerMove(event: PointerEvent): void {
    if (panDrag) {
      const dx = event.clientX - panDrag.startClientX;
      const dy = event.clientY - panDrag.startClientY;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) panDrag.moved = true;
      cameraState = {
        zoom: camera.zoom,
        panX: panDrag.startPanX + dx,
        panY: panDrag.startPanY + dy,
      };
      return;
    }
    if (connection) {
      if (
        Math.abs(event.clientX - connection.startX) > 3 ||
        Math.abs(event.clientY - connection.startY) > 3
      ) {
        connection.moved = true;
      }
      const point = toWorld(event);
      connection = { ...connection, x: point.x, y: point.y };
      return;
    }
    if (nodeDrag) {
      handleNodePointerMove(event);
    }
  }

  function roomAt(event: PointerEvent): string | null {
    if (typeof document !== 'undefined') {
      const hit = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest('[data-room-id]');
      const id = hit?.getAttribute('data-room-id');
      if (id) return id;
    }
    const point = toWorld(event);
    for (const [roomId, position] of Object.entries(positions)) {
      if (
        point.x >= position.x &&
        point.x <= position.x + NODE_WIDTH &&
        point.y >= position.y &&
        point.y <= position.y + NODE_HEIGHT
      ) {
        return roomId;
      }
    }
    return null;
  }

  function handlePointerUp(event: PointerEvent): void {
    if (panDrag) {
      if (!panDrag.moved) {
        if (addMode) {
          openAddForm(event);
        } else if (armedConnection) {
          armedConnection = null;
          connection = null;
          notice = 'Connection cancelled.';
        } else if (paletteArmed) {
          paletteArmed = null;
          notice = 'Palette selection cleared.';
        }
      }
      panDrag = null;
      svgEl?.releasePointerCapture(event.pointerId);
      return;
    }
    if (connection) {
      const current = connection;
      const target = roomAt(event);
      if (target && target !== current.sourceId) {
        completeConnection(current, target);
      } else if (!current.moved) {
        armedConnection = {
          sourceId: current.sourceId,
          direction: current.direction,
        };
        connection = null;
        notice = `Click a target room to connect ${roomName(
          current.sourceId,
        )} ${current.direction}.`;
      } else if (target === current.sourceId) {
        notice = 'A room cannot link to itself.';
        connection = null;
      } else {
        connection = null;
        notice = 'Connection cancelled: drop on another room.';
      }
      nodeDrag = null;
      svgEl?.releasePointerCapture(event.pointerId);
      return;
    }
    if (nodeDrag) {
      if (nodeDrag.moved) suppressClick = true;
      nodeDrag = null;
      svgEl?.releasePointerCapture(event.pointerId);
      return;
    }
  }

  function handlePointerCancel(event: PointerEvent): void {
    panDrag = null;
    nodeDrag = null;
    connection = null;
    svgEl?.releasePointerCapture(event.pointerId);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return;
    if (pending) {
      pending = null;
      formError = '';
      return;
    }
    if (connection || armedConnection || addMode || paletteArmed) {
      connection = null;
      armedConnection = null;
      addMode = false;
      paletteArmed = null;
      notice = '';
    }
  }

  function openAddForm(event: PointerEvent): void {
    const rect = svgEl?.getBoundingClientRect();
    const point = toWorld(event);
    pending = {
      x: point.x,
      y: point.y,
      screenX: event.clientX - (rect?.left ?? 0),
      screenY: event.clientY - (rect?.top ?? 0),
    };
    formArea = defaultAreaIndex;
    formId = suggestedRoomId;
    formName = 'New Room';
    formError = '';
    notice = 'Name the room, then add it at the clicked point.';
  }

  function toggleAddMode(): void {
    addMode = !addMode;
    pending = null;
    formError = '';
    if (addMode) {
      formId = suggestedRoomId;
      formArea = defaultAreaIndex;
      formName = 'New Room';
    }
    notice = addMode
      ? 'Click empty canvas to place the new room.'
      : 'Add-room placement cancelled.';
  }

  function submitAddRoom(event: SubmitEvent): void {
    event.preventDefault();
    if (!pending) return;
    const id = formId.trim();
    if (!id) {
      formError = 'A room id is required.';
      return;
    }
    const error = oncreateroom({
      areaIndex: formArea,
      id,
      name: formName.trim() || 'New Room',
      x: Math.round(pending.x),
      y: Math.round(pending.y),
    });
    if (error) {
      formError = error;
      return;
    }
    pending = null;
    addMode = false;
    formError = '';
    notice = `Added room ${id}.`;
  }

  function cancelAddRoom(): void {
    pending = null;
    formError = '';
  }

  function armPalette(kind: PaletteKind): void {
    paletteArmed = paletteArmed === kind ? null : kind;
    paletteDragging = null;
    if (paletteArmed) {
      notice = `Click a room to add a ${kind === 'npc' ? 'NPC' : kind}.`;
    }
  }

  function startPaletteDrag(event: DragEvent, kind: PaletteKind): void {
    paletteDragging = kind;
    if (event.dataTransfer) {
      event.dataTransfer.setData(PALETTE_MIME, kind);
      event.dataTransfer.setData('text/plain', kind);
      event.dataTransfer.effectAllowed = 'copy';
    }
  }

  function handleRoomDragOver(event: DragEvent, roomId: string): void {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    dropTarget = roomId;
  }

  function dragLeaveRoom(roomId: string): void {
    if (dropTarget === roomId) dropTarget = null;
  }

  function dropOnRoom(event: DragEvent, roomId: string): void {
    event.preventDefault();
    const raw =
      event.dataTransfer?.getData(PALETTE_MIME) ||
      event.dataTransfer?.getData('text/plain') ||
      paletteDragging;
    dropTarget = null;
    paletteDragging = null;
    if (!PALETTE.some((item) => item.kind === raw)) return;
    const kind = raw as PaletteKind;
    onpalette(kind, roomId);
    notice = `Added ${paletteArticle(kind)} ${kind === 'npc' ? 'NPC' : kind} to ${roomName(
      roomId,
    )}.`;
  }

  function selectEdge(event: MouseEvent, edge: RenderedEdge): void {
    const point = toWorld(event);
    const distanceA = Math.hypot(point.x - edge.pA.x, point.y - edge.pA.y);
    const distanceB = Math.hypot(point.x - edge.pB.x, point.y - edge.pB.y);
    const preferredRoom = distanceA <= distanceB ? edge.a : edge.b;
    const preferred =
      edge.directions.find((item) => item.roomId === preferredRoom) ??
      edge.directions[0];
    if (preferred) onselect(preferred.roomId, 'exits');
  }

  function edgeKeydown(event: KeyboardEvent, edge: RenderedEdge): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    const preferred = edge.directions[0];
    if (preferred) onselect(preferred.roomId, 'exits');
  }
</script>

<svelte:window onkeydown={onKeydown} />

<section class="map-panel" aria-label="Visual world map">
  <header class="map-head">
    <div>
      <h3>World map</h3>
      <p class="muted">
        Drag rooms to arrange them, drag an edge handle to connect exits, and
        drag a palette chip onto a room.
      </p>
    </div>
    <div class="map-controls">
      <button
        type="button"
        class={addMode ? 'small primary' : 'small'}
        onclick={toggleAddMode}
        aria-pressed={addMode}
      >
        {addMode ? 'Click canvas…' : 'Add room'}
      </button>
      <label class="check-inline compact-check">
        <input type="checkbox" bind:checked={oneWayConnect} />
        One-way connect
      </label>
      <button type="button" class="small" onclick={autoLayoutNow}>
        Auto-layout
      </button>
      <button type="button" class="small" onclick={fit}>Fit</button>
      <div class="zoom-controls" aria-label="Map zoom">
        <button
          type="button"
          class="small"
          onclick={() => zoomAtCenter(1 / 1.18)}
          aria-label="Zoom out">−</button
        >
        <span class="mono tiny">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          class="small"
          onclick={() => zoomAtCenter(1.18)}
          aria-label="Zoom in">+</button
        >
      </div>
    </div>
  </header>

  <div class="map-palette" aria-label="Object and NPC palette">
    <span class="palette-label">Palette</span>
    {#each PALETTE as item (item.kind)}
      <button
        type="button"
        class={paletteArmed === item.kind
          ? 'palette-chip armed'
          : 'palette-chip'}
        draggable="true"
        aria-pressed={paletteArmed === item.kind}
        onclick={() => armPalette(item.kind)}
        ondragstart={(event) => startPaletteDrag(event, item.kind)}
        ondragend={() => {
          paletteDragging = null;
          dropTarget = null;
        }}
      >
        <span class={`palette-dot ${item.kind}`}></span>
        {item.label}
      </button>
    {/each}
    <span class="fine-print palette-hint">
      Drag onto a room, or arm a chip and click one.
    </span>
  </div>

  <p class="map-notice" aria-live="polite">{notice}</p>

  <div class="map-shell">
    <div
      class="map-canvas"
      bind:clientWidth={canvasWidth}
      bind:clientHeight={canvasHeight}
    >
      <svg
        {@attach attachSvg}
        class="world-map"
        viewBox={`0 0 ${Math.max(1, canvasWidth)} ${Math.max(1, canvasHeight)}`}
        role="application"
        aria-label="World map: rooms, exits, and palette drops"
        onpointerdown={startPan}
        onpointermove={handlePointerMove}
        onpointerup={handlePointerUp}
        onpointercancel={handlePointerCancel}
        onwheel={onwheel}
      >
        <title>World map</title>
        <desc>
          Rooms are nodes, exits are edges. Drag a node to move it, drag a
          direction handle to connect, or drop a palette chip on a node.
        </desc>
        <defs>
          <pattern
            id="map-dots"
            width="24"
            height="24"
            patternUnits="userSpaceOnUse"
          >
            <circle cx="1.5" cy="1.5" r="1.1" class="map-dot"></circle>
          </pattern>
        </defs>
        <rect
          x="0"
          y="0"
          width={Math.max(1, canvasWidth)}
          height={Math.max(1, canvasHeight)}
          class="map-bg"
        ></rect>
        <rect
          x="0"
          y="0"
          width={Math.max(1, canvasWidth)}
          height={Math.max(1, canvasHeight)}
          fill="url(#map-dots)"
        ></rect>

        <g transform={`translate(${panX} ${panY}) scale(${zoom})`}>
          {#each renderedEdges as edge (edge.key)}
            {@const badge = edgeBadge(edge)}
            <g
              class={`map-edge ${edge.locked ? 'locked' : ''} ${edge.oneWay
                ? 'oneway'
                : ''}`}
              role="button"
              tabindex="0"
              aria-label={`Exit: ${edgeDescription(edge)}`}
              onpointerdown={(event) => event.stopPropagation()}
              onclick={(event) => selectEdge(event, edge)}
              onkeydown={(event) => edgeKeydown(event, edge)}
            >
              <title>{edgeDescription(edge)}</title>
              <line
                class="edge-hit"
                x1={edge.pA.x}
                y1={edge.pA.y}
                x2={edge.pB.x}
                y2={edge.pB.y}
              ></line>
              <line
                class="edge-line"
                x1={edge.pA.x}
                y1={edge.pA.y}
                x2={edge.pB.x}
                y2={edge.pB.y}
              ></line>
              <polygon
                class="edge-arrow"
                points={arrowPoints(edge.pB, edge.pA)}
              ></polygon>
              {#if edge.directions.length > 1 || !edge.oneWay}
                <polygon
                  class="edge-arrow"
                  points={arrowPoints(edge.pA, edge.pB)}
                ></polygon>
              {/if}
              {#each edge.directions as item (item.roomId + item.direction)}
                {@const at = edgeLabel(edge, item)}
                {@const reciprocal = reciprocalLabel(edge, item)}
                <text class="edge-direction" x={at.x} y={at.y}>
                  {item.direction}
                </text>
                {#if reciprocal}
                  {@const back = reciprocalPoint(edge, item)}
                  <text class="edge-direction" x={back.x} y={back.y}>
                    {reciprocal}
                  </text>
                {/if}
              {/each}
              {#if badge}
                {@const middle = labelPoint(edge.pA, edge.pB, 0.5)}
                <text class="edge-badge" x={middle.x} y={middle.y}>
                  {badge}
                </text>
              {/if}
            </g>
          {/each}

          {#if connection}
            <line
              class="edge-preview"
              x1={connection.originX}
              y1={connection.originY}
              x2={connection.x}
              y2={connection.y}
            ></line>
            <circle class="edge-preview-tip" cx={connection.x} cy={connection.y} r="5"></circle>
          {/if}

          {#each rooms as room (room.id)}
            {@const point = positions[room.id] ?? { x: 0, y: 0 }}
            {@const color = areaColor(room.area)}
            {@const errors = roomErrors[room.id] ?? 0}
            <g
              class={`map-node ${selectedId === room.id ? 'selected' : ''} ${
                errors > 0 ? 'has-error' : ''
              } ${dropTarget === room.id ? 'drop-target' : ''} ${
                armedConnection ? 'connect-target' : ''
              }`}
              transform={`translate(${point.x} ${point.y})`}
              data-room-id={room.id}
              role="button"
              tabindex="0"
              aria-label={`${room.name} (${room.id}), ${room.objects.length} objects, ${room.npcs.length} NPCs${
                errors > 0 ? `, ${errors} validation errors` : ''
              }`}
              style={`--node-stroke:${color.stroke};--node-fill:${color.fill}`}
              onpointerdown={(event) => startNodeDrag(event, room.id)}
              onclick={(event) => onNodeClick(event, room.id)}
              onkeydown={(event) => onNodeKeydown(event, room.id)}
              ondragover={(event) => handleRoomDragOver(event, room.id)}
              ondragleave={() => dragLeaveRoom(room.id)}
              ondrop={(event) => dropOnRoom(event, room.id)}
            >
              <rect
                class="node-card"
                width={NODE_WIDTH}
                height={NODE_HEIGHT}
                rx="12"
              ></rect>
              <rect
                class="node-area-bar"
                width={NODE_WIDTH}
                height="5"
                rx="2.5"
                fill={color.stroke}
              ></rect>
              <text class="node-name" x="14" y="31">
                {clipText(room.name || room.id, 23)}
              </text>
              <text class="node-id" x="14" y="49">{room.id}</text>
              <text class="node-meta" x="14" y="70">
                {room.objects.length} obj · {room.npcs.length} NPC
              </text>
              <text class="node-area" x="14" y="87">
                {clipText(room.area ?? 'world.yaml', 24)}
              </text>
              {#if room.id === startRoom}
                <g transform={`translate(${NODE_WIDTH - 60} 14)`}>
                  <rect class="start-pill" width="46" height="17" rx="8"></rect>
                  <text class="start-label" x="23" y="12">START</text>
                </g>
              {/if}
              {#if errors > 0}
                <g transform={`translate(${NODE_WIDTH - 24} 12)`}>
                  <circle class="error-dot" r="10"></circle>
                  <text class="error-count" x="0" y="4">{errors}</text>
                </g>
              {/if}
              {#each HANDLES as handle (handle.direction)}
                <g
                  class="node-handle"
                  role="button"
                  tabindex="0"
                  aria-label={`Connect ${room.name} ${handle.direction}`}
                  transform={`translate(${handle.x} ${handle.y})`}
                  onpointerdown={(event) =>
                    startConnection(
                      event,
                      room.id,
                      handle.direction,
                      handle.x,
                      handle.y,
                    )}
                  onclick={stopHandleClick}
                  onkeydown={(event) =>
                    armConnection(event, room.id, handle.direction)}
                >
                  <circle r="10"></circle>
                  <circle class="handle-hit" r="18"></circle>
                  <text x="0" y="4">{handle.label}</text>
                </g>
              {/each}
            </g>
          {/each}
        </g>
      </svg>

      {#if pending}
        <form
          class="add-room-card"
          style:left={`${Math.max(
            8,
            Math.min(pending.screenX + 12, Math.max(8, canvasWidth - 286)),
          )}px`}
          style:top={`${Math.max(
            8,
            Math.min(pending.screenY + 12, Math.max(8, canvasHeight - 214)),
          )}px`}
          onsubmit={submitAddRoom}
        >
          <div class="add-room-head">
            <strong>New room</strong>
            <span class="mono tiny muted"
              >{Math.round(pending.x)}, {Math.round(pending.y)}</span
            >
          </div>
          <label>
            Area
            <select bind:value={formArea}>
              {#each areaChoices as choice (choice.index)}
                <option value={choice.index}>{choice.label}</option>
              {/each}
            </select>
          </label>
          <label>
            Id
            <input class="mono" bind:value={formId} spellcheck="false" />
          </label>
          <label>
            Name
            <input bind:value={formName} />
          </label>
          {#if formError}
            <p class="error-box" role="alert">{formError}</p>
          {/if}
          <div class="inline-actions">
            <button type="submit" class="small primary">Add room</button>
            <button type="button" class="small ghost" onclick={cancelAddRoom}>
              Cancel
            </button>
          </div>
        </form>
      {/if}
    </div>

    <footer class="map-legend">
      {#each areaLegend as entry (entry.name)}
        <span>
          <i class="legend-dot" style={`background:${entry.color}`}></i>
          {entry.name}
        </span>
      {/each}
      <span class="legend-start">START marker</span>
      <span class="legend-error">validation badge</span>
      <span class="legend-handle">N/E/S/W handles connect exits</span>
    </footer>
  </div>
</section>

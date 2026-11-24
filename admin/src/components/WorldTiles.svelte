<script lang="ts">
  import type { PaletteKind, Room, RoomTab } from '../lib/types';
  import {
    fitCamera,
    levelLabel,
    placeWorld,
    tileMetrics,
    type TileCamera,
    type TileMetricsLevel,
  } from '../lib/worldTiles';

  interface Props {
    rooms: Room[];
    startRoom: string;
    selectedId: string | null;
    roomErrors: Record<string, number>;
    onselect: (roomId: string, tab?: RoomTab) => void;
    onpalette: (kind: PaletteKind, roomId: string) => void;
    ongenerategrids: () => void;
  }

  let {
    rooms,
    startRoom,
    selectedId,
    roomErrors,
    onselect,
    onpalette,
    ongenerategrids,
  }: Props = $props();

  interface PanDrag {
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startPanX: number;
    startPanY: number;
  }

  const CELL = 22;
  const PAD = 26;
  const HEADING = 26;
  const LEVEL_GAP = 34;
  const PALETTE_MIME = 'application/x-mud-palette';
  const AREA_HUES = [172, 202, 266, 32, 328, 92, 12, 232];
  const PALETTE: { kind: PaletteKind; label: string }[] = [
    { kind: 'item', label: 'Item' },
    { kind: 'container', label: 'Container' },
    { kind: 'readable', label: 'Readable' },
    { kind: 'npc', label: 'NPC' },
  ];

  let canvasWidth = $state(900);
  let canvasHeight = $state(640);
  let cameraState = $state<TileCamera | null>(null);
  let svgEl: SVGSVGElement | null = null;

  let notice = $state('');
  let paletteArmed = $state<PaletteKind | null>(null);
  let paletteDragging = $state<PaletteKind | null>(null);
  let dropTarget = $state<string | null>(null);
  let panDrag: PanDrag | null = null;

  const placement = $derived.by(() => placeWorld(rooms, startRoom));
  const metrics = $derived.by(() =>
    tileMetrics(placement, CELL, PAD, HEADING, LEVEL_GAP),
  );
  const defaultCamera = $derived.by(() =>
    initialCamera(metrics.width, metrics.height, canvasWidth, canvasHeight),
  );
  const camera = $derived(cameraState ?? defaultCamera);
  const zoom = $derived(camera.zoom);
  const panX = $derived(camera.panX);
  const panY = $derived(camera.panY);
  const missingGrids = $derived(rooms.filter((room) => !room.geometry).length);
  const detachedNames = $derived(
    placement.detached.map((room) => room.room.name || room.room.id),
  );
  const conflictRooms = $derived.by(() => {
    const ids: string[] = [];
    for (const conflict of placement.conflicts) {
      for (const id of conflict.rooms) {
        if (!ids.includes(id)) ids.push(id);
      }
    }
    return ids;
  });
  const areaLegend = $derived.by(() => {
    const names: string[] = [];
    const entries: { name: string; color: string }[] = [];
    for (const room of rooms) {
      const name = room.area ?? 'world.yaml';
      if (names.includes(name)) continue;
      names.push(name);
      entries.push({ name, color: areaColor(room.area).stroke });
    }
    return entries;
  });

  function attachSvg(node: SVGSVGElement): () => void {
    svgEl = node;
    return () => {
      svgEl = null;
    };
  }

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

  function areaHue(area: string | null): number {
    let hash = 0;
    for (const char of area ?? 'world') {
      hash = (hash * 31 + char.charCodeAt(0)) | 0;
    }
    return AREA_HUES[Math.abs(hash) % AREA_HUES.length] ?? 172;
  }

  function roomFor(id: string): Room | undefined {
    return rooms.find((room) => room.id === id);
  }

  function cellFill(cell: { char: string; owners: string[] }): string {
    const owner = roomFor(cell.owners[0] ?? '');
    const hue = areaHue(owner?.area ?? null);
    if (cell.char === '#') return `hsl(${hue} 16% 19%)`;
    if (cell.char === '+') return '#b98a34';
    if (cell.char === '^' || cell.char === 'v') return '#2f5da8';
    return `hsl(${hue} 38% 27%)`;
  }

  function levelView(level: number): TileMetricsLevel | undefined {
    return metrics.levels.find((entry) => entry.level.level === level);
  }

  function cellX(x: number): number {
    return PAD + (x - metrics.minX) * CELL;
  }

  function cellY(y: number, view: TileMetricsLevel): number {
    return view.top + (y - view.level.bounds.minY) * CELL;
  }

  function roomRect(roomId: string): {
    left: number;
    top: number;
    width: number;
    height: number;
  } | null {
    const placed = placement.placed.find(
      (candidate) => candidate.room.id === roomId,
    );
    const view = placed ? levelView(placed.level) : undefined;
    if (!placed || !view) return null;
    return {
      left: cellX(placed.x),
      top: cellY(placed.y, view),
      width: placed.footprint.width * CELL,
      height: placed.footprint.height * CELL,
    };
  }

  function roomCenter(roomId: string): { x: number; y: number } | null {
    const rect = roomRect(roomId);
    if (!rect) return null;
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    };
  }

  function initialCamera(
    width: number,
    height: number,
    viewWidth: number,
    viewHeight: number,
  ): TileCamera {
    const fitted = fitCamera(width, height, viewWidth, viewHeight, 14);
    if (fitted.zoom >= 0.8 || !placement.startId) return fitted;
    const center = roomCenter(placement.startId);
    if (!center) return fitted;
    const initialZoom = 0.9;
    return {
      zoom: initialZoom,
      panX: viewWidth / 2 - center.x * initialZoom,
      panY: viewHeight / 2 - center.y * initialZoom,
    };
  }

  function clampZoom(value: number): number {
    return Math.max(0.2, Math.min(2.5, value));
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

  function fit(): void {
    cameraState = fitCamera(
      metrics.width,
      metrics.height,
      canvasWidth,
      canvasHeight,
      14,
    );
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

  function startPan(event: PointerEvent): void {
    if (event.button !== 0) return;
    panDrag = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startPanX: panX,
      startPanY: panY,
    };
    svgEl?.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: PointerEvent): void {
    if (!panDrag) return;
    cameraState = {
      zoom: camera.zoom,
      panX: panDrag.startPanX + (event.clientX - panDrag.startClientX),
      panY: panDrag.startPanY + (event.clientY - panDrag.startClientY),
    };
  }

  function handlePointerUp(event: PointerEvent): void {
    if (!panDrag) return;
    panDrag = null;
    svgEl?.releasePointerCapture(event.pointerId);
  }

  function activateRoom(roomId: string): void {
    if (paletteArmed) {
      const kind = paletteArmed;
      paletteArmed = null;
      onpalette(kind, roomId);
      notice = `Added ${article(kind)} ${kind === 'npc' ? 'NPC' : kind} to ${
        roomFor(roomId)?.name || roomId
      }.`;
      return;
    }
    onselect(roomId);
  }

  function article(kind: PaletteKind): string {
    return kind === 'item' || kind === 'npc' ? 'an' : 'a';
  }

  function onRoomKeydown(event: KeyboardEvent, roomId: string): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    activateRoom(roomId);
  }

  function armPalette(kind: PaletteKind): void {
    paletteArmed = paletteArmed === kind ? null : kind;
    paletteDragging = null;
    if (paletteArmed) {
      notice = `Click a room to add ${article(kind)} ${
        kind === 'npc' ? 'NPC' : kind
      }.`;
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
    notice = `Added ${article(kind)} ${kind === 'npc' ? 'NPC' : kind} to ${
      roomFor(roomId)?.name || roomId
    }.`;
  }

  function linkBadges(roomId: string): string[] {
    const badges: string[] = [];
    const add = (badge: string): void => {
      if (!badges.includes(badge)) badges.push(badge);
    };
    for (const link of placement.links) {
      if (link.sourceId !== roomId) continue;
      if (link.oneWay) add('1-way');
      if (link.kind === 'vertical') {
        add(link.direction === 'up' ? '↑' : '↓');
      } else if (link.kind === 'portal') {
        add(link.direction);
      }
    }
    return badges.slice(0, 3);
  }

  function clipText(value: string, limit: number): string {
    return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
  }

  function labelLines(room: Room): { name: string; meta: string } {
    const placed = placement.placed.find(
      (candidate) => candidate.room.id === room.id,
    );
    const width = placed ? placed.footprint.width * CELL - 12 : 120;
    const isStart = room.id === placement.startId;
    const maxChars = Math.max(
      8,
      Math.floor((width - (isStart ? 54 : 0)) / 6.1),
    );
    return {
      name: clipText(room.name || room.id, maxChars),
      meta: clipText(
        `${room.id || 'no id'} · ${room.objects.length} obj · ${
          room.npcs.length
        } NPC`,
        Math.max(10, Math.floor(width / 5.2)),
      ),
    };
  }
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key !== 'Escape') return;
    if (paletteArmed) {
      paletteArmed = null;
      notice = '';
    }
  }}
/>

<section class="map-panel tiles-panel" aria-label="Spatial world map">
  <header class="map-head">
    <div>
      <h3>Spatial world</h3>
      <p class="muted">
        Room grids are placed from exits and door cells. Levels stack; use
        Connections for manual positions and exit handles.
      </p>
    </div>
    <div class="map-controls">
      <button
        type="button"
        class="small"
        onclick={ongenerategrids}
        disabled={missingGrids === 0}
      >
        {missingGrids === 0
          ? 'All rooms have grids'
          : `Generate missing grids (${missingGrids})`}
      </button>
      <button type="button" class="small" onclick={fit}>Fit</button>
      <div class="zoom-controls" aria-label="Tile map zoom">
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

  {#if placement.conflicts.length > 0}
    <div class="error-box placement-conflicts" role="alert">
      <strong>
        {placement.conflicts.length} placement conflict{placement.conflicts
          .length === 1
          ? ''
          : 's'}
      </strong>
      <ul>
        {#each placement.conflicts as conflict, index (index)}
          <li>{conflict.message}</li>
        {/each}
      </ul>
      <div class="inline-actions conflict-jump">
        {#each conflictRooms as id (id)}
          <button
            type="button"
            class="small"
            onclick={() => onselect(id)}
          >{roomFor(id)?.name || id}</button>
        {/each}
      </div>
    </div>
  {/if}

  {#if placement.detached.length > 0}
    <p class="fine-print detached-note">
      <strong>Detached:</strong>
      {detachedNames.join(', ')} — no cardinal path from the start room; shown
      in the level 0 detached band.
    </p>
  {/if}

  {#if placement.portals.length > 0}
    <details class="portal-note">
      <summary>
        {placement.portals.length} portal link{placement.portals.length === 1
          ? ''
          : 's'} (not placed in 2D)
      </summary>
      <ul>
        {#each placement.portals as portal, index (index)}
          <li>
            <span class="mono tiny">{portal.sourceId}</span>
            {portal.direction} →
            <span class="mono tiny">{portal.targetId}</span>
            {#if portal.oneWay}<span class="badge warn">one-way</span>{/if}
          </li>
        {/each}
      </ul>
    </details>
  {/if}

  <div class="map-shell">
    <div
      class="map-canvas tiles-canvas"
      bind:clientWidth={canvasWidth}
      bind:clientHeight={canvasHeight}
    >
      <svg
        {@attach attachSvg}
        class="world-map tiles-map"
        viewBox={`0 0 ${Math.max(1, canvasWidth)} ${Math.max(1, canvasHeight)}`}
        role="application"
        aria-label="Spatial world map of room tiles"
        onpointerdown={startPan}
        onpointermove={handlePointerMove}
        onpointerup={handlePointerUp}
        onpointercancel={handlePointerUp}
        onwheel={onwheel}
      >
        <title>Spatial world map</title>
        <desc>
          Room floor plans placed by exits. Select a room to edit it; drop a
          palette chip to add an object or NPC.
        </desc>
        <rect
          x="0"
          y="0"
          width={Math.max(1, canvasWidth)}
          height={Math.max(1, canvasHeight)}
          class="map-bg"
        ></rect>

        <g transform={`translate(${panX} ${panY}) scale(${zoom})`}>
          {#each metrics.levels as view (view.level.level)}
            <text class="level-heading" x={PAD} y={view.headingY}>
              {levelLabel(view.level.level)}
            </text>
            <line
              class="level-rule"
              x1={PAD}
              y1={view.headingY + 7}
              x2={metrics.width - PAD}
              y2={view.headingY + 7}
            ></line>

            {#if view.level.level === 0 && view.level.detachedBounds}
              {@const detachedY =
                cellY(view.level.detachedBounds.minY, view) - 8}
              <line
                class="detached-divider"
                x1={PAD}
                y1={detachedY}
                x2={metrics.width - PAD}
                y2={detachedY}
              ></line>
              <text class="detached-heading" x={PAD} y={detachedY - 5}>
                Detached
              </text>
            {/if}

            {#each view.level.cells as cell (cell.x + ',' + cell.y)}
              {@const x = cellX(cell.x)}
              {@const y = cellY(cell.y, view)}
              <rect
                class={`tile tile-${cell.char === '#' ? 'wall' : cell.char === '+' ? 'door' : cell.char === '^' || cell.char === 'v' ? 'stairs' : 'floor'}`}
                x={x}
                y={y}
                width={CELL}
                height={CELL}
                fill={cellFill(cell)}
              ></rect>
              {#if cell.char === '^' || cell.char === 'v'}
                <text
                  class="tile-glyph"
                  x={x + CELL / 2}
                  y={y + CELL / 2 + 3}
                >
                  {cell.char === '^' ? '↑' : '↓'}
                </text>
              {/if}
            {/each}

            {#each view.level.rooms as placed (placed.room.id)}
              {@const room = placed.room}
              {@const rect = roomRect(room.id)}
              {@const color = areaColor(room.area)}
              {@const errors = roomErrors[room.id] ?? 0}
              {@const conflict = conflictRooms.includes(room.id)}
              {@const lines = labelLines(room)}
              {@const badges = linkBadges(room.id)}
              {@const isStart = room.id === placement.startId}
              {@const labelWidth = placed.footprint.width * CELL - 8}
              {#if rect}
                <rect
                  class={`room-outline ${placed.footprint.authored ? '' : 'generated'} ${selectedId === room.id ? 'selected' : ''} ${conflict ? 'conflict' : ''} ${dropTarget === room.id ? 'drop-target' : ''}`}
                  x={rect.left + 0.5}
                  y={rect.top + 0.5}
                  width={rect.width - 1}
                  height={rect.height - 1}
                  rx="3"
                  fill="none"
                  stroke={color.stroke}
                ></rect>

                {#each placed.footprint.props as prop, index (index)}
                  <rect
                    class="prop-tile"
                    x={cellX(placed.x + prop.cell[0]) + CELL / 2 - 4}
                    y={cellY(placed.y + prop.cell[1], view) + CELL / 2 - 4}
                    width="8"
                    height="8"
                    rx="1"
                  ></rect>
                {/each}

                {#each Object.entries(placed.footprint.spawn) as [direction, cell] (direction)}
                  <circle
                    class="spawn-dot"
                    cx={cellX(placed.x + cell[0]) + CELL / 2}
                    cy={cellY(placed.y + cell[1], view) + CELL / 2}
                    r="3"
                  >
                    <title>Spawn {direction}</title>
                  </circle>
                {/each}

                <g class="tile-label" aria-hidden="true">
                  <rect
                    class="tile-label-bg"
                    x={rect.left + 4}
                    y={rect.top + 4}
                    width={Math.max(52, labelWidth)}
                    height="31"
                    rx="4"
                  ></rect>
                  <text
                    class="tile-name"
                    x={rect.left + 9}
                    y={rect.top + 17}
                  >{lines.name}</text>
                  <text
                    class="tile-meta"
                    x={rect.left + 9}
                    y={rect.top + 29}
                  >{lines.meta}</text>
                  {#if isStart}
                    <rect
                      class="start-pill"
                      x={rect.left + Math.max(52, labelWidth) - 40}
                      y={rect.top + 8}
                      width="38"
                      height="15"
                      rx="7"
                    ></rect>
                    <text
                      class="start-label"
                      x={rect.left + Math.max(52, labelWidth) - 21}
                      y={rect.top + 18}
                    >START</text>
                  {/if}
                </g>

                {#if errors > 0}
                  <g
                    transform={`translate(${rect.left + rect.width - 13} ${rect.top + rect.height - 13})`}
                  >
                    <circle class="error-dot" r="9"></circle>
                    <text class="error-count" x="0" y="4">{errors}</text>
                  </g>
                {/if}

                {#if badges.length > 0}
                  {#each badges as badge, index (badge)}
                    <g
                      class="tile-badge"
                      transform={`translate(${rect.left + 5 + index * 37} ${rect.top + rect.height - 22})`}
                    >
                      <rect width="34" height="14" rx="4"></rect>
                      <text x="17" y="10">{badge}</text>
                    </g>
                  {/each}
                {/if}

                <g
                  class="room-hit"
                  role="button"
                  tabindex="0"
                  aria-pressed={selectedId === room.id}
                  aria-label={`${room.name} (${room.id}), ${placed.level === 0 ? 'ground' : `level ${placed.level}`}, ${room.objects.length} objects, ${room.npcs.length} NPCs`}
                  onpointerdown={(event) => event.stopPropagation()}
                  onclick={() => activateRoom(room.id)}
                  onkeydown={(event) => onRoomKeydown(event, room.id)}
                  ondragover={(event) => handleRoomDragOver(event, room.id)}
                  ondragleave={() => dragLeaveRoom(room.id)}
                  ondrop={(event) => dropOnRoom(event, room.id)}
                >
                  <rect
                    x={rect.left}
                    y={rect.top}
                    width={rect.width}
                    height={rect.height}
                    fill="rgba(255,255,255,0.001)"
                  ></rect>
                </g>
              {/if}
            {/each}
          {/each}
        </g>
      </svg>
    </div>

    <footer class="map-legend tiles-legend">
      <span><i class="swatch wall"></i>wall</span>
      <span><i class="swatch floor"></i>floor</span>
      <span><i class="swatch door"></i>door</span>
      <span><i class="swatch stairs"></i>stairs</span>
      <span><i class="swatch prop"></i>prop</span>
      <span><i class="swatch spawn"></i>spawn</span>
      {#each areaLegend as entry (entry.name)}
        <span>
          <i
            class="legend-dot"
            style={`background:${entry.color}`}
          ></i>
          {entry.name}
        </span>
      {/each}
      <span class="tiles-legend-note">dashed outline = generated grid</span>
    </footer>
  </div>
</section>

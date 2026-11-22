<script lang="ts">
  import type { CellChar, Geometry, Prop, Room } from '../lib/types';
  import { CELL_CHARS, defaultGeometry } from '../lib/types';

  interface Props {
    geometry: Geometry | null;
    room: Room;
    onchange: (geometry: Geometry | null) => void;
  }

  let { geometry, room, onchange }: Props = $props();
  const CELL = 32;
  const PAD = 10;

  let spawnDirection = $state('');
  let propError = $state('');

  const cells = $derived(geometry?.cells ?? []);
  const width = $derived(cells[0]?.length ?? 0);
  const height = $derived(cells.length);
  const viewBox = $derived(
    `0 0 ${width * CELL + PAD * 2} ${height * CELL + PAD * 2}`,
  );
  const spawnDirections = $derived(
    Object.keys(room.exits).filter(
      (direction) => geometry && !(direction in geometry.spawn),
    ),
  );

  function cellClass(char: string): string {
    if (char === '#') return 'wall';
    if (char === '+') return 'door';
    if (char === '^') return 'stairs';
    if (char === 'v') return 'stairs';
    return 'floor';
  }

  function cycle(x: number, y: number): void {
    if (!geometry) return;
    const rows = [...geometry.cells];
    const row = rows[y];
    if (!row || x < 0 || x >= row.length) return;
    const current = row[x] as CellChar;
    const found = CELL_CHARS.indexOf(current);
    const next = CELL_CHARS[(found + 1) % CELL_CHARS.length];
    rows[y] = row.slice(0, x) + next + row.slice(x + 1);
    onchange({ ...geometry, cells: rows });
  }

  function addMap(): void {
    onchange(defaultGeometry(room));
    spawnDirection = '';
  }

  function resetMap(): void {
    if (!geometry) return;
    if (!window.confirm('Replace this map with a grid derived from the exits?')) {
      return;
    }
    onchange(defaultGeometry(room));
    spawnDirection = '';
  }

  function removeMap(): void {
    if (!geometry) return;
    if (!window.confirm('Remove this room map? Clients will fall back to text.')) {
      return;
    }
    onchange(null);
  }

  function updateSpawn(direction: string, axis: 0 | 1, value: number): void {
    if (!geometry) return;
    const cell = geometry.spawn[direction];
    if (!cell) return;
    const next: [number, number] = [cell[0], cell[1]];
    next[axis] = Math.max(0, Math.trunc(value));
    onchange({
      ...geometry,
      spawn: { ...geometry.spawn, [direction]: next },
    });
  }

  function removeSpawn(direction: string): void {
    if (!geometry) return;
    const spawn = { ...geometry.spawn };
    delete spawn[direction];
    onchange({ ...geometry, spawn });
  }

  function autoSpawn(direction: string): [number, number] | null {
    if (!geometry) return null;
    const rows = geometry.cells;
    if (direction === 'up' || direction === 'down') {
      const symbol = direction === 'up' ? '^' : 'v';
      for (let y = 0; y < rows.length; y += 1) {
        const x = rows[y]?.indexOf(symbol) ?? -1;
        if (x >= 0) return [x, y];
      }
      return null;
    }
    const offsets: Record<string, [number, number]> = {
      north: [0, 1],
      south: [0, -1],
      west: [1, 0],
      east: [-1, 0],
    };
    const offset = offsets[direction];
    if (!offset) return null;
    for (let y = 0; y < rows.length; y += 1) {
      for (let x = 0; x < (rows[y]?.length ?? 0); x += 1) {
        if (rows[y]?.[x] !== '+') continue;
        const targetX = x + offset[0];
        const targetY = y + offset[1];
        if (rows[targetY]?.[targetX] === '.') return [targetX, targetY];
      }
    }
    return null;
  }

  function addSpawn(): void {
    if (!geometry) return;
    const direction = spawnDirection || spawnDirections[0];
    if (!direction) return;
    const cell = autoSpawn(direction);
    if (!cell) {
      propError = `Could not derive a ${direction} spawn from the map; place the door or stairs first.`;
      return;
    }
    propError = '';
    onchange({
      ...geometry,
      spawn: { ...geometry.spawn, [direction]: cell },
    });
    spawnDirection = '';
  }

  function firstFloor(): [number, number] {
    if (!geometry) return [1, 1];
    for (let y = 1; y < geometry.cells.length - 1; y += 1) {
      for (let x = 1; x < (geometry.cells[y]?.length ?? 0) - 1; x += 1) {
        if (geometry.cells[y]?.[x] === '.') return [x, y];
      }
    }
    return [1, 1];
  }

  function addProp(): void {
    if (!geometry) return;
    const prop: Prop = { kind: 'prop', cell: firstFloor() };
    onchange({ ...geometry, props: [...geometry.props, prop] });
  }

  function updatePropKind(index: number, kind: string): void {
    if (!geometry) return;
    const props = geometry.props.map((prop, at) =>
      at === index ? { ...prop, kind } : prop,
    );
    onchange({ ...geometry, props });
  }

  function updatePropCell(index: number, axis: 0 | 1, value: number): void {
    if (!geometry) return;
    const props = geometry.props.map((prop, at) => {
      if (at !== index) return prop;
      const cell: [number, number] = [prop.cell[0], prop.cell[1]];
      cell[axis] = Math.max(0, Math.trunc(value));
      return { ...prop, cell };
    });
    onchange({ ...geometry, props });
  }

  function removeProp(index: number): void {
    if (!geometry) return;
    onchange({
      ...geometry,
      props: geometry.props.filter((_, at) => at !== index),
    });
  }
</script>

<section class="editor-section">
  <div class="section-head">
    <div>
      <h3>Geometry</h3>
      <p class="muted">
        Click a cell to cycle <code>#</code>, <code>.</code>, <code>+</code>,
        <code>^</code>, <code>v</code>. Doors must sit on an edge and match an
        exit; stairs must match an up/down exit.
      </p>
    </div>
    <div class="inline-actions">
      {#if geometry}
        <button type="button" class="small" onclick={resetMap}>
          Reset from exits
        </button>
        <button type="button" class="small danger-ghost" onclick={removeMap}>
          Remove map
        </button>
      {:else}
        <button type="button" class="small primary" onclick={addMap}>
          Add default grid
        </button>
      {/if}
    </div>
  </div>

  {#if propError}
    <p class="error-box" role="alert">{propError}</p>
  {/if}

  {#if !geometry}
    <p class="empty">
      This room has no geometry. Add a default 9×7 grid, then draw its doors.
    </p>
  {:else}
    <div class="geometry-layout">
      <div class="map-card">
        <svg
          class="geometry-map"
          viewBox={viewBox}
          role="img"
          aria-label={`Room map for ${room.name}, ${width} by ${height}`}
        >
          <rect
            x="0"
            y="0"
            width={width * CELL + PAD * 2}
            height={height * CELL + PAD * 2}
            class="map-bg"
          />
          {#each cells as row, y (y)}
            {#each [...row] as char, x (x)}
              <g
                class="map-cell"
                role="button"
                tabindex="0"
                aria-label={`Cell ${x}, ${y}: ${char}. Activate to cycle the symbol.`}
                onclick={() => cycle(x, y)}
                onkeydown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    cycle(x, y);
                  }
                }}
              >
                <rect
                  x={PAD + x * CELL}
                  y={PAD + y * CELL}
                  width={CELL}
                  height={CELL}
                  class={`cell ${cellClass(char)}`}
                />
                <text
                  x={PAD + x * CELL + CELL / 2}
                  y={PAD + y * CELL + CELL / 2}
                  class="cell-label"
                >{char}</text>
              </g>
            {/each}
          {/each}
          {#each Object.entries(geometry.spawn) as [direction, cell] (direction)}
            <circle
              cx={PAD + cell[0] * CELL + CELL / 2}
              cy={PAD + cell[1] * CELL + CELL / 2}
              r="7"
              class="spawn-mark"
            />
            <text
              x={PAD + cell[0] * CELL + CELL / 2}
              y={PAD + cell[1] * CELL + CELL - 7}
              class="overlay-label"
            >{direction.slice(0, 1)}</text>
          {/each}
          {#each geometry.props as prop, index (index)}
            <g class="prop-mark">
              <rect
                x={PAD + prop.cell[0] * CELL + 8}
                y={PAD + prop.cell[1] * CELL + 8}
                width="12"
                height="12"
                rx="2"
              />
              <text
                x={PAD + prop.cell[0] * CELL + 14}
                y={PAD + prop.cell[1] * CELL + 17}
                class="overlay-label"
              >{prop.kind.slice(0, 1).toUpperCase()}</text>
            </g>
          {/each}
        </svg>
        <div class="legend">
          <span><i class="swatch wall"></i>wall</span>
          <span><i class="swatch floor"></i>floor</span>
          <span><i class="swatch door"></i>door</span>
          <span><i class="swatch stairs"></i>stairs</span>
          <span><i class="swatch spawn"></i>spawn</span>
          <span><i class="swatch prop"></i>prop</span>
        </div>
      </div>

      <div class="geometry-side">
        <h4>Spawn points</h4>
        {#if Object.keys(geometry.spawn).length === 0}
          <p class="fine-print">
            No explicit spawn points. Clients use the floor cell just inside
            each door.
          </p>
        {:else}
          <div class="table-wrap compact">
            <table>
              <thead><tr><th>Exit</th><th>X</th><th>Y</th><th></th></tr></thead>
              <tbody>
                {#each Object.entries(geometry.spawn) as [direction, cell] (direction)}
                  <tr>
                    <td>{direction}</td>
                    <td>
                      <input
                        type="number"
                        value={cell[0]}
                        aria-label={`${direction} spawn x`}
                        onchange={(event) =>
                          updateSpawn(direction, 0, Number(event.currentTarget.value))}
                      />
                    </td>
                    <td>
                      <input
                        type="number"
                        value={cell[1]}
                        aria-label={`${direction} spawn y`}
                        onchange={(event) =>
                          updateSpawn(direction, 1, Number(event.currentTarget.value))}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        class="small danger-ghost"
                        onclick={() => removeSpawn(direction)}
                        aria-label={`Remove ${direction} spawn`}>×</button
                      >
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
        {#if spawnDirections.length > 0}
          <div class="inline-actions">
            <select bind:value={spawnDirection} aria-label="Direction for a new spawn">
              <option value="">{spawnDirections[0]}</option>
              {#each spawnDirections as direction (direction)}
                <option value={direction}>{direction}</option>
              {/each}
            </select>
            <button type="button" class="small" onclick={addSpawn}>Add spawn</button>
          </div>
        {/if}

        <h4>Props</h4>
        {#if geometry.props.length === 0}
          <p class="fine-print">No props on this map.</p>
        {:else}
          <div class="prop-list">
            {#each geometry.props as prop, index (index)}
              <div class="prop-row">
                <input
                  value={prop.kind}
                  aria-label={`Prop ${index + 1} kind`}
                  oninput={(event) =>
                    updatePropKind(index, event.currentTarget.value)}
                />
                <input
                  type="number"
                  value={prop.cell[0]}
                  aria-label={`Prop ${index + 1} x`}
                  onchange={(event) =>
                    updatePropCell(index, 0, Number(event.currentTarget.value))}
                />
                <input
                  type="number"
                  value={prop.cell[1]}
                  aria-label={`Prop ${index + 1} y`}
                  onchange={(event) =>
                    updatePropCell(index, 1, Number(event.currentTarget.value))}
                />
                <button
                  type="button"
                  class="small danger-ghost"
                  onclick={() => removeProp(index)}
                  aria-label={`Remove prop ${index + 1}`}>×</button
                >
              </div>
            {/each}
          </div>
        {/if}
        <button type="button" class="small" onclick={addProp}>Add prop</button>
      </div>
    </div>
  {/if}
</section>

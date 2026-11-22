<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError, errorList, errorText } from '../lib/api';
  import type {
    AddRoomRequest,
    ApplyResult,
    ConnectRequest,
    GameObject,
    Layout,
    NPC,
    PaletteKind,
    Room,
    RoomTab,
    SaveResult,
    WorldDoc,
  } from '../lib/types';
  import {
    filesize,
    newRoom,
    normalizeWorldDoc,
    OPPOSITE_EXITS,
    uniqueId,
    worldPayload,
  } from '../lib/types';
  import { autoLayout, positionForNewRoom } from '../lib/layout';
  import RoomEditor from './RoomEditor.svelte';
  import WorldMap from './WorldMap.svelte';

  interface Props {
    token: string;
    onDirty: (dirty: boolean) => void;
    onUnauthorized: () => void;
    onApplied: () => void | Promise<void>;
  }

  let { token, onDirty, onUnauthorized, onApplied }: Props = $props();

  interface Selection {
    area: number;
    room: number;
  }

  const ROOM_ID = /^[a-z0-9_]+$/;

  let doc = $state<WorldDoc | null>(null);
  let selection = $state<Selection | null>(null);
  let loading = $state(true);
  let loadError = $state('');
  let actionError = $state('');
  let message = $state('');
  let dirty = $state(false);
  let busy = $state<'validate' | 'save' | 'apply' | null>(null);
  let errors = $state<string[]>([]);
  let lastSaved = $state<SaveResult | null>(null);
  let newAreaName = $state('');
  let applyConfirm = $state<number | null>(null);
  let roomTab = $state<RoomTab>('room');
  let focusObjectId = $state<string | null>(null);
  let focusNpcId = $state<string | null>(null);

  const allRooms = $derived(doc ? doc.areas.flatMap((area) => area.rooms) : []);
  const allIds = $derived.by(() => {
    const ids: string[] = [];
    for (const room of allRooms) {
      ids.push(room.id);
      for (const object of room.objects) ids.push(object.id);
      for (const npc of room.npcs) ids.push(npc.id);
    }
    return ids;
  });
  const objectIds = $derived(
    Array.from(
      new Set(allRooms.flatMap((room) => room.objects.map((object) => object.id))),
    ).sort(),
  );
  const areaNames = $derived(
    Array.from(
      new Set(
        (doc?.areas ?? [])
          .map((area) => area.area)
          .filter((name): name is string => Boolean(name)),
      ),
    ),
  );
  const selectedArea = $derived(
    doc && selection ? (doc.areas[selection.area] ?? null) : null,
  );
  const selectedRoom = $derived(
    selectedArea && selection
      ? (selectedArea.rooms[selection.room] ?? null)
      : null,
  );
  const roomErrors = $derived.by(() => {
    if (!selectedRoom || !selectedRoom.id) return [];
    const needles = [`room '${selectedRoom.id}'`, `id '${selectedRoom.id}'`];
    return errors.filter((error) =>
      needles.some((needle) => error.includes(needle)),
    );
  });
  const roomErrorCounts = $derived.by(() => {
    const counts: Record<string, number> = {};
    for (const room of allRooms) {
      if (!room.id) continue;
      const needles = [`room '${room.id}'`, `id '${room.id}'`];
      const count = errors.filter((error) =>
        needles.some((needle) => error.includes(needle)),
      ).length;
      if (count > 0) counts[room.id] = count;
    }
    return counts;
  });
  const effectiveLayout = $derived(
    doc
      ? {
          ...autoLayout(allRooms, doc.world.start_room),
          ...doc.layout,
        }
      : {},
  );
  const suggestedRoomId = $derived(uniqueId('new_room', new Set(allIds)));
  const areaChoices = $derived(
    (doc?.areas ?? []).map((area, index) => ({
      index,
      label: area.area ?? area.file,
    })),
  );

  onMount(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    void initialLoad();
    return () => window.removeEventListener('beforeunload', handler);
  });

  function locateRoom(id: string): Selection | null {
    if (!doc) return null;
    for (let areaIndex = 0; areaIndex < doc.areas.length; areaIndex += 1) {
      const roomIndex = doc.areas[areaIndex]?.rooms.findIndex(
        (room) => room.id === id,
      );
      if (roomIndex !== undefined && roomIndex >= 0) {
        return { area: areaIndex, room: roomIndex };
      }
    }
    return null;
  }

  function selectRoomById(
    id: string | undefined,
    tab?: RoomTab,
    keepFocus = false,
  ): void {
    if (!doc || !id) {
      selection = doc?.areas[0]?.rooms[0] ? { area: 0, room: 0 } : null;
    } else {
      selection =
        locateRoom(id) ??
        (doc.areas[0]?.rooms[0] ? { area: 0, room: 0 } : null);
    }
    if (tab) roomTab = tab;
    if (!keepFocus) {
      focusObjectId = null;
      focusNpcId = null;
    }
  }

  async function fetchWorld(): Promise<void> {
    const wanted = selectedRoom?.id;
    const next = normalizeWorldDoc(
      await api<unknown>(token, 'GET', '/api/world'),
    );
    doc = next;
    errors = next.validation?.errors ?? [];
    dirty = false;
    onDirty(false);
    selectRoomById(wanted ?? next.areas[0]?.rooms[0]?.id);
  }

  async function initialLoad(): Promise<void> {
    loading = true;
    loadError = '';
    try {
      await fetchWorld();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      loadError = errorText(caught);
    } finally {
      loading = false;
    }
  }

  function markDirty(): void {
    dirty = true;
    onDirty(true);
    message = '';
    actionError = '';
  }

  function updateWorld(patch: Partial<WorldDoc['world']>): void {
    if (!doc) return;
    doc = { ...doc, world: { ...doc.world, ...patch } };
    markDirty();
  }

  function updateSelectedRoom(room: Room): void {
    if (!doc || !selection) return;
    const { area, room: roomIndex } = selection;
    const previousId = selectedRoom?.id;
    let layout = doc.layout;
    if (previousId && previousId !== room.id && layout[previousId]) {
      const point = layout[previousId];
      const nextLayout = { ...layout };
      delete nextLayout[previousId];
      nextLayout[room.id] = point;
      layout = nextLayout;
    }
    doc = {
      ...doc,
      layout,
      areas: doc.areas.map((section, areaIndex) =>
        areaIndex === area
          ? {
              ...section,
              rooms: section.rooms.map((candidate, index) =>
                index === roomIndex ? room : candidate,
              ),
            }
          : section,
      ),
    };
    markDirty();
  }

  function selectRoom(areaIndex: number, roomIndex: number): void {
    selection = { area: areaIndex, room: roomIndex };
    focusObjectId = null;
    focusNpcId = null;
  }

  function createRoom(
    areaIndex: number,
    options: { id?: string; name?: string; x?: number; y?: number } = {},
  ): string | null {
    if (!doc) return 'The world is not loaded.';
    const section = doc.areas[areaIndex];
    if (!section) return 'Choose an area for the new room.';
    const ids = new Set(allIds);
    const candidate = options.id?.trim() ?? '';
    if (candidate && !ROOM_ID.test(candidate)) {
      return 'A room id may use lowercase letters, digits, and underscores.';
    }
    if (candidate && ids.has(candidate)) {
      return `The id ${candidate} is already used by a room, object, or NPC.`;
    }
    const room = newRoom(section.area, ids);
    room.id = candidate || room.id;
    room.name = options.name?.trim() || 'New Room';
    const position = positionForNewRoom(effectiveLayout, allRooms);
    const x = options.x ?? position.x;
    const y = options.y ?? position.y;
    doc = {
      ...doc,
      areas: doc.areas.map((candidateSection, index) =>
        index === areaIndex
          ? { ...candidateSection, rooms: [...candidateSection.rooms, room] }
          : candidateSection,
      ),
      layout: { ...doc.layout, [room.id]: { x, y } },
    };
    selection = { area: areaIndex, room: section.rooms.length };
    roomTab = 'room';
    focusObjectId = null;
    focusNpcId = null;
    actionError = '';
    markDirty();
    return null;
  }

  function addRoom(areaIndex: number): void {
    const error = createRoom(areaIndex);
    if (error) actionError = error;
  }

  function createRoomFromMap(request: AddRoomRequest): string | null {
    return createRoom(request.areaIndex, {
      id: request.id,
      name: request.name,
      x: request.x,
      y: request.y,
    });
  }

  function updateLayout(next: Layout): void {
    if (!doc) return;
    doc = { ...doc, layout: next };
    markDirty();
  }

  function replaceRooms(changes: Record<string, Room>): void {
    if (!doc) return;
    doc = {
      ...doc,
      areas: doc.areas.map((section) => ({
        ...section,
        rooms: section.rooms.map((room) => changes[room.id] ?? room),
      })),
    };
    markDirty();
  }

  function roomName(roomId: string): string {
    return allRooms.find((room) => room.id === roomId)?.name || roomId;
  }

  function connectRooms(request: ConnectRequest): string | null {
    if (!doc) return 'The world is not loaded.';
    const source = allRooms.find((room) => room.id === request.sourceId);
    const target = allRooms.find((room) => room.id === request.targetId);
    if (!source || !target) return 'Both rooms must still exist.';
    if (source.id === target.id) return 'A room cannot link to itself.';
    const alreadyLinked =
      Object.values(source.exits).some((exit) => exit.to === target.id) ||
      Object.values(target.exits).some((exit) => exit.to === source.id);
    if (alreadyLinked) {
      return `${source.name} and ${target.name} are already connected.`;
    }
    const opposite = OPPOSITE_EXITS[request.direction];
    if (!opposite) {
      return `The map has no handle for ${request.direction}; use the Exits tab.`;
    }
    if (source.exits[request.direction]) {
      const replaced = source.exits[request.direction];
      if (
        !window.confirm(
          `Replace the ${request.direction} exit from ${source.name} to ${
            roomName(replaced.to)
          }?`,
        )
      ) {
        return 'Connection cancelled.';
      }
    }
    const reverse = target.exits[opposite];
    if (!request.oneWay && reverse) {
      if (
        !window.confirm(
          `${target.name} already has a ${opposite} exit to ${roomName(
            reverse.to,
          )}. Replace it?`,
        )
      ) {
        return 'Connection cancelled.';
      }
    }
    const changes: Record<string, Room> = {};
    changes[source.id] = {
      ...source,
      exits: {
        ...source.exits,
        [request.direction]: {
          to: target.id,
          locked: false,
          key: null,
          label: null,
          one_way: request.oneWay,
        },
      },
    };
    if (!request.oneWay && reverse) {
      const exits = { ...target.exits };
      delete exits[opposite];
      changes[target.id] = { ...target, exits };
    }
    replaceRooms(changes);
    selection = locateRoom(source.id);
    roomTab = 'exits';
    focusObjectId = null;
    focusNpcId = null;
    return null;
  }

  function addPaletteItem(kind: PaletteKind, roomId: string): void {
    if (!doc) return;
    const room = allRooms.find((candidate) => candidate.id === roomId);
    if (!room) return;
    const ids = new Set(allIds);
    if (kind === 'npc') {
      const id = uniqueId('new_npc', ids);
      const npc: NPC = {
        id,
        name: 'New NPC',
        description: '',
        dialogue: { greeting: '', default: '', topics: {} },
        wander: false,
        wander_every: 15,
        effects: {},
      };
      replaceRooms({
        [room.id]: { ...room, npcs: [...room.npcs, npc] },
      });
      focusNpcId = id;
      focusObjectId = null;
      roomTab = 'npcs';
    } else {
      const base =
        kind === 'container'
          ? 'new_container'
          : kind === 'readable'
            ? 'new_readable'
            : 'new_item';
      const id = uniqueId(base, ids);
      const object: GameObject = {
        id,
        name:
          kind === 'container'
            ? 'New Container'
            : kind === 'readable'
              ? 'New Readable'
              : 'New Item',
        description: '',
        kind,
        portable: true,
        weight: 0,
        readable_text: kind === 'readable' ? '' : null,
        locked: false,
        required_key: null,
        light_source: false,
        lit: false,
        dropped_on_sleep: false,
        state: {},
        contains: [],
        effects: {},
      };
      replaceRooms({
        [room.id]: { ...room, objects: [...room.objects, object] },
      });
      focusObjectId = id;
      focusNpcId = null;
      roomTab = 'objects';
    }
    selection = locateRoom(room.id);
  }

  function removeRoom(): void {
    if (!doc || !selection) return;
    const section = doc.areas[selection.area];
    if (!section) return;
    if (section.rooms.length <= 1) {
      actionError =
        'An area must keep at least one room. Remove the area in the YAML file instead.';
      return;
    }
    const room = section.rooms[selection.room];
    if (!room) return;
    if (!window.confirm(`Remove room ${room.name || room.id}?`)) return;
    const layout = { ...doc.layout };
    delete layout[room.id];
    doc = {
      ...doc,
      layout,
      areas: doc.areas.map((candidate, index) =>
        index === selection?.area
          ? {
              ...candidate,
              rooms: candidate.rooms.filter(
                (_, roomIndex) => roomIndex !== selection?.room,
              ),
            }
          : candidate,
      ),
    };
    selection = {
      area: selection.area,
      room: Math.max(0, Math.min(selection.room, section.rooms.length - 2)),
    };
    focusObjectId = null;
    focusNpcId = null;
    markDirty();
  }

  function addArea(): void {
    if (!doc) return;
    const slug = newAreaName
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '');
    if (!slug) {
      actionError = 'An area name needs at least one letter or digit.';
      return;
    }
    if (doc.areas.some((area) => area.area === slug)) {
      actionError = `Area ${slug} already exists.`;
      return;
    }
    const file = `areas/${slug}.yaml`;
    if (doc.areas.some((area) => area.file === file)) {
      actionError = `${file} already exists.`;
      return;
    }
    const room = newRoom(slug, new Set(allIds));
    const position = positionForNewRoom(effectiveLayout, allRooms);
    doc = {
      ...doc,
      areas: [
        ...doc.areas,
        { area: slug, file, rooms: [room] },
      ],
      layout: { ...doc.layout, [room.id]: position },
    };
    selection = { area: doc.areas.length - 1, room: 0 };
    roomTab = 'room';
    focusObjectId = null;
    focusNpcId = null;
    newAreaName = '';
    actionError = '';
    markDirty();
  }

  async function validate(): Promise<void> {
    if (!doc) return;
    busy = 'validate';
    actionError = '';
    message = '';
    try {
      const result = await api<{ ok: boolean; errors: string[] }>(
        token,
        'POST',
        '/api/world/validate',
        worldPayload(doc),
      );
      errors = result.errors;
      message = result.ok
        ? 'Validation passed. Nothing was written.'
        : `Validation found ${result.errors.length} problem${
            result.errors.length === 1 ? '' : 's'
          }.`;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      actionError = errorList(caught).join('\n');
    } finally {
      busy = null;
    }
  }

  async function save(): Promise<void> {
    if (!doc) return;
    busy = 'save';
    actionError = '';
    message = '';
    try {
      const result = await api<SaveResult>(
        token,
        'POST',
        '/api/world/save',
        worldPayload(doc),
      );
      lastSaved = result;
      await fetchWorld();
      message = `Saved ${result.rooms} rooms across ${result.areas} area files. Backup: ${result.backup_dir}`;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      if (caught instanceof ApiError && Array.isArray(caught.payload.errors)) {
        errors = caught.payload.errors.map((item) => String(item));
      }
      actionError = errorList(caught).join('\n');
    } finally {
      busy = null;
    }
  }

  function handleApplied(result: ApplyResult): void {
    applyConfirm = null;
    message = `Applied ${result.world.name} at epoch ${result.world.epoch}. ${result.closed} MUD client${
      result.closed === 1 ? '' : 's'
    } closed with 1001 “World reload”; characters stay in SQLite and can log back in.`;
    void onApplied();
  }

  async function apply(force: boolean): Promise<void> {
    if (!doc || dirty) return;
    busy = 'apply';
    actionError = '';
    message = '';
    try {
      const result = await api<ApplyResult>(
        token,
        'POST',
        '/api/world/apply',
        { force },
      );
      handleApplied(result);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      if (caught instanceof ApiError && caught.status === 409) {
        const live = caught.payload.live_players;
        applyConfirm = Array.isArray(live) ? live.length : 1;
      } else {
        actionError = errorList(caught).join('\n');
      }
    } finally {
      busy = null;
    }
  }
</script>

<section class="panel world-panel">
  <header class="panel-head">
    <div>
      <h2>World editor</h2>
      <p class="muted">
        {doc ? `${doc.path} · ${doc.kind}` : 'Loading the configured world…'}
      </p>
    </div>
    <div class="inline-actions">
      {#if dirty}<span class="badge warn">unsaved changes</span>{/if}
      <button
        type="button"
        class="small"
        onclick={validate}
        disabled={!doc || busy !== null}>Validate</button
      >
      <button
        type="button"
        class="small primary"
        onclick={save}
        disabled={!doc || busy !== null}
      >
        {busy === 'save' ? 'Saving…' : 'Save'}
      </button>
      <button
        type="button"
        class="small"
        onclick={() => apply(false)}
        disabled={!doc || dirty || busy !== null}
        title={dirty
          ? 'Save before applying: Apply reloads the world from disk.'
          : 'Reload the live engine from the saved YAML world.'}
      >
        {busy === 'apply' ? 'Applying…' : 'Apply'}
      </button>
    </div>
  </header>

  {#if dirty}
    <p class="fine-print warn-text">
      Apply is disabled while the editor has unsaved changes. Save first; Apply
      always reloads the YAML on disk.
    </p>
  {/if}

  {#if actionError}
    <p class="error-box pre" role="alert">{actionError}</p>
  {/if}
  {#if message}
    <p class="success-box" role="status">{message}</p>
  {/if}
  {#if loadError}
    <p class="error-box" role="alert">{loadError}</p>
  {/if}

  {#if loading && !doc}
    <p class="muted">Reading the world files…</p>
  {:else if !doc}
    <p class="empty">
      The world could not be loaded. Check the configured path and the server
      log.
    </p>
  {:else}
    <div class="world-meta">
      <label>
        World name
        <input
          value={doc.world.name}
          oninput={(event) =>
            updateWorld({ name: event.currentTarget.value })}
        />
      </label>
      <label>
        Start room
        <select
          value={doc.world.start_room}
          onchange={(event) =>
            updateWorld({ start_room: event.currentTarget.value })}
        >
          <option value={doc.world.start_room}>
            {doc.world.start_room || 'choose a room'}
          </option>
          {#each allRooms as room (room.id)}
            {#if room.id !== doc.world.start_room}
              <option value={room.id}>{room.name} ({room.id})</option>
            {/if}
          {/each}
        </select>
      </label>
      <label>
        RNG seed
        <input
          type="number"
          value={doc.world.rng_seed}
          oninput={(event) =>
            updateWorld({ rng_seed: Number(event.currentTarget.value) })}
        />
      </label>
      {#if doc.manifest}
        <details class="manifest">
          <summary>
            {doc.manifest.files.length} source file
            {doc.manifest.files.length === 1 ? '' : 's'} · canonical YAML
          </summary>
          <ul>
            {#each doc.manifest.files as file (file.path)}
              <li>
                <span class="mono">{file.path}</span>
                <span class="muted">{filesize(file.bytes)}</span>
              </li>
            {/each}
          </ul>
        </details>
      {/if}
    </div>

    {#if errors.length > 0}
      <div class="error-box validation-list" role="alert">
        <strong>{errors.length} validation problem{errors.length === 1 ? '' : 's'}</strong>
        <ul>
          {#each errors as error, index (index)}
            <li>{error}</li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if lastSaved}
      <details class="success-box save-details">
        <summary>Last save: {lastSaved.rooms} rooms, {lastSaved.areas} areas</summary>
        <p class="fine-print">Backup directory: <span class="mono">{lastSaved.backup_dir}</span></p>
        <p class="fine-print">Manifest: <span class="mono">{lastSaved.manifest}</span></p>
        <ul>
          {#each lastSaved.saved as file (file)}
            <li class="mono tiny">{file}</li>
          {/each}
        </ul>
      </details>
    {/if}

    <div class="editor-layout global-map-layout">
      <aside class="tree-panel" aria-label="Areas and rooms">
        {#each doc.areas as area, areaIndex (area.file)}
          <div class="area-group">
            <div class="area-head">
              <div>
                <strong>{area.area ?? 'world.yaml'}</strong>
                <span class="mono tiny muted">{area.file}</span>
              </div>
              <button
                type="button"
                class="small"
                onclick={() => addRoom(areaIndex)}
                aria-label={`Add a room to ${area.area ?? area.file}`}>+ room</button
              >
            </div>
            {#each area.rooms as room, roomIndex (room.id)}
              <button
                type="button"
                class={`room-link ${selection?.area === areaIndex && selection?.room === roomIndex ? 'active' : ''}`}
                onclick={() => selectRoom(areaIndex, roomIndex)}
              >
                <span>{room.name || '(unnamed room)'}</span>
                <span class="mono tiny muted">{room.id || 'no id'}</span>
              </button>
            {/each}
          </div>
        {/each}

        <div class="add-area">
          <label for="new-area">Add area</label>
          <div class="inline-actions">
            <input
              id="new-area"
              bind:value={newAreaName}
              placeholder="old_mine"
              class="mono"
            />
            <button type="button" class="small" onclick={addArea}>Add</button>
          </div>
          <p class="fine-print">
            New area files are written under <code>areas/</code>. Area file
            removal stays manual.
          </p>
        </div>
      </aside>

      <div class="map-column">
        <WorldMap
          rooms={allRooms}
          layout={doc.layout}
          startRoom={doc.world.start_room}
          selectedId={selectedRoom?.id ?? null}
          roomErrors={roomErrorCounts}
          {areaChoices}
          defaultAreaIndex={selection?.area ?? 0}
          {suggestedRoomId}
          onselect={selectRoomById}
          onlayout={updateLayout}
          oncreateroom={createRoomFromMap}
          onpalette={addPaletteItem}
          onconnect={connectRooms}
        />

        <div class="room-panel inspector-panel">
          {#if selectedRoom}
            <RoomEditor
              room={selectedRoom}
              rooms={allRooms}
              {areaNames}
              {allIds}
              {objectIds}
              errors={roomErrors}
              bind:tab={roomTab}
              focusObjectId={focusObjectId}
              focusNpcId={focusNpcId}
              onchange={updateSelectedRoom}
              onremove={removeRoom}
            />
          {:else}
            <p class="empty">Select a room from the tree or the map.</p>
          {/if}
        </div>
      </div>
    </div>
  {/if}
</section>

{#if applyConfirm !== null}
  <div class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="apply-title">
      <h2 id="apply-title">Reload the world with players online?</h2>
      <p class="warning-box">
        {applyConfirm} player{applyConfirm === 1 ? ' is' : 's are'} online. Apply
        closes every MUD client with <code>1001 “World reload”</code>. The admin
        console stays up. Characters remain in SQLite; reconnecting and logging
        in restores their room and inventory.
      </p>
      <p class="fine-print">
        Apply loads the last <strong>saved</strong> YAML from disk, not the
        unsaved editor draft.
      </p>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick={() => (applyConfirm = null)}>
          Cancel
        </button>
        <button
          type="button"
          class="danger"
          onclick={() => apply(true)}
          disabled={busy !== null}
        >
          {busy === 'apply' ? 'Applying…' : 'Close clients and apply'}
        </button>
      </div>
    </div>
  </div>
{/if}

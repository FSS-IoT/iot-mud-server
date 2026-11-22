<script lang="ts">
  import EffectsEditor from './EffectsEditor.svelte';
  import ExitsEditor from './ExitsEditor.svelte';
  import GeometryEditor from './GeometryEditor.svelte';
  import NpcsEditor from './NpcsEditor.svelte';
  import ObjectsEditor from './ObjectsEditor.svelte';
  import type {
    Effects,
    ExitTarget,
    GameObject,
    Geometry,
    NPC,
    Room,
    RoomTab,
  } from '../lib/types';

  interface Props {
    room: Room;
    rooms: Room[];
    areaNames: string[];
    allIds: string[];
    objectIds: string[];
    errors: string[];
    tab?: RoomTab;
    focusObjectId?: string | null;
    focusNpcId?: string | null;
    onchange: (room: Room) => void;
    onremove: () => void;
  }

  let {
    room,
    rooms,
    areaNames,
    allIds,
    objectIds,
    errors,
    tab = $bindable<RoomTab>('room'),
    focusObjectId = null,
    focusNpcId = null,
    onchange,
    onremove,
  }: Props = $props();

  const tabs: { id: RoomTab; label: string }[] = [
    { id: 'room', label: 'Room' },
    { id: 'geometry', label: 'Geometry' },
    { id: 'exits', label: 'Exits' },
    { id: 'objects', label: 'Objects' },
    { id: 'npcs', label: 'NPCs' },
    { id: 'effects', label: 'Effects' },
  ];

  function patch(changes: Partial<Room>): void {
    onchange({ ...room, ...changes });
  }

  function updateExits(exits: Record<string, ExitTarget>): void {
    patch({ exits });
  }

  function updateObjects(objects: GameObject[]): void {
    patch({ objects });
  }

  function updateNpcs(npcs: NPC[]): void {
    patch({ npcs });
  }

  function updateEffects(effects: Effects): void {
    patch({ effects });
  }

  function updateGeometry(geometry: Geometry | null): void {
    patch({ geometry });
  }
</script>

<div class="room-editor">
  <div class="room-title">
    <div>
      <h2>{room.name || '(unnamed room)'}</h2>
      <span class="mono tiny muted">{room.id || 'no id'}</span>
    </div>
    <div class="inline-actions">
      <span class={`badge ${room.dark ? 'stone' : 'ok'}`}>
        {room.dark ? 'dark' : 'lit'}
      </span>
      <button type="button" class="small danger-ghost" onclick={onremove}>
        Remove room
      </button>
    </div>
  </div>

  {#if errors.length > 0}
    <div class="error-box" role="alert">
      <strong>This room has validation errors</strong>
      <ul>
        {#each errors as error, index (index)}
          <li>{error}</li>
        {/each}
      </ul>
    </div>
  {/if}

  <nav class="room-tabs" aria-label="Room editor sections">
    {#each tabs as item (item.id)}
      <button
        type="button"
        class={tab === item.id ? 'active' : ''}
        aria-current={tab === item.id ? 'page' : undefined}
        onclick={() => (tab = item.id)}
      >
        {item.label}
        {#if item.id === 'exits'}
          <span class="tab-count">{Object.keys(room.exits).length}</span>
        {:else if item.id === 'objects'}
          <span class="tab-count">{room.objects.length}</span>
        {:else if item.id === 'npcs'}
          <span class="tab-count">{room.npcs.length}</span>
        {/if}
      </button>
    {/each}
  </nav>

  {#if tab === 'room'}
    <section class="editor-section">
      <h3>Room</h3>
      <div class="form-grid">
        <label>
          Id
          <input
            value={room.id}
            class="mono"
            oninput={(event) => patch({ id: event.currentTarget.value })}
          />
        </label>
        <label>
          Name
          <input
            value={room.name}
            oninput={(event) => patch({ name: event.currentTarget.value })}
          />
        </label>
        <label>
          Area
          <input
            list="area-names"
            value={room.area ?? ''}
            oninput={(event) =>
              patch({ area: event.currentTarget.value.trim() || null })}
          />
          <datalist id="area-names">
            {#each areaNames as name (name)}
              <option value={name}></option>
            {/each}
          </datalist>
        </label>
        <label class="check-inline">
          <input
            type="checkbox"
            checked={room.dark}
            onchange={(event) => patch({ dark: event.currentTarget.checked })}
          />
          Dark unless lit
        </label>
        <label class="span-2">
          Description
          <textarea
            rows="3"
            value={room.description}
            oninput={(event) => patch({ description: event.currentTarget.value })}
          ></textarea>
        </label>
      </div>
    </section>
  {:else if tab === 'geometry'}
    <GeometryEditor
      geometry={room.geometry}
      {room}
      onchange={updateGeometry}
    />
  {:else if tab === 'exits'}
    <ExitsEditor
      {room}
      {rooms}
      {objectIds}
      onchange={updateExits}
    />
  {:else if tab === 'objects'}
    {#key `${room.id}:${focusObjectId ?? ''}`}
      <ObjectsEditor
        objects={room.objects}
        {allIds}
        focusId={focusObjectId}
        onchange={updateObjects}
      />
    {/key}
  {:else if tab === 'npcs'}
    {#key `${room.id}:${focusNpcId ?? ''}`}
      <NpcsEditor
        npcs={room.npcs}
        {allIds}
        focusId={focusNpcId}
        onchange={updateNpcs}
      />
    {/key}
  {:else}
    <section class="editor-section">
      <div class="section-head">
        <div>
          <h3>Room effects</h3>
          <p class="muted">
            Triggers such as <code>on_enter</code> publish effects to the bus and
            the event log.
          </p>
        </div>
      </div>
      <EffectsEditor effects={room.effects} onchange={updateEffects} />
    </section>
  {/if}
</div>

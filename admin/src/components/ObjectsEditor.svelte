<script lang="ts">
  import EffectsEditor from './EffectsEditor.svelte';
  import type { Effects, GameObject } from '../lib/types';
  import { uniqueId } from '../lib/types';

  interface Props {
    objects: GameObject[];
    allIds: string[];
    onchange: (objects: GameObject[]) => void;
  }

  let { objects, allIds, onchange }: Props = $props();
  let selectedIndex = $state(0);
  let jsonError = $state('');

  const selected = $derived(
    objects[Math.min(selectedIndex, Math.max(0, objects.length - 1))] ?? null,
  );
  const kindLabel = $derived(selected?.kind ?? 'item');

  function patch(changes: Partial<GameObject>): void {
    onchange(
      objects.map((object, index) =>
        index === selectedIndex ? { ...object, ...changes } : object,
      ),
    );
  }

  function addObject(): void {
    const id = uniqueId('new_object', new Set(allIds));
    const object: GameObject = {
      id,
      name: 'New Object',
      description: '',
      kind: 'item',
      portable: true,
      weight: 0,
      readable_text: null,
      locked: false,
      required_key: null,
      light_source: false,
      lit: false,
      dropped_on_sleep: false,
      state: {},
      contains: [],
      effects: {},
    };
    onchange([...objects, object]);
    selectedIndex = objects.length;
  }

  function removeObject(index: number): void {
    const object = objects[index];
    if (!object) return;
    if (!window.confirm(`Remove object ${object.name || object.id}?`)) return;
    onchange(objects.filter((_, at) => at !== index));
    selectedIndex = Math.max(0, Math.min(selectedIndex, objects.length - 2));
  }

  function updateState(text: string): void {
    try {
      const parsed: unknown = text.trim() ? JSON.parse(text) : {};
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        jsonError = 'State must be a JSON object.';
        return;
      }
      jsonError = '';
      patch({ state: parsed as Record<string, unknown> });
    } catch {
      jsonError = 'State is not valid JSON; the change was not applied.';
    }
  }

  function updateContains(text: string): void {
    patch({
      contains: text
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
    });
  }

  function updateEffects(effects: Effects): void {
    patch({ effects });
  }
</script>

<section class="editor-section">
  <div class="section-head">
    <div>
      <h3>Objects</h3>
      <p class="muted">
        Items, containers, and readables. Object ids share one namespace with
        rooms and NPCs.
      </p>
    </div>
    <button type="button" class="small" onclick={addObject}>Add object</button>
  </div>

  {#if objects.length === 0}
    <p class="empty">This room holds no objects.</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Id</th>
            <th>Name</th>
            <th>Kind</th>
            <th class="numeric">Weight</th>
            <th>Flags</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each objects as object, index (index)}
            <tr class={index === selectedIndex ? 'selected' : ''}>
              <td class="mono tiny">{object.id}</td>
              <td>{object.name}</td>
              <td>{object.kind}</td>
              <td class="numeric">{object.weight}</td>
              <td class="tiny">
                {object.locked ? 'locked ' : ''}{object.light_source
                  ? 'light '
                  : ''}{object.dropped_on_sleep ? 'drops ' : ''}
              </td>
              <td class="row-actions">
                <button
                  type="button"
                  class="small"
                  onclick={() => (selectedIndex = index)}>Edit</button
                >
                <button
                  type="button"
                  class="small danger-ghost"
                  onclick={() => removeObject(index)}
                  aria-label={`Remove object ${object.name || object.id}`}>×</button
                >
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    {#if selected}
      <div class="detail-card">
        <div class="detail-head">
          <h4>Editing {selected.name || selected.id}</h4>
          <span class="mono tiny muted">{selected.id}</span>
        </div>
        {#if jsonError}
          <p class="error-box" role="alert">{jsonError}</p>
        {/if}
        <div class="form-grid">
          <label>
            Id
            <input
              value={selected.id}
              class="mono"
              oninput={(event) => patch({ id: event.currentTarget.value })}
            />
          </label>
          <label>
            Name
            <input
              value={selected.name}
              oninput={(event) => patch({ name: event.currentTarget.value })}
            />
          </label>
          <label>
            Kind
            <select
              value={selected.kind}
              onchange={(event) =>
                patch({
                  kind: event.currentTarget.value as GameObject['kind'],
                })}
            >
              <option value="item">item</option>
              <option value="container">container</option>
              <option value="readable">readable</option>
            </select>
          </label>
          <label>
            Weight
            <input
              type="number"
              min="0"
              step="0.01"
              value={selected.weight}
              oninput={(event) =>
                patch({ weight: Number(event.currentTarget.value) })}
            />
          </label>
          <label class="span-2">
            Description
            <textarea
              rows="2"
              value={selected.description}
              oninput={(event) =>
                patch({ description: event.currentTarget.value })}
            ></textarea>
          </label>
          {#if kindLabel === 'readable'}
            <label class="span-2">
              Readable text
              <textarea
                rows="3"
                value={selected.readable_text ?? ''}
                oninput={(event) =>
                  patch({
                    readable_text: event.currentTarget.value || null,
                  })}
              ></textarea>
            </label>
          {/if}
        </div>

        <div class="check-row">
          <label><input
            type="checkbox"
            checked={selected.portable}
            onchange={(event) =>
              patch({ portable: event.currentTarget.checked })}
          /> Portable</label>
          <label><input
            type="checkbox"
            checked={selected.light_source}
            onchange={(event) =>
              patch({ light_source: event.currentTarget.checked })}
          /> Light source</label>
          <label><input
            type="checkbox"
            checked={selected.lit}
            onchange={(event) => patch({ lit: event.currentTarget.checked })}
          /> Lit</label>
          <label><input
            type="checkbox"
            checked={selected.dropped_on_sleep}
            onchange={(event) =>
              patch({ dropped_on_sleep: event.currentTarget.checked })}
          /> Drops on sleep</label>
          <label><input
            type="checkbox"
            checked={selected.locked}
            onchange={(event) => patch({ locked: event.currentTarget.checked })}
          /> Locked</label>
        </div>

        {#if selected.locked}
          <div class="form-grid">
            <label>
              Required key object id
              <input
                list="world-object-ids"
                value={selected.required_key ?? ''}
                oninput={(event) =>
                  patch({
                    required_key: event.currentTarget.value.trim() || null,
                  })}
              />
            </label>
          </div>
        {/if}

        {#if selected.kind === 'container'}
          <label class="block-label">
            Contains (comma-separated object ids)
            <input
              value={selected.contains.join(', ')}
              class="mono"
              onchange={(event) => updateContains(event.currentTarget.value)}
            />
          </label>
        {/if}

        {#key selected.id}
          <label class="block-label">
            State (JSON object; container lid at <code>open</code>)
            <textarea
              rows="3"
              class="mono"
              spellcheck="false"
              value={JSON.stringify(selected.state, null, 2)}
              onchange={(event) => updateState(event.currentTarget.value)}
            ></textarea>
          </label>
        {/key}

        <h4>Object effects</h4>
        <EffectsEditor effects={selected.effects} onchange={updateEffects} />
      </div>
    {/if}
  {/if}
</section>

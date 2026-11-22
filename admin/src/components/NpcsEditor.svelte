<script lang="ts">
  import EffectsEditor from './EffectsEditor.svelte';
  import type { Effects, NPC } from '../lib/types';
  import { uniqueId } from '../lib/types';

  interface Props {
    npcs: NPC[];
    allIds: string[];
    focusId?: string | null;
    onchange: (npcs: NPC[]) => void;
  }

  let { npcs, allIds, focusId = null, onchange }: Props = $props();
  let selectedId = $state<string | null>(null);

  const activeId = $derived(selectedId ?? focusId);
  const selectedIndex = $derived.by(() => {
    const index = npcs.findIndex((npc) => npc.id === activeId);
    return index >= 0 ? index : 0;
  });
  const selected = $derived(npcs[selectedIndex] ?? null);

  function patch(changes: Partial<NPC>): void {
    onchange(
      npcs.map((npc, index) =>
        index === selectedIndex ? { ...npc, ...changes } : npc,
      ),
    );
  }

  function patchDialogue(changes: Partial<NPC['dialogue']>): void {
    if (!selected) return;
    patch({ dialogue: { ...selected.dialogue, ...changes } });
  }

  function addNpc(): void {
    const id = uniqueId('new_npc', new Set(allIds));
    const npc: NPC = {
      id,
      name: 'New NPC',
      description: '',
      dialogue: { greeting: '', default: '', topics: {} },
      wander: false,
      wander_every: 15,
      effects: {},
    };
    onchange([...npcs, npc]);
    selectedId = id;
  }

  function removeNpc(index: number): void {
    const npc = npcs[index];
    if (!npc) return;
    if (!window.confirm(`Remove NPC ${npc.name || npc.id}?`)) return;
    const remaining = npcs.filter((_, at) => at !== index);
    onchange(remaining);
    selectedId = remaining[Math.min(index, remaining.length - 1)]?.id ?? null;
  }

  function updateTopicKey(oldKey: string, newKey: string): void {
    if (!selected) return;
    const key = newKey.trim();
    if (!key || key === oldKey) return;
    const topics: Record<string, string> = {};
    for (const [name, text] of Object.entries(selected.dialogue.topics)) {
      topics[name === oldKey ? key : name] = text;
    }
    patchDialogue({ topics });
  }

  function updateTopicText(key: string, text: string): void {
    if (!selected) return;
    patchDialogue({
      topics: { ...selected.dialogue.topics, [key]: text },
    });
  }

  function addTopic(): void {
    if (!selected) return;
    let key = 'topic';
    let counter = 2;
    while (key in selected.dialogue.topics) {
      key = `topic_${counter}`;
      counter += 1;
    }
    patchDialogue({
      topics: { ...selected.dialogue.topics, [key]: '' },
    });
  }

  function removeTopic(key: string): void {
    if (!selected) return;
    const topics = { ...selected.dialogue.topics };
    delete topics[key];
    patchDialogue({ topics });
  }

  function updateEffects(effects: Effects): void {
    patch({ effects });
  }
</script>

<section class="editor-section">
  <div class="section-head">
    <div>
      <h3>NPCs</h3>
      <p class="muted">
        Scripted dialogue for <code>talk</code> and
        <code>ask &lt;npc&gt; about &lt;topic&gt;</code>. Wandering is
        deterministic and seeded by the world.
      </p>
    </div>
    <button type="button" class="small" onclick={addNpc}>Add NPC</button>
  </div>

  {#if npcs.length === 0}
    <p class="empty">No NPCs live in this room.</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Id</th>
            <th>Name</th>
            <th>Wander</th>
            <th class="numeric">Topic count</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each npcs as npc, index (index)}
            <tr class={index === selectedIndex ? 'selected' : ''}>
              <td class="mono tiny">{npc.id}</td>
              <td>{npc.name}</td>
              <td>{npc.wander ? `every ${npc.wander_every}s` : 'no'}</td>
              <td class="numeric">{Object.keys(npc.dialogue.topics).length}</td>
              <td class="row-actions">
                <button
                  type="button"
                  class="small"
                  onclick={() => (selectedId = npc.id)}>Edit</button
                >
                <button
                  type="button"
                  class="small danger-ghost"
                  onclick={() => removeNpc(index)}
                  aria-label={`Remove NPC ${npc.name || npc.id}`}>×</button
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
          <label class="span-2">
            Description
            <textarea
              rows="2"
              value={selected.description}
              oninput={(event) =>
                patch({ description: event.currentTarget.value })}
            ></textarea>
          </label>
          <label class="span-2">
            Greeting
            <input
              value={selected.dialogue.greeting}
              oninput={(event) =>
                patchDialogue({ greeting: event.currentTarget.value })}
            />
          </label>
          <label class="span-2">
            Default reply
            <input
              value={selected.dialogue.default}
              oninput={(event) =>
                patchDialogue({ default: event.currentTarget.value })}
            />
          </label>
          <label>
            Wander every (seconds)
            <input
              type="number"
              min="1"
              value={selected.wander_every}
              oninput={(event) =>
                patch({ wander_every: Number(event.currentTarget.value) })}
            />
          </label>
          <label class="check-inline">
            <input
              type="checkbox"
              checked={selected.wander}
              onchange={(event) =>
                patch({ wander: event.currentTarget.checked })}
            />
            Wanders on the world tick
          </label>
        </div>

        <div class="section-head small-head">
          <h4>Topics</h4>
          <button type="button" class="small" onclick={addTopic}>Add topic</button>
        </div>
        {#if Object.keys(selected.dialogue.topics).length === 0}
          <p class="fine-print">No topics; the default reply answers everything.</p>
        {:else}
          <div class="table-wrap compact">
            <table>
              <thead><tr><th>Topic</th><th>Reply</th><th></th></tr></thead>
              <tbody>
                {#each Object.entries(selected.dialogue.topics) as [topic, reply] (topic)}
                  <tr>
                    <td>
                      <input
                        value={topic}
                        class="mono"
                        aria-label={`Topic ${topic}`}
                        onchange={(event) =>
                          updateTopicKey(topic, event.currentTarget.value)}
                      />
                    </td>
                    <td>
                      <input
                        value={reply}
                        aria-label={`Reply for ${topic}`}
                        oninput={(event) =>
                          updateTopicText(topic, event.currentTarget.value)}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        class="small danger-ghost"
                        onclick={() => removeTopic(topic)}
                        aria-label={`Remove topic ${topic}`}>×</button
                      >
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}

        <h4>NPC effects</h4>
        <EffectsEditor effects={selected.effects} onchange={updateEffects} />
      </div>
    {/if}
  {/if}
</section>

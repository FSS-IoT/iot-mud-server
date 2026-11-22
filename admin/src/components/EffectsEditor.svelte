<script lang="ts">
  import { EFFECT_TRIGGERS } from '../lib/types';
  import type { EffectEntry, Effects } from '../lib/types';

  interface Props {
    effects: Effects;
    onchange: (effects: Effects) => void;
  }

  let { effects, onchange }: Props = $props();
  let chosenTrigger = $state('');
  let paramError = $state('');

  const availableTriggers = $derived(
    EFFECT_TRIGGERS.filter((trigger) => !(trigger in effects)),
  );

  function addTrigger(): void {
    const trigger = chosenTrigger || availableTriggers[0];
    if (!trigger) return;
    onchange({ ...effects, [trigger]: [] });
    chosenTrigger = '';
  }

  function removeTrigger(trigger: string): void {
    const next = { ...effects };
    delete next[trigger];
    onchange(next);
  }

  function addEffect(trigger: string): void {
    const entry: EffectEntry = { type: 'sound.play', params: {} };
    onchange({
      ...effects,
      [trigger]: [...(effects[trigger] ?? []), entry],
    });
  }

  function updateType(trigger: string, index: number, type: string): void {
    const entries = [...(effects[trigger] ?? [])];
    const current = entries[index];
    if (!current) return;
    entries[index] = { ...current, type };
    onchange({ ...effects, [trigger]: entries });
  }

  function updateParams(trigger: string, index: number, text: string): void {
    let parsed: unknown;
    try {
      parsed = text.trim() ? JSON.parse(text) : {};
    } catch {
      paramError = `${trigger}[${index}] params are not valid JSON; the change was not applied.`;
      return;
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      paramError = `${trigger}[${index}] params must be a JSON object.`;
      return;
    }
    paramError = '';
    const entries = [...(effects[trigger] ?? [])];
    const current = entries[index];
    if (!current) return;
    entries[index] = { ...current, params: parsed as Record<string, unknown> };
    onchange({ ...effects, [trigger]: entries });
  }

  function removeEffect(trigger: string, index: number): void {
    const entries = (effects[trigger] ?? []).filter((_, at) => at !== index);
    onchange({ ...effects, [trigger]: entries });
  }
</script>

<div class="effects-editor">
  {#if paramError}
    <p class="error-box" role="alert">{paramError}</p>
  {/if}

  {#each Object.entries(effects) as [trigger, entries] (trigger)}
    <div class="effect-group">
      <div class="effect-head">
        <h4>{trigger}</h4>
        <div class="inline-actions">
          <button type="button" class="small" onclick={() => addEffect(trigger)}>
            Add effect
          </button>
          <button
            type="button"
            class="small danger-ghost"
            onclick={() => removeTrigger(trigger)}>Remove trigger</button
          >
        </div>
      </div>
      {#if entries.length === 0}
        <p class="fine-print">No effects on this trigger yet.</p>
      {/if}
      {#each entries as entry, index (index)}
        <div class="effect-row">
          <label>
            Type
            <input
              value={entry.type}
              placeholder="sound.play"
              oninput={(event) =>
                updateType(trigger, index, event.currentTarget.value)}
            />
          </label>
          <label class="grow">
            Params (JSON object)
            <textarea
              rows="2"
              value={JSON.stringify(entry.params)}
              spellcheck="false"
              onchange={(event) =>
                updateParams(trigger, index, event.currentTarget.value)}
            ></textarea>
          </label>
          <button
            type="button"
            class="small danger-ghost"
            aria-label={`Remove ${trigger} effect ${index + 1}`}
            onclick={() => removeEffect(trigger, index)}>×</button
          >
        </div>
      {/each}
    </div>
  {/each}

  {#if availableTriggers.length > 0}
    <div class="inline-actions">
      <label class="inline-label">
        Add trigger
        <select bind:value={chosenTrigger} aria-label="Effect trigger to add">
          <option value="">{availableTriggers[0]}</option>
          {#each availableTriggers as trigger (trigger)}
            <option value={trigger}>{trigger}</option>
          {/each}
        </select>
      </label>
      <button type="button" class="small" onclick={addTrigger}>Add</button>
    </div>
  {/if}
  <p class="fine-print">
    Params are applied when the field loses focus. Effect types are dotted names
    such as <code>light.glow</code>; the engine's validator checks the shape.
  </p>
</div>

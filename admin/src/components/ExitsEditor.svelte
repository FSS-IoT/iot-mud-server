<script lang="ts">
  import { EXIT_DIRECTIONS } from '../lib/types';
  import type { ExitTarget, Room } from '../lib/types';

  interface Props {
    room: Room;
    rooms: Room[];
    objectIds: string[];
    onchange: (exits: Record<string, ExitTarget>) => void;
  }

  let { room, rooms, objectIds, onchange }: Props = $props();

  const directions = $derived(
    EXIT_DIRECTIONS.filter((direction) => !(direction in room.exits)),
  );

  function update(
    direction: string,
    patch: Partial<ExitTarget>,
    newDirection = direction,
  ): void {
    const current = room.exits[direction];
    if (!current) return;
    const next: Record<string, ExitTarget> = {};
    for (const [name, exit] of Object.entries(room.exits)) {
      if (name === direction) next[newDirection] = { ...exit, ...patch };
      else next[name] = exit;
    }
    onchange(next);
  }

  function addExit(): void {
    const direction = directions[0];
    if (!direction) return;
    const target = rooms.find((candidate) => candidate.id !== room.id)?.id ?? '';
    onchange({
      ...room.exits,
      [direction]: {
        to: target,
        locked: false,
        key: null,
        label: null,
        one_way: false,
      },
    });
  }

  function removeExit(direction: string): void {
    const next = { ...room.exits };
    delete next[direction];
    onchange(next);
  }
</script>

<section class="editor-section">
  <div class="section-head">
    <div>
      <h3>Exits</h3>
      <p class="muted">
        Declare each way once. The loader creates the reciprocal unless
        <code>one_way</code> is set; declaring both directions is a validation
        error.
      </p>
    </div>
    <button
      type="button"
      class="small"
      onclick={addExit}
      disabled={directions.length === 0}>Add exit</button
    >
  </div>

  {#if Object.keys(room.exits).length === 0}
    <p class="empty">No exits. Add one to connect this room.</p>
  {:else}
    <div class="table-wrap">
      <table class="exits-table">
        <thead>
          <tr>
            <th>Direction</th>
            <th>Target room</th>
            <th>Locked</th>
            <th>Key object id</th>
            <th>Lock label</th>
            <th>One way</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each Object.entries(room.exits) as [direction, exit] (direction)}
            <tr>
              <td>
                <select
                  value={direction}
                  aria-label={`Direction for the ${direction} exit`}
                  onchange={(event) =>
                    update(direction, {}, event.currentTarget.value)}
                >
                  {#each EXIT_DIRECTIONS as name (name)}
                    <option value={name}>{name}</option>
                  {/each}
                </select>
              </td>
              <td>
                <select
                  value={exit.to}
                  aria-label={`Target room for the ${direction} exit`}
                  onchange={(event) =>
                    update(direction, { to: event.currentTarget.value })}
                >
                  <option value="">choose a room</option>
                  {#each rooms as candidate (candidate.id)}
                    <option value={candidate.id}>{candidate.name} ({candidate.id})</option>
                  {/each}
                </select>
              </td>
              <td class="center">
                <input
                  type="checkbox"
                  checked={exit.locked}
                  aria-label={`${direction} exit is locked`}
                  onchange={(event) =>
                    update(direction, { locked: event.currentTarget.checked })}
                />
              </td>
              <td>
                <input
                  list="world-object-ids"
                  value={exit.key ?? ''}
                  placeholder="key_gold_1"
                  aria-label={`Key object id for the ${direction} exit`}
                  disabled={!exit.locked}
                  oninput={(event) =>
                    update(direction, {
                      key: event.currentTarget.value.trim() || null,
                    })}
                />
              </td>
              <td>
                <input
                  value={exit.label ?? ''}
                  placeholder="tavern door"
                  aria-label={`Lock label for the ${direction} exit`}
                  disabled={!exit.locked}
                  oninput={(event) =>
                    update(direction, {
                      label: event.currentTarget.value.trim() || null,
                    })}
                />
              </td>
              <td class="center">
                <input
                  type="checkbox"
                  checked={exit.one_way}
                  aria-label={`${direction} exit is one way`}
                  onchange={(event) =>
                    update(direction, { one_way: event.currentTarget.checked })}
                />
              </td>
              <td>
                <button
                  type="button"
                  class="small danger-ghost"
                  onclick={() => removeExit(direction)}
                  aria-label={`Remove the ${direction} exit`}>×</button
                >
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <datalist id="world-object-ids">
      {#each objectIds as id (id)}
        <option value={id}></option>
      {/each}
    </datalist>
  {/if}
</section>

<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError, errorText } from '../lib/api';
  import type { PlayersResponse, RekeyResult } from '../lib/types';
  import { formatWhen } from '../lib/types';

  interface Props {
    token: string;
    onunauthorized: () => void;
  }

  let { token, onunauthorized }: Props = $props();
  let data = $state<PlayersResponse | null>(null);
  let loading = $state(false);
  let error = $state('');
  let rekeyTarget = $state<string | null>(null);
  let rekeyBusy = $state(false);
  let rekeyError = $state('');
  let newKey = $state<RekeyResult | null>(null);
  let copyState = $state('');

  onMount(() => {
    void load();
  });

  async function load(): Promise<void> {
    loading = true;
    error = '';
    try {
      data = await api<PlayersResponse>(token, 'GET', '/api/players');
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onunauthorized();
        return;
      }
      error = errorText(caught);
    } finally {
      loading = false;
    }
  }

  function askRekey(name: string): void {
    rekeyTarget = name;
    rekeyError = '';
    newKey = null;
    copyState = '';
  }

  async function confirmRekey(): Promise<void> {
    if (!rekeyTarget) return;
    rekeyBusy = true;
    rekeyError = '';
    try {
      const result = await api<RekeyResult>(
        token,
        'POST',
        `/api/players/${encodeURIComponent(rekeyTarget)}/rekey`,
        {},
      );
      newKey = result;
      rekeyTarget = null;
      await load();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onunauthorized();
        return;
      }
      rekeyError = errorText(caught);
    } finally {
      rekeyBusy = false;
    }
  }

  async function copyKey(): Promise<void> {
    if (!newKey) return;
    try {
      await navigator.clipboard.writeText(newKey.key);
      copyState = 'Copied to the clipboard.';
    } catch {
      copyState = 'Copy failed; select the key text and copy it manually.';
    }
  }

  function dismissKey(): void {
    newKey = null;
    copyState = '';
  }
</script>

<section class="panel">
  <header class="panel-head">
    <div>
      <h2>Players</h2>
      <p class="muted">
        The SQLite roster overlaid with live engine state. Rekey issues a new
        character key exactly once.
      </p>
    </div>
    <button type="button" class="ghost" onclick={load} disabled={loading}>
      {loading ? 'Refreshing…' : 'Refresh'}
    </button>
  </header>

  {#if error}
    <p class="error-box" role="alert">{error}</p>
  {/if}

  {#if data === null && loading}
    <p class="muted">Loading the roster…</p>
  {:else if data && data.players.length === 0}
    <p class="empty">No characters have registered yet.</p>
  {:else if data}
    <p class="muted">
      {data.players.length} character{data.players.length === 1 ? '' : 's'} ·
      {data.live_players} live · {data.live_sessions} socket{data.live_sessions ===
      1
        ? ''
        : 's'}
    </p>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Room</th>
            <th>State</th>
            <th>Last seen</th>
            <th class="numeric">Keys</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each data.players as player (player.name)}
            <tr>
              <td><strong>{player.name}</strong></td>
              <td>
                {#if player.room_name}
                  <span>{player.room_name}</span>
                  <span class="mono tiny muted">{player.room_id}</span>
                {:else}
                  <span class="muted">{player.room_id ?? '—'}</span>
                {/if}
              </td>
              <td>
                {#if player.online && !player.asleep}
                  <span class="badge ok">awake</span>
                {:else if player.asleep}
                  <span class="badge stone">stone</span>
                {:else}
                  <span class="badge">offline</span>
                {/if}
              </td>
              <td class="mono tiny">{formatWhen(player.last_seen)}</td>
              <td class="numeric">{player.key_count}</td>
              <td class="row-actions">
                <button
                  type="button"
                  class="small"
                  onclick={() => askRekey(player.name)}>Rekey</button
                >
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

{#if rekeyTarget}
  <div class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="rekey-title">
      <h2 id="rekey-title">Rekey {rekeyTarget}?</h2>
      <p>
        Every current key for <strong>{rekeyTarget}</strong> stops working and
        a new key is issued. The character's room and inventory do not change.
        An already-authenticated live session stays connected until it leaves.
      </p>
      {#if rekeyError}
        <p class="error-box" role="alert">{rekeyError}</p>
      {/if}
      <div class="modal-actions">
        <button
          type="button"
          class="ghost"
          onclick={() => (rekeyTarget = null)}
          disabled={rekeyBusy}>Cancel</button
        >
        <button
          type="button"
          class="danger"
          onclick={confirmRekey}
          disabled={rekeyBusy}
        >
          {rekeyBusy ? 'Rekeying…' : 'Issue a new key'}
        </button>
      </div>
    </div>
  </div>
{/if}

{#if newKey}
  <div class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="new-key-title">
      <h2 id="new-key-title">New key for {newKey.name}</h2>
      <p class="warning-box">
        This key is shown once. It is not stored in plaintext and the page
        cannot show it again. Copy it now and give it to the character's owner.
      </p>
      <div class="key-line">
        <input class="mono" readonly value={newKey.key} aria-label="New character key" />
        <button type="button" class="primary" onclick={copyKey}>Copy</button>
      </div>
      {#if copyState}
        <p class="fine-print" aria-live="polite">{copyState}</p>
      {/if}
      <p class="fine-print">
        Revoked {newKey.revoked} previous key{newKey.revoked === 1 ? '' : 's'}.
        The old key now fails login. This response is the only copy.
      </p>
      <div class="modal-actions">
        <button type="button" class="ghost" onclick={dismissKey}>
          I saved the key
        </button>
      </div>
    </div>
  </div>
{/if}

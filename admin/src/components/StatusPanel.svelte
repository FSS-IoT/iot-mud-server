<script lang="ts">
  import type { Status } from '../lib/types';
  import { shortCommit } from '../lib/types';

  interface Props {
    status: Status | null;
    error?: string;
    onrefresh: () => void | Promise<void>;
  }

  let { status, error = '', onrefresh }: Props = $props();
</script>

<section class="panel">
  <header class="panel-head">
    <div>
      <h2>Status</h2>
      <p class="muted">Live process and durable database in one view.</p>
    </div>
    <button type="button" class="ghost" onclick={onrefresh}>Refresh</button>
  </header>

  {#if error}
    <p class="error-box" role="alert">{error}</p>
  {/if}

  {#if status === null}
    <p class="muted">Loading status…</p>
  {:else}
    <div class="stat-grid">
      <div class="stat">
        <span class="stat-label">Commit</span>
        <strong title={status.commit}>{shortCommit(status.commit)}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">World</span>
        <strong>{status.world.name}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">Epoch</span>
        <strong class="mono small">{status.world.epoch}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">Rooms</span>
        <strong>{status.rooms}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">Characters</span>
        <strong>{status.players}</strong>
      </div>
      <div class="stat">
        <span class="stat-label">Live sessions</span>
        <strong>{status.live_sessions}</strong>
      </div>
    </div>

    <div class="two-col">
      <div class="card">
        <h3>Paths</h3>
        <dl class="facts">
          <dt>Database</dt>
          <dd class="mono">{status.db_path ?? 'none'}</dd>
          <dt>World</dt>
          <dd class="mono">{status.world_path}</dd>
        </dl>
        <p class="fine-print">
          Save writes canonical YAML and keeps a timestamped backup. Apply
          reloads this path; it does not reseed SQLite.
        </p>
      </div>
      <div class="card">
        <h3>Features and limits</h3>
        <dl class="facts">
          {#each Object.entries(status.features) as [name, value] (name)}
            <dt>{name}</dt>
            <dd>{value ? 'on' : 'off'}</dd>
          {/each}
          {#each Object.entries(status.limits) as [name, value] (name)}
            <dt>{name.replaceAll('_', ' ')}</dt>
            <dd>{value}</dd>
          {/each}
          {#each Object.entries(status.auth_limits) as [name, value] (name)}
            <dt>auth {name.replaceAll('_', ' ')}</dt>
            <dd>{value}</dd>
          {/each}
        </dl>
      </div>
    </div>

    <p class="fine-print">
      No seed, reset, or backup buttons live here. Destructive database
      operations stay in the offline <code>python -m src.mud.admin</code> CLI.
    </p>
  {/if}
</section>

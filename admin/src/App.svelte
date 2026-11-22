<script lang="ts">
  import { onMount } from 'svelte';
  import Login from './components/Login.svelte';
  import PlayersPanel from './components/PlayersPanel.svelte';
  import StatusPanel from './components/StatusPanel.svelte';
  import WorldEditor from './components/WorldEditor.svelte';
  import { api, errorText, TOKEN_STORAGE_KEY } from './lib/api';
  import type { Status } from './lib/types';

  type Tab = 'status' | 'players' | 'world';

  let token = $state('');
  let authenticated = $state(false);
  let connecting = $state(false);
  let loginError = $state('');
  let tab = $state<Tab>('status');
  let status = $state<Status | null>(null);
  let statusError = $state('');
  let worldDirty = $state(false);

  onMount(() => {
    const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored) void connect(stored, true);
  });

  async function connect(candidate: string, fromSession = false): Promise<void> {
    const value = candidate.trim();
    if (!value) return;
    connecting = true;
    loginError = '';
    try {
      const snapshot = await api<Status>(value, 'GET', '/api/status');
      token = value;
      status = snapshot;
      statusError = '';
      authenticated = true;
      sessionStorage.setItem(TOKEN_STORAGE_KEY, value);
    } catch (error) {
      if (!fromSession) loginError = errorText(error);
      else loginError = 'The saved token was rejected. Enter it again.';
      authenticated = false;
      status = null;
      token = '';
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } finally {
      connecting = false;
    }
  }

  async function refreshStatus(): Promise<void> {
    if (!token) return;
    statusError = '';
    try {
      status = await api<Status>(token, 'GET', '/api/status');
    } catch (error) {
      statusError = errorText(error);
    }
  }

  function logout(): void {
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    token = '';
    authenticated = false;
    status = null;
    worldDirty = false;
    loginError = '';
    tab = 'status';
  }

  function unauthorized(): void {
    logout();
    loginError = 'The admin token is no longer accepted.';
  }
</script>

{#if !authenticated}
  <Login onconnect={connect} error={loginError} busy={connecting} />
{:else}
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark">MUD</span>
      <div>
        <strong>Admin console</strong>
        <span class="experimental">experimental · held branch</span>
      </div>
    </div>
    <nav class="tabs" aria-label="Admin sections">
      <button
        type="button"
        class={tab === 'status' ? 'active' : ''}
        onclick={() => (tab = 'status')}>Status</button
      >
      <button
        type="button"
        class={tab === 'players' ? 'active' : ''}
        onclick={() => (tab = 'players')}>Players</button
      >
      <button
        type="button"
        class={tab === 'world' ? 'active' : ''}
        onclick={() => (tab = 'world')}
      >
        World
        {#if worldDirty}<span class="unsaved-dot" title="Unsaved changes"></span
          >{/if}
      </button>
    </nav>
    <div class="topbar-actions">
      <button type="button" class="ghost" onclick={refreshStatus}>Refresh</button>
      <button type="button" class="ghost" onclick={logout}>Lock</button>
    </div>
  </header>

  <main>
    <div hidden={tab !== 'status'}>
      <StatusPanel {status} error={statusError} onrefresh={refreshStatus} />
    </div>
    <div hidden={tab !== 'players'}>
      <PlayersPanel {token} onunauthorized={unauthorized} />
    </div>
    <div hidden={tab !== 'world'}>
      <WorldEditor
        {token}
        onDirty={(dirty) => (worldDirty = dirty)}
        onUnauthorized={unauthorized}
        onApplied={refreshStatus}
      />
    </div>
  </main>
{/if}

<script lang="ts">
  interface Props {
    onconnect: (token: string) => void | Promise<void>;
    error?: string;
    busy?: boolean;
  }

  let { onconnect, error = '', busy = false }: Props = $props();
  let value = $state('');

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    const candidate = value.trim();
    if (candidate && !busy) void onconnect(candidate);
  }
</script>

<div class="login-shell">
  <form class="login-card" onsubmit={submit}>
    <div class="login-brand">
      <span class="brand-mark large">MUD</span>
      <span class="experimental">experimental · held branch</span>
    </div>
    <h1>Admin console</h1>
    <p class="muted">
      Server status, the character roster, and the YAML world editor. The token
      stays in this tab's <code>sessionStorage</code> and travels only in the
      <code>Authorization</code> header.
    </p>
    <label for="admin-token">Admin token</label>
    <input
      id="admin-token"
      type="password"
      autocomplete="off"
      spellcheck="false"
      bind:value
      disabled={busy}
      placeholder="MUD_ADMIN_TOKEN"
    />
    {#if error}
      <p class="error-box" role="alert" aria-live="polite">{error}</p>
    {/if}
    <button class="primary wide" type="submit" disabled={busy || !value.trim()}>
      {busy ? 'Checking…' : 'Connect'}
    </button>
    <p class="fine-print">
      This page is public only in the sense that it is static. Every
      <code>/api/*</code> request needs the bearer token. The admin surface is
      not a public service.
    </p>
  </form>
</div>

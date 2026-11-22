import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  // The API is same-origin and has no CORS. In dev, Vite proxies /api to the
  // local admin listener so the browser still sees one origin.
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8766',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});

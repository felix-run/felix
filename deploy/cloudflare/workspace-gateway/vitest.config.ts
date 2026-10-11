import { defineConfig } from 'vitest/config';

// The handler, and the Durable Object's ordering with `cloudflare:workers` stubbed (Node cannot load
// it). What the Durable Object runs is the helper, tested in the harness against the real filesystem.
export default defineConfig({
  resolve: { alias: { 'cloudflare:workers': new URL('./tests/stubs/workers.ts', import.meta.url).pathname } },
  test: { include: ['tests/**/*.test.ts'], environment: 'node' },
});

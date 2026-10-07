import { defineConfig } from 'vitest/config';

// The handler only: the Durable Object imports `cloudflare:workers`, which Node cannot load, and
// what it runs is the helper, tested on its own by `python3 -m unittest`.
export default defineConfig({
  test: { include: ['tests/**/*.test.ts'], environment: 'node' },
});

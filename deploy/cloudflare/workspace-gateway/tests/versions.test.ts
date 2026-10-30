import { describe, expect, it } from 'vitest';
import dockerfile from '../Dockerfile?raw';
import pkg from '../package.json';

describe('the sandbox image', () => {
  it('copies the sandbox-shim built for the @cloudflare/sandbox version the Worker runs', () => {
    // `DirectoryBackup` drives the shim inside the Container, and the two speak one protocol
    // version. Dependabot bumps the npm package and the image tag in separate pull requests.
    // The tag, ahead of the `@sha256:` digest the image is pinned by.
    const shim =
      /cloudflare\/sandbox:([^\s/@]+)(?:@sha256:[0-9a-f]{64})?\s+\/usr\/local\/bin\/sandbox-shim/.exec(dockerfile);
    expect(shim?.[1]).toBe(pkg.dependencies['@cloudflare/sandbox']);
  });
});

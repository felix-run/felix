/**
 * The gateway's HTTP surface, run against a fake Durable Object namespace, so it needs no
 * container. What a real container does is the helper's (`tests/unit/test_workspace_gateway_helper.py`
 * in the harness suite) and a live run's.
 *
 * What the harness's hosted backend relies on, stated as a contract rather than as what the
 * Worker happens to do:
 *   - nothing reaches a sandbox without the bearer, and a gateway with no token serves nobody
 *   - the sandbox is named from the scope alone, and a malformed scope names none
 *   - each operation reaches the scope's sandbox with its defaults filled and its bounds checked
 *   - the helper's error code comes back as `{error, message}`, which is what the backend maps
 */
import { beforeEach, describe, expect, it } from 'vitest';
import handler from '../src/handler';

interface Stub {
  operate(scope: string, request: unknown): Promise<unknown>;
}

interface GatewayEnv {
  SANDBOX: { getByName(name: string): Stub };
  WORKSPACE_GATEWAY_TOKEN?: string;
}

interface WorkspaceGatewayWorker {
  fetch(req: Request, env: GatewayEnv): Promise<Response>;
}

const TOKEN = 't'.repeat(40);
const THREAD = 'a'.repeat(40);
const BASE = 'https://gateway.example.com';

interface Call {
  name: string;
  scope: string;
  request: unknown;
}

// The handler must assign to this with no cast: the only type-level link to the fake below.
const worker: WorkspaceGatewayWorker = handler;

let calls: Call[];
let answer: () => Promise<unknown>;

const env = (over: Partial<GatewayEnv> = {}): GatewayEnv => ({
  SANDBOX: {
    getByName: (name) => ({
      operate: async (scope, request) => {
        calls.push({ name, scope, request });
        return answer();
      },
    }),
  },
  WORKSPACE_GATEWAY_TOKEN: TOKEN,
  ...over,
});

const post = (path: string, body?: unknown, headers: Record<string, string> = {}): Request =>
  new Request(`${BASE}${path}`, {
    method: 'POST',
    headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

const send = (req: Request, over?: Partial<GatewayEnv>) => worker.fetch(req, env(over));

describe('workspace gateway', () => {
  beforeEach(() => {
    calls = [];
    answer = async () => ({ ok: true, result: { done: true } });
  });

  it('answers /health without a credential', async () => {
    const res = await send(new Request(`${BASE}/health`), { WORKSPACE_GATEWAY_TOKEN: undefined });
    expect(res.status).toBe(200);
  });

  it('serves nobody without a token of 32 or more characters', async () => {
    for (const token of [undefined, '', 'short']) {
      const res = await send(post(`/v1/workspaces/acme/shared/list`, {}), {
        WORKSPACE_GATEWAY_TOKEN: token,
      });
      expect(res.status).toBe(503);
      expect(((await res.json()) as { error: string }).error).toBe('misconfigured');
    }
    expect(calls).toEqual([]);
  });

  it('refuses a missing, wrong, or prefix-of bearer', async () => {
    for (const header of [
      '',
      `Bearer ${TOKEN.slice(0, 39)}`,
      `Bearer ${TOKEN}x`,
      `Basic ${TOKEN}`,
      TOKEN,
    ]) {
      const res = await send(
        post(`/v1/workspaces/acme/shared/list`, {}, { authorization: header }),
      );
      expect(res.status, header).toBe(401);
    }
    expect(calls).toEqual([]);
  });

  it('names the sandbox from the scope and fills each operation’s defaults', async () => {
    const res = await send(post(`/v1/workspaces/acme/${THREAD}/read`, { path: 'a.txt' }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ result: { done: true } });
    expect(calls).toEqual([
      {
        name: `acme/${THREAD}`,
        scope: `acme/${THREAD}`,
        request: { op: 'read', path: 'a.txt', offset: 0, limit: 512_000 },
      },
    ]);
    await send(post(`/v1/workspaces/gh-4242/shared/search`, { query: 'x' }));
    expect(calls[1]?.request).toEqual({
      op: 'search',
      path: '.',
      query: 'x',
      regex: false,
      max_hits: 20,
    });
  });

  it('passes checkpoint and destroy through with no arguments', async () => {
    for (const op of ['checkpoint', 'destroy']) {
      const res = await send(post(`/v1/workspaces/acme/shared/${op}`));
      expect(res.status, op).toBe(200);
    }
    expect(calls.map((c) => c.request)).toEqual([{ op: 'checkpoint' }, { op: 'destroy' }]);
  });

  it('takes an exec with the shell tool’s defaults and bounds', async () => {
    await send(post('/v1/workspaces/acme/shared/exec', { argv: ['make', 'test'] }));
    expect(calls[0]?.request).toEqual({ op: 'exec', argv: ['make', 'test'], cwd: '.', timeout_ms: 300_000 });
    const bad: unknown[] = [
      {},
      { argv: [] },
      { argv: 'make' },
      { argv: [1] },
      { argv: Array(257).fill('x') },
      { argv: ['x'.repeat(64_001)] },
      { argv: ['ls'], timeout_ms: 3_600_001 },
      { argv: ['cat'], stdin: 'x'.repeat(256_001) },
    ];
    for (const body of bad) {
      const res = await send(post('/v1/workspaces/acme/shared/exec', body));
      expect(res.status, JSON.stringify(body).slice(0, 60)).toBe(400);
    }
    expect(calls).toHaveLength(1);
  });

  it('takes a clone with a repository, a branch and a token, and nothing looser', async () => {
    const ok = { repo: 'felix-run/felix', branch: 'main', token: 'ghs_x' };
    await send(post('/v1/workspaces/acme/shared/clone', ok));
    expect(calls[0]?.request).toEqual({ op: 'clone', ...ok });
    for (const body of [
      { ...ok, repo: 'felix' },
      { ...ok, repo: 'a/../b' },
      { ...ok, branch: '--upload-pack=x' },
      { ...ok, branch: 'a..b' },
      { ...ok, token: '' },
      { repo: ok.repo, branch: ok.branch },
    ]) {
      const res = await send(post('/v1/workspaces/acme/shared/clone', body));
      expect(res.status, JSON.stringify(body)).toBe(400);
    }
    expect(calls).toHaveLength(1);
  });

  it('names no sandbox for a malformed scope', async () => {
    for (const path of [
      `/v1/workspaces/..//shared/list`,
      `/v1/workspaces/a%2Fb/shared/list`,
      `/v1/workspaces/acme/${'A'.repeat(40)}/list`,
      `/v1/workspaces/acme/${'a'.repeat(39)}/list`,
      `/v1/workspaces/acme/other/list`,
      `/v1/workspaces/${'t'.repeat(129)}/shared/list`,
    ]) {
      const res = await send(post(path, {}));
      expect([400, 404], path).toContain(res.status);
    }
    expect(calls).toEqual([]);
  });

  it('takes only its operations, and only by POST', async () => {
    expect((await send(post(`/v1/workspaces/acme/shared/delete`, {}))).status).toBe(404);
    const get = new Request(`${BASE}/v1/workspaces/acme/shared/list`, {
      headers: { authorization: `Bearer ${TOKEN}` },
    });
    expect((await send(get)).status).toBe(405);
    expect(calls).toEqual([]);
  });

  it('checks each operation’s bounds before any sandbox is asked', async () => {
    const bad: [string, unknown][] = [
      ['read', { path: 'a', limit: 512_001 }],
      ['read', { path: 'a', offset: -1 }],
      ['read', {}],
      ['write', { path: 'a', data: 1 }],
      ['edit', { path: 'a', old: '', new: 'b' }],
      ['search', { query: 'x'.repeat(513) }],
      ['search', { query: 'x', max_hits: 51 }],
      ['list', []],
    ];
    for (const [op, body] of bad) {
      const res = await send(post(`/v1/workspaces/acme/shared/${op}`, body));
      expect(res.status, `${op} ${JSON.stringify(body)}`).toBe(400);
    }
    expect(calls).toEqual([]);
  });

  it('refuses a body over the cap', async () => {
    const res = await send(
      post(`/v1/workspaces/acme/shared/write`, { path: 'a', data: 'x'.repeat(1_100_000) }),
    );
    expect(res.status).toBe(413);
    expect(calls).toEqual([]);
  });

  it('returns the helper’s refusal as its code and message', async () => {
    answer = async () => ({
      ok: false,
      error: 'edit_refused',
      message: 'old_string not found in a.txt',
    });
    const res = await send(
      post(`/v1/workspaces/acme/shared/edit`, { path: 'a.txt', old: 'x', new: 'y' }),
    );
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({
      error: 'edit_refused',
      message: 'old_string not found in a.txt',
    });
  });

  it('says the sandbox is unavailable when its Durable Object fails', async () => {
    answer = async () => {
      throw new Error('container crashed');
    };
    const res = await send(post(`/v1/workspaces/acme/shared/list`, {}));
    expect(res.status).toBe(503);
    expect(((await res.json()) as { error: string }).error).toBe('unavailable');
  });
});

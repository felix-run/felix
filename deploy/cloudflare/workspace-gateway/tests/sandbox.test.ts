/**
 * The Durable Object's own ordering, against a Container and a backup store kept in memory: what
 * runs beside what. The helper it runs is tested in the harness (`test_workspace_gateway_helper.py`).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

let deleteGate: Promise<void> = Promise.resolve();
let backupGate: Promise<void> = Promise.resolve();

vi.mock('@cloudflare/sandbox', () => ({
  DirectoryBackup: class {
    async backup() {
      await backupGate;
      return { id: 'new', size: 1 };
    }
    async restore() {}
    async delete() {
      await deleteGate;
    }
  },
}));

const { WorkspaceSandbox } = await import('../src/sandbox');

type Events = string[];

function fakeContainer(events: Events) {
  const answer = new TextEncoder().encode(JSON.stringify({ ok: true, result: {} }));
  return {
    running: true,
    images: { workspace: 'img' },
    start() {
      events.push('start');
      this.running = true;
    },
    async destroy() {
      events.push('destroy');
      this.running = false;
    },
    async setInactivityTimeout() {},
    async interceptOutboundHttps() {},
    async exec(argv: string[], opts?: { stdin?: ReadableStream }) {
      const request = opts?.stdin ? JSON.parse(await new Response(opts.stdin).text()) : { op: argv.join(" ") };
      events.push(`exec ${request.op}`);
      return { output: async () => ({ exitCode: 0, stdout: answer, stderr: new Uint8Array() }) };
    },
  };
}

function fakeState(container: ReturnType<typeof fakeContainer>) {
  const kv = new Map<string, unknown>();
  return {
    kv,
    state: {
      container,
      exports: { DirectoryBackupGateway: {}, GitHubGateway: () => ({}) },
      blockConcurrencyWhile: async (f: () => Promise<unknown>) => f(),
      storage: {
        kv: {
          get: (k: string) => kv.get(k),
          put: (k: string, v: unknown) => kv.set(k, v),
          delete: (k: string) => kv.delete(k),
        },
        setAlarm: async () => {},
        deleteAlarm: async () => {},
      },
    },
  };
}

function gate(): [Promise<void>, () => void] {
  let open = () => {};
  const shut = new Promise<void>((resolve) => {
    open = resolve;
  });
  return [shut, open];
}

describe('WorkspaceSandbox ordering', () => {
  let events: Events;
  let container: ReturnType<typeof fakeContainer>;
  let kv: Map<string, unknown>;
  let sandbox: InstanceType<typeof WorkspaceSandbox>;

  beforeEach(() => {
    events = [];
    container = fakeContainer(events);
    const fake = fakeState(container);
    kv = fake.kv;
    kv.set('backup', { id: 'old', size: 1 });
    sandbox = new WorkspaceSandbox(fake.state as never, { WORKSPACE_INSTANCE: '' } as never);
  });

  it('holds a background call that arrives mid-destroy until the destroy is done', async () => {
    const [shut, open] = gate();
    deleteGate = shut;
    const destroyed = sandbox.operate('acme/shared', { op: 'destroy' });
    await vi.waitFor(() => expect(events).toContain('destroy'));
    const polled = sandbox.operate('acme/shared', { op: 'exec_output', id: null, wait_ms: 0 });
    await new Promise((r) => setTimeout(r, 20));
    expect(events).not.toContain('start'); // not restarted from the backup being deleted
    open();
    await destroyed;
    await polled;
    expect(kv.has('backup')).toBe(false);
    expect(events.indexOf('start')).toBeGreaterThan(events.indexOf('destroy'));
    expect(events).toContain('exec exec_output');
  });

  it('waits for background calls already running before it destroys', async () => {
    const [shut, open] = gate();
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const exec = container.exec.bind(container);
    container.exec = async (argv, opts) => {
      const out = await exec(argv, opts);
      await held;
      return out;
    };
    deleteGate = Promise.resolve();
    const polled = sandbox.operate('acme/shared', { op: 'exec_output', id: null, wait_ms: 0 });
    await vi.waitFor(() => expect(events).toContain('exec exec_output'));
    const destroyed = sandbox.operate('acme/shared', { op: 'destroy' });
    await new Promise((r) => setTimeout(r, 20));
    expect(events).not.toContain('destroy');
    release();
    open();
    await Promise.all([polled, destroyed]);
    expect(events.at(-1)).toBe('destroy');
    void shut;
  });

  it('keeps a scope dirty when a background call writes during a checkpoint', async () => {
    const [shut, open] = gate();
    backupGate = shut;
    kv.set('dirty', true);
    const checkpointed = sandbox.operate('acme/shared', { op: 'checkpoint' });
    await new Promise((r) => setTimeout(r, 10));
    await sandbox.operate('acme/shared', { op: 'exec_output', id: null, wait_ms: 0 });
    open();
    await checkpointed;
    expect(kv.get('dirty')).toBe(true);
  });

  it('clears dirty after a checkpoint nothing wrote during', async () => {
    backupGate = Promise.resolve();
    kv.set('dirty', true);
    await sandbox.operate('acme/shared', { op: 'checkpoint' });
    expect(kv.get('dirty')).toBe(false);
  });
});

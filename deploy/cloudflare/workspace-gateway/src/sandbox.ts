/**
 * One Durable Object per workspace scope, holding the Container that scope's files live in.
 *
 * The Container is a Firecracker microVM started with the internet off and an empty
 * environment: nothing of the harness's, and none of this Worker's secrets, ever reaches it. Each
 * file operation runs the image's `felix-fs` helper once, with the request on stdin, and returns
 * the one JSON object it prints. Writes, edits, checkpoints and destroys of a scope run one at a
 * time, which is what the harness's per-path write lock gave the local backend, and stronger.
 *
 * **The disk does not survive the Container stopping, so `/workspace` is kept in R2.**
 * - A started Container gets the scope's latest backup restored into `/workspace` before any
 *   operation reaches it. A restore that fails destroys the Container and refuses the call, so the
 *   next call tries again: a scope is never served empty while its files sit in R2.
 * - `checkpoint` backs `/workspace` up (`DirectoryBackup`: one object, written by the Container
 *   through a grant for that one object; it holds no R2 credential), records it, then deletes the
 *   previous one. The harness calls it when a run that wrote files ends.
 * - A write or an edit marks the scope dirty, and an alarm shortly before the inactivity timeout
 *   checkpoints a dirty scope, so a run that ends without a checkpoint loses nothing to idling.
 * - `destroy` stops the Container and deletes its backup: the retention sweep's call.
 */
import {
  DirectoryBackup,
  type DirectoryBackupGatewayBinding,
  type DirectoryBackupRecord,
} from '@cloudflare/sandbox';
import { DurableObject } from 'cloudflare:workers';
import type { Env } from './handler';
import type { HelperAnswer, HelperRequest } from './protocol';

/** Stopped after this long with no operation, and not billed while stopped. */
export const INACTIVITY_TIMEOUT_MS = 10 * 60 * 1000;
/** The idle backup runs this long after the last operation: before the stop, with a margin. */
export const IDLE_BACKUP_AFTER_MS = INACTIVITY_TIMEOUT_MS - 2 * 60 * 1000;
/** Longer than any one tool call: the harness's search budget is 5s, a read or write far less. */
const OPERATION_TIMEOUT_MS = 30 * 1000;
/** A backup or a restore moves the whole workspace; give it room, but not forever. */
const TRANSFER_TIMEOUT_MS = 5 * 60 * 1000;
const HELPER = ['python3', '-I', '/opt/felix-fs/felix_fs.py'];
const WORKSPACE = '/workspace';
const BACKUP_KEY = 'backup';
const DIRTY_KEY = 'dirty';

type GatewayState = DurableObjectState & {
  readonly exports: { readonly DirectoryBackupGateway: DirectoryBackupGatewayBinding };
};

export class WorkspaceSandbox extends DurableObject<Env> {
  readonly #container: Container;
  readonly #backups: DirectoryBackup;
  #serial: Promise<unknown> = Promise.resolve();
  #starting: Promise<void> | null = null;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const container = ctx.container;
    if (container === undefined) throw new Error('the container binding is not configured');
    this.#container = container;
    // One prefix for every scope: each Durable Object keeps its own record, and an object's key is
    // a fresh UUID, so no scope can name another's backup.
    this.#backups = new DirectoryBackup(
      container,
      (ctx as GatewayState).exports.DirectoryBackupGateway,
      { binding: 'BACKUPS', prefix: 'workspaces/' },
    );
    // Each Durable Object instance sets its own timeout; it is not inherited across restarts.
    if (container.running) {
      void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
    }
  }

  async operate(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    const answer = await (request.op === 'read' ||
    request.op === 'list' ||
    request.op === 'search' ||
    request.op === 'prepare'
      ? this.#run(scope, request)
      : this.#oneAtATime(() => this.#run(scope, request)));
    if (request.op !== 'destroy') await this.ctx.storage.setAlarm(Date.now() + IDLE_BACKUP_AFTER_MS);
    return answer;
  }

  /** Checkpoints a scope written since its last backup, before the inactivity timeout stops it. */
  override async alarm(): Promise<void> {
    if (!this.ctx.storage.kv.get<boolean>(DIRTY_KEY) || !this.#container.running) return;
    // Thrown, not swallowed: the runtime retries a failed alarm with backoff.
    await this.#oneAtATime(() => this.#checkpoint());
  }

  #oneAtATime<T>(work: () => Promise<T>): Promise<T> {
    const next = this.#serial.then(work, work);
    this.#serial = next.catch(() => undefined);
    return next;
  }

  async #run(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    if (request.op === 'destroy') return this.#destroy(scope);
    try {
      await this.#ensureStarted(scope);
    } catch (cause) {
      console.error({ event: 'workspace.start.failed', scope, error: describe(cause) });
      return {
        ok: false,
        error: 'unavailable',
        message: 'the workspace sandbox could not be started or its files restored',
      };
    }
    if (request.op === 'checkpoint') {
      try {
        return { ok: true, result: await this.#checkpoint() };
      } catch (cause) {
        console.error({ event: 'workspace.checkpoint.failed', scope, error: describe(cause) });
        return { ok: false, error: 'unavailable', message: 'the workspace could not be backed up' };
      }
    }
    const answer = await this.#helper(scope, request);
    if (answer.ok && (request.op === 'write' || request.op === 'edit')) {
      this.ctx.storage.kv.put(DIRTY_KEY, true);
    }
    return answer;
  }

  async #helper(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), OPERATION_TIMEOUT_MS);
    try {
      const process = await this.#container.exec(HELPER, {
        cwd: '/',
        env: {},
        stdin: new Blob([JSON.stringify(request)]).stream(),
        signal: abort.signal,
      });
      const { exitCode, stdout, stderr } = await process.output();
      if (exitCode !== 0) {
        console.error({
          event: 'workspace.helper.failed',
          scope,
          op: request.op,
          exitCode,
          stderr: new TextDecoder().decode(stderr).slice(-2000),
        });
        return { ok: false, error: 'io_error', message: 'the workspace helper failed' };
      }
      return JSON.parse(new TextDecoder().decode(stdout)) as HelperAnswer;
    } catch (cause) {
      if (abort.signal.aborted) {
        return {
          ok: false,
          error: 'timeout',
          message: `the operation took over ${OPERATION_TIMEOUT_MS / 1000}s`,
        };
      }
      console.error({ event: 'workspace.exec.failed', scope, op: request.op, error: describe(cause) });
      return { ok: false, error: 'unavailable', message: 'the workspace sandbox did not answer' };
    } finally {
      clearTimeout(timer);
    }
  }

  /** A running Container with the scope's files in it. Concurrent callers share one start. */
  #ensureStarted(scope: string): Promise<void> {
    if (this.#container.running && this.#starting === null) return Promise.resolve();
    this.#starting ??= this.#start(scope).finally(() => {
      this.#starting = null;
    });
    return this.#starting;
  }

  async #start(scope: string): Promise<void> {
    if (this.#container.running) return;
    const image = this.#container.images.workspace;
    if (image === undefined) throw new Error('no `workspace` image is configured');
    this.#container.start({
      image,
      instance: 'lite',
      enableInternet: false,
      env: {},
      labels: { app: 'felix-workspace' },
    });
    await this.#container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    const record = this.ctx.storage.kv.get<DirectoryBackupRecord>(BACKUP_KEY);
    if (record === undefined) {
      // A scope with nothing saved yet. Made here rather than in the image (see the Dockerfile).
      const made = await (await this.#container.exec(['mkdir', '-p', WORKSPACE])).output();
      if (made.exitCode !== 0) throw new Error(`mkdir ${WORKSPACE} exited ${made.exitCode}`);
      return;
    }
    try {
      await withTimeout(TRANSFER_TIMEOUT_MS, (signal) =>
        this.#backups.restore(record, { dir: WORKSPACE, signal }),
      );
    } catch (cause) {
      // Never serve the scope without its files: stop, so the next call restores again.
      console.error({ event: 'workspace.restore.failed', scope, backup: record.id, error: describe(cause) });
      await this.#container.destroy();
      throw cause;
    }
  }

  async #checkpoint(): Promise<Record<string, unknown>> {
    const previous = this.ctx.storage.kv.get<DirectoryBackupRecord>(BACKUP_KEY);
    if (!this.#container.running || !this.ctx.storage.kv.get<boolean>(DIRTY_KEY)) {
      // Nothing written since the last backup (or nothing to back up): the record still holds.
      return { backed_up: false, size: previous?.size ?? 0 };
    }
    const record = await withTimeout(TRANSFER_TIMEOUT_MS, (signal) =>
      this.#backups.backup({ dir: WORKSPACE, signal }),
    );
    this.ctx.storage.kv.put(BACKUP_KEY, record);
    this.ctx.storage.kv.put(DIRTY_KEY, false);
    if (previous !== undefined) {
      // After the new record is stored, so a failure here leaves an orphan, never a gap. The
      // retention sweep reconciles the bucket against the records.
      await this.#backups.delete(previous).catch((cause: unknown) => {
        console.error({ event: 'workspace.backup.delete.failed', backup: previous.id, error: describe(cause) });
      });
    }
    return { backed_up: true, size: record.size };
  }

  async #destroy(scope: string): Promise<HelperAnswer> {
    const work = async () => {
      if (this.#container.running) await this.#container.destroy();
      const record = this.ctx.storage.kv.get<DirectoryBackupRecord>(BACKUP_KEY);
      if (record !== undefined) await this.#backups.delete(record);
      this.ctx.storage.kv.delete(BACKUP_KEY);
      this.ctx.storage.kv.delete(DIRTY_KEY);
      await this.ctx.storage.deleteAlarm();
    };
    try {
      await work();
      return { ok: true, result: {} };
    } catch (cause) {
      console.error({ event: 'workspace.destroy.failed', scope, error: describe(cause) });
      return { ok: false, error: 'unavailable', message: 'the workspace could not be destroyed' };
    }
  }
}

// Not AbortSignal.timeout(): it stays armed after the operation ends.
async function withTimeout<T>(ms: number, operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), ms);
  try {
    return await operation(abort.signal);
  } finally {
    clearTimeout(timer);
  }
}

function describe(cause: unknown): string {
  return cause instanceof Error && cause.stack !== undefined ? cause.stack : String(cause);
}

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
import type { GitHubProps } from './github-rules';
import type { Env } from './handler';
import { DEFAULT_INSTANCE, type HelperAnswer, type HelperRequest, instanceType } from './protocol';

/** Stopped after this long with no operation, and not billed while stopped. */
export const INACTIVITY_TIMEOUT_MS = 10 * 60 * 1000;
/** The idle backup runs this long after the last operation: before the stop, with a margin. */
export const IDLE_BACKUP_AFTER_MS = INACTIVITY_TIMEOUT_MS - 2 * 60 * 1000;
/** Longer than any one tool call: the harness's search budget is 5s, a read or write far less. */
const OPERATION_TIMEOUT_MS = 30 * 1000;
/** An `exec` is bounded by its own timeout; the helper kills at it and drains for up to 5s. */
const EXEC_GRACE_MS = 30 * 1000;
/** `git` is bounded by the harness's `_GIT_TIMEOUT_S` (60s) inside the helper. */
const GIT_DEADLINE_MS = 75 * 1000;
/** A backup or a restore moves the whole workspace; give it room, but not forever. */
const TRANSFER_TIMEOUT_MS = 5 * 60 * 1000;
const HELPER = ['python3', '-I', '/opt/felix-fs/felix_fs.py'];
const WORKSPACE = '/workspace';
/** A clone moves a whole repository through the intercept; the harness caps its size. */
const CLONE_TIMEOUT_MS = 10 * 60 * 1000;
/** Where the runtime puts the CA it re-signs intercepted HTTPS with, once an intercept exists. */
const CONTAINERS_CA = '/etc/cloudflare/certs/cloudflare-containers-ca.crt';
const BACKUP_KEY = 'backup';
const DIRTY_KEY = 'dirty';

type GatewayState = DurableObjectState & {
  readonly exports: {
    readonly DirectoryBackupGateway: DirectoryBackupGatewayBinding;
    readonly GitHubGateway: (options: { readonly props: GitHubProps }) => Fetcher;
  };
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
    request.op === 'prepare' ||
    request.op === 'git' ||
    request.op === 'lstat'
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
    if (request.op === 'clone') return this.#clone(scope, request);
    if (request.op === 'checkpoint') {
      try {
        return { ok: true, result: await this.#checkpoint() };
      } catch (cause) {
        console.error({ event: 'workspace.checkpoint.failed', scope, error: describe(cause) });
        return { ok: false, error: 'unavailable', message: 'the workspace could not be backed up' };
      }
    }
    const answer = await this.#helper(scope, request);
    if (answer.ok && (request.op === 'write' || request.op === 'edit' || request.op === 'exec')) {
      this.ctx.storage.kv.put(DIRTY_KEY, true);
    }
    return answer;
  }

  async #helper(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    const deadline =
      request.op === 'exec'
        ? request.timeout_ms + EXEC_GRACE_MS
        : request.op === 'git'
          ? GIT_DEADLINE_MS
          : OPERATION_TIMEOUT_MS;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), deadline);
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
          message: `the operation took over ${deadline / 1000}s`,
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
      // The handler refuses every request while WORKSPACE_INSTANCE names no type.
      instance: instanceType(this.env.WORKSPACE_INSTANCE) ?? DEFAULT_INSTANCE,
      enableInternet: false,
      env: {},
      labels: { app: 'felix-workspace' },
    });
    await this.#container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    // github.com resolves only because it is intercepted, and the intercept denies until a clone
    // grants. Registered now so that is true from the Container's first instant.
    await this.#github({ mode: 'deny' });
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

  #github(props: GitHubProps): Promise<void> {
    const exports = (this.ctx as GatewayState).exports;
    return this.#container.interceptOutboundHttps('github.com', exports.GitHubGateway({ props }));
  }

  async #output(argv: string[], env: Record<string, string> = {}, timeoutMs = OPERATION_TIMEOUT_MS) {
    return withTimeout(timeoutMs, async (signal) => {
      const process = await this.#container.exec(argv, { cwd: '/', env, signal });
      const { exitCode, stdout, stderr } = await process.output();
      const text = (b: ArrayBuffer) => new TextDecoder().decode(b);
      return { exitCode, stdout: text(stdout), stderr: text(stderr) };
    });
  }

  /**
   * Clone `repo` into the scope's empty /workspace with the person's token. The token reaches
   * `GitHubGateway` as props and is added to the requests there; the container never holds it.
   * One clone at a time per scope (this runs inside `#oneAtATime`), since a grant is per container.
   */
  async #clone(
    scope: string,
    request: Extract<HelperRequest, { op: 'clone' }>,
  ): Promise<HelperAnswer> {
    const occupied = await this.#output(['find', WORKSPACE, '-mindepth', '1', '-maxdepth', '1', '-print', '-quit']);
    if (occupied.exitCode !== 0 || occupied.stdout.trim() !== '') {
      return { ok: false, error: 'conflict', message: 'a repository is cloned into an empty workspace, and this one is not' };
    }
    const path = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
    try {
      await this.#github({ mode: 'grant', token: request.token, repo: request.repo });
      // The CA appears once an intercept is registered; git trusts it and nothing else here.
      const ca = await this.#output(['sh', '-c', `for i in $(seq 1 100); do [ -s ${CONTAINERS_CA} ] && exit 0; sleep 0.1; done; exit 1`]);
      if (ca.exitCode !== 0) {
        return { ok: false, error: 'unavailable', message: 'the sandbox never received the certificate it reaches GitHub with' };
      }
      const cloned = await this.#output(
        [
          'git',
          '-c', 'protocol.file.allow=never',
          '-c', 'protocol.ext.allow=never',
          '-c', 'submodule.recurse=false',
          '-c', 'credential.helper=',
          'clone', '--no-recurse-submodules',
          '--branch', request.branch,
          '--origin', 'origin',
          '--', `https://github.com/${request.repo}.git`, WORKSPACE,
        ],
        { PATH: path, HOME: '/tmp', GIT_SSL_CAINFO: CONTAINERS_CA, GIT_TERMINAL_PROMPT: '0' },
        CLONE_TIMEOUT_MS,
      );
      if (cloned.exitCode !== 0) {
        await this.#output(['find', WORKSPACE, '-mindepth', '1', '-delete']);
        console.error({ event: 'workspace.clone.failed', scope, repo: request.repo, stderr: cloned.stderr.slice(-2000) });
        return { ok: false, error: 'clone_failed', message: `git clone failed: ${cloned.stderr.trim().split('\n').slice(-3).join(' ')}` };
      }
    } finally {
      // Whatever happened, the grant ends with the clone.
      await this.#github({ mode: 'deny' });
    }
    const head = await this.#output(['git', '-C', WORKSPACE, 'rev-parse', 'HEAD'], { PATH: path, HOME: '/tmp' });
    this.ctx.storage.kv.put(DIRTY_KEY, true);
    return { ok: true, result: { repo: request.repo, branch: request.branch, head: head.stdout.trim() } };
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

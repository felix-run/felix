/**
 * One Durable Object per workspace scope, holding the Container that scope's files live in.
 *
 * The Container is a Firecracker microVM started with the internet off and an empty
 * environment: nothing of the harness's, and none of this Worker's secrets, ever reaches it. Each
 * operation runs the image's `felix-fs` helper once, with the request on stdin, and returns the
 * one JSON object it prints. Writes and edits to a scope are run one at a time, which is what the
 * harness's per-path write lock gave the local backend, and stronger.
 *
 * Its disk does not survive the Container stopping. Keeping `/workspace` across a stop is the next
 * change (backup to R2 at the end of a run, restore on start): until then a scope's files last as
 * long as its Container does, which is why `hosted` is opt-in in the harness.
 */
import { DurableObject } from 'cloudflare:workers';
import type { Env } from './handler';
import type { HelperAnswer, HelperRequest } from './protocol';

/** Stopped after this long with no operation, and not billed while stopped. */
export const INACTIVITY_TIMEOUT_MS = 10 * 60 * 1000;
/** Longer than any one tool call: the harness's search budget is 5s, a read or write far less. */
const OPERATION_TIMEOUT_MS = 30 * 1000;
const HELPER = ['python3', '-I', '/opt/felix-fs/felix_fs.py'];

export class WorkspaceSandbox extends DurableObject<Env> {
  readonly #container: Container;
  #writes: Promise<unknown> = Promise.resolve();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const container = ctx.container;
    if (container === undefined) throw new Error('the container binding is not configured');
    this.#container = container;
    // Each Durable Object instance sets its own timeout; it is not inherited across restarts.
    if (container.running) {
      void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
    }
  }

  async operate(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    const run = () => this.#run(scope, request);
    if (request.op !== 'write' && request.op !== 'edit') return run();
    const next = this.#writes.then(run, run);
    this.#writes = next.catch(() => undefined);
    return next;
  }

  async #run(scope: string, request: HelperRequest): Promise<HelperAnswer> {
    try {
      await this.#ensureStarted();
    } catch (cause) {
      console.error({ event: 'workspace.start.failed', scope, error: String(cause) });
      return {
        ok: false,
        error: 'unavailable',
        message: 'the workspace sandbox could not be started',
      };
    }
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
      console.error({
        event: 'workspace.exec.failed',
        scope,
        op: request.op,
        error: String(cause),
      });
      return { ok: false, error: 'unavailable', message: 'the workspace sandbox did not answer' };
    } finally {
      clearTimeout(timer);
    }
  }

  async #ensureStarted(): Promise<void> {
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
  }
}

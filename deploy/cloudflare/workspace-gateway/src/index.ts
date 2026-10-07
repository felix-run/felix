/**
 * The workspace gateway: the harness's hosted workspace backend reaches each scope's sandbox
 * through here (docs/WORKSPACE.md phase 3 in felix-run/felix). `handler.ts` is the HTTP surface,
 * `sandbox.ts` the Durable Object that holds a scope's Container.
 */
import handler from './handler';

export { WorkspaceSandbox } from './sandbox';

// `satisfies`, not a cast: the real namespace's stub has to be what the handler calls.
export default handler satisfies ExportedHandler<WorkerEnv>;

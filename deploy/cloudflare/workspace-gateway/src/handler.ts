/**
 * The gateway's HTTP surface: authenticate the harness, name the scope's sandbox, hand the
 * operation to its Durable Object, and return what the helper answered.
 *
 * Kept apart from the Durable Object (`sandbox.ts`) so it imports nothing from
 * `cloudflare:workers`, and the contract suite (`@felix/test-kit/workspace-gateway`) can hold it
 * to the wire format in Node.
 */
import {
  type ErrorCode,
  type HelperAnswer,
  type HelperRequest,
  INSTANCE_TYPES,
  instanceType,
  MAX_BODY_BYTES,
  OPS,
  type Op,
  parseRequest,
  STATUS,
  scopeName,
} from './protocol';

/** The one method the handler calls on a scope's Durable Object. */
export interface WorkspaceStub {
  operate(scope: string, request: HelperRequest): Promise<HelperAnswer>;
}

export interface Env {
  SANDBOX: { getByName(name: string): WorkspaceStub };
  /** The harness's bearer. A secret (`wrangler secret put`), at least 32 characters. */
  WORKSPACE_GATEWAY_TOKEN?: string;
  /** The sandbox's instance type (`INSTANCE_TYPES`); `standard-1` when unset. A plain var. */
  WORKSPACE_INSTANCE?: string;
}

export const MIN_TOKEN_CHARS = 32;

/** The refusals that carry a `count` (a folder operation's): nothing else passes one through. */
const COUNTED: ReadonlySet<ErrorCode> = new Set(['too_many_entries', 'too_deep', 'workspace_changed']);

const ROUTE = /^\/v1\/workspaces\/([^/]+)\/([^/]+)\/([^/]+)$/;

function refuse(
  error: ErrorCode,
  message: string,
  kind?: string,
  current?: { sha256: string | null; bytes: number | null } | { count: number },
): Response {
  // `kind` (the helper's exception name, for a filesystem failure) lets the harness raise the same
  // exception the local backend would, so a tool words the failure the same on both. `current` is
  // a `workspace_changed` refusal's file as it is now, which the harness returns to its caller --
  // or, for a folder operation, `count`: the folder's file count now, or the entries it walked.
  return Response.json({ error, message, ...(kind ? { kind } : {}), ...(current ?? {}) }, {
    status: STATUS[error],
  });
}

/** Constant-time over the longer of the two, so the comparison says nothing about the length. */
function sameSecret(presented: string, expected: string): boolean {
  const a = new TextEncoder().encode(presented);
  const b = new TextEncoder().encode(expected);
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  }
  return diff === 0;
}

function authorized(request: Request, token: string): boolean {
  const header = request.headers.get('authorization') ?? '';
  const [scheme, presented] = header.split(' ', 2);
  return scheme?.toLowerCase() === 'bearer' && !!presented && sameSecret(presented, token);
}

async function readBody(request: Request): Promise<unknown | Response> {
  const declared = Number(request.headers.get('content-length') ?? '0');
  if (declared > MAX_BODY_BYTES) {
    return refuse('payload_too_large', `the body is over ${MAX_BODY_BYTES} bytes`);
  }
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length > MAX_BODY_BYTES) {
    return refuse('payload_too_large', `the body is over ${MAX_BODY_BYTES} bytes`);
  }
  if (bytes.length === 0) return {};
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return refuse('bad_request', 'the body must be JSON');
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health') {
      return request.method === 'GET' ? new Response('ok') : new Response(null, { status: 405 });
    }

    const token = env.WORKSPACE_GATEWAY_TOKEN ?? '';
    if (token.length < MIN_TOKEN_CHARS) {
      // Refused before anything else: a gateway with no token, or a guessable one, serves nobody.
      return refuse('misconfigured', 'WORKSPACE_GATEWAY_TOKEN is not set to 32 or more characters');
    }
    if (instanceType(env.WORKSPACE_INSTANCE) === null) {
      // Refused rather than quietly sized some other way: the operator named a size on purpose.
      return refuse('misconfigured', `WORKSPACE_INSTANCE must be one of ${INSTANCE_TYPES.join(', ')}`);
    }
    if (!authorized(request, token)) {
      return refuse('unauthorized', 'a valid bearer is required');
    }

    const match = ROUTE.exec(url.pathname);
    if (match === null) return new Response('Not found', { status: 404 });
    const [, tenant = '', key = '', op = ''] = match;
    if (!(OPS as readonly string[]).includes(op)) return new Response('Not found', { status: 404 });
    if (request.method !== 'POST') return new Response(null, { status: 405 });
    // Not URL-decoded: a scope is plain characters, so anything encoded is not one.
    const scope = scopeName(tenant, key);
    if (scope === null) {
      return refuse(
        'bad_request',
        'not a workspace scope: tenant [A-Za-z0-9._-]{1,128}, key `shared` or 40 hex',
      );
    }

    const body = await readBody(request);
    if (body instanceof Response) return body;
    const parsed = parseRequest(op as Op, body);
    if (typeof parsed === 'string') return refuse('bad_request', parsed);

    let answer: HelperAnswer;
    try {
      answer = await env.SANDBOX.getByName(scope).operate(scope, parsed);
    } catch (cause) {
      console.error({ event: 'workspace.operate.failed', scope, op, error: String(cause) });
      return refuse('unavailable', 'the workspace sandbox did not answer');
    }
    return answer.ok
      ? Response.json({ result: answer.result })
      : refuse(
          answer.error,
          answer.message,
          answer.kind,
          typeof answer.count === 'number' && COUNTED.has(answer.error)
            ? { count: answer.count }
            : answer.error === 'workspace_changed'
              ? { sha256: answer.sha256 ?? null, bytes: answer.bytes ?? null }
              : undefined,
        );
  },
};

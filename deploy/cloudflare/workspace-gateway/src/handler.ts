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
}

export const MIN_TOKEN_CHARS = 32;

const ROUTE = /^\/v1\/workspaces\/([^/]+)\/([^/]+)\/([^/]+)$/;

function refuse(error: ErrorCode, message: string): Response {
  return Response.json({ error, message }, { status: STATUS[error] });
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
      : refuse(answer.error, answer.message);
  },
};

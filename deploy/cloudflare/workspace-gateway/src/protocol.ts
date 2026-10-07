/**
 * The gateway's wire contract: what the harness's hosted workspace backend sends, and what it
 * gets back. Mirrored by `HostedBackend` in the harness (felix-run/felix), which maps each error
 * code to the exception its workspace tools already handle.
 *
 *   POST /v1/workspaces/{tenant}/{key}/{op}     Authorization: Bearer <WORKSPACE_GATEWAY_TOKEN>
 *   GET  /health                                no credential
 *
 * `{tenant}/{key}` is a workspace scope (felix/tools/workspace_scope.py): `key` is `shared` for a
 * tenant's shared scope or the 40-hex hash of a thread. The gateway never takes a sandbox id: it
 * derives the Durable Object's name from the two, so even a leaked token names nothing outside
 * the scope scheme.
 */

export const OPS = [
  'prepare',
  'list',
  'read',
  'write',
  'edit',
  'search',
  // Not file operations: back `/workspace` up to R2 now, and stop the sandbox and delete its backup.
  'checkpoint',
  'destroy',
] as const;
export type Op = (typeof OPS)[number];

/** The limits of the harness's workspace tools, held again here and in the helper. */
export const MAX_READ_BYTES = 512_000;
export const MAX_WRITE_BYTES = 512_000;
export const MAX_QUERY_CHARS = 512;
export const MAX_SEARCH_HITS = 50;
/** A write's base64 body plus JSON framing, rounded up. Anything larger is not a tool call. */
export const MAX_BODY_BYTES = 1024 * 1024;

const TENANT = /^[A-Za-z0-9._-]{1,128}$/;
const KEY = /^(shared|[0-9a-f]{40})$/;

/** A tenant id as the harness holds it (`assert_valid_tenant_id`), and a scope key. */
export function scopeName(tenant: string, key: string): string | null {
  if (!TENANT.test(tenant) || tenant === '.' || tenant === '..') return null;
  if (!KEY.test(key)) return null;
  return `${tenant}/${key}`;
}

export type HelperRequest =
  | { op: 'prepare' }
  | { op: 'list'; path: string }
  | { op: 'read'; path: string; offset: number; limit: number }
  | { op: 'write'; path: string; data: string; append: boolean }
  | { op: 'edit'; path: string; old: string; new: string; replace_all: boolean }
  | { op: 'search'; path: string; query: string; regex: boolean; max_hits: number }
  | { op: 'checkpoint' }
  | { op: 'destroy' };

export type ErrorCode =
  | 'bad_request'
  | 'invalid_path'
  | 'not_found'
  | 'not_a_directory'
  | 'not_a_file'
  | 'edit_refused'
  | 'permission_denied'
  | 'io_error'
  | 'unavailable'
  | 'timeout'
  | 'unauthorized'
  | 'misconfigured'
  | 'payload_too_large';

/** What the sandbox's `felix-fs` helper prints, and what the Durable Object returns. */
export type HelperAnswer =
  | { ok: true; result: Record<string, unknown> }
  | { ok: false; error: ErrorCode; message: string; kind?: string };

/** The HTTP status each answer travels under. The body carries the code; the status is for logs. */
export const STATUS: Record<ErrorCode, number> = {
  bad_request: 400,
  unauthorized: 401,
  permission_denied: 403,
  not_found: 404,
  not_a_directory: 409,
  not_a_file: 409,
  payload_too_large: 413,
  invalid_path: 422,
  edit_refused: 422,
  io_error: 500,
  misconfigured: 503,
  unavailable: 503,
  timeout: 504,
};

type Body = Record<string, unknown>;

function str(body: Body, field: string, fallback?: string): string | null {
  const value = body[field] ?? fallback;
  return typeof value === 'string' ? value : null;
}

function int(body: Body, field: string, fallback: number, min: number, max: number): number | null {
  const value = body[field] ?? fallback;
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max
    ? value
    : null;
}

function bool(body: Body, field: string): boolean | null {
  const value = body[field] ?? false;
  return typeof value === 'boolean' ? value : null;
}

/**
 * A request body as the helper's request, or the reason it is not one. Shapes only: what a path
 * means is the helper's to decide, against the filesystem it walks.
 */
export function parseRequest(op: Op, raw: unknown): HelperRequest | string {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return 'the body must be a JSON object';
  }
  const body = raw as Body;
  switch (op) {
    case 'prepare':
    case 'checkpoint':
    case 'destroy':
      return { op };
    case 'list': {
      const path = str(body, 'path', '.');
      return path === null ? '`path` must be a string' : { op, path };
    }
    case 'read': {
      const path = str(body, 'path');
      const offset = int(body, 'offset', 0, 0, Number.MAX_SAFE_INTEGER);
      const limit = int(body, 'limit', MAX_READ_BYTES, 1, MAX_READ_BYTES);
      if (path === null) return '`path` must be a string';
      if (offset === null) return '`offset` must be a non-negative integer';
      if (limit === null) return `\`limit\` must be an integer from 1 to ${MAX_READ_BYTES}`;
      return { op, path, offset, limit };
    }
    case 'write': {
      const path = str(body, 'path');
      const data = str(body, 'data');
      const append = bool(body, 'append');
      if (path === null) return '`path` must be a string';
      if (data === null) return '`data` must be a base64 string';
      if (append === null) return '`append` must be a boolean';
      return { op, path, data, append };
    }
    case 'edit': {
      const path = str(body, 'path');
      const old = str(body, 'old');
      const replacement = str(body, 'new');
      const replaceAll = bool(body, 'replace_all');
      if (path === null) return '`path` must be a string';
      if (old === null || old === '') return '`old` must be a non-empty string';
      if (replacement === null) return '`new` must be a string';
      if (replaceAll === null) return '`replace_all` must be a boolean';
      return { op, path, old, new: replacement, replace_all: replaceAll };
    }
    case 'search': {
      const path = str(body, 'path', '.');
      const query = str(body, 'query');
      const regex = bool(body, 'regex');
      const maxHits = int(body, 'max_hits', 20, 1, MAX_SEARCH_HITS);
      if (path === null) return '`path` must be a string';
      if (query === null || query.length === 0 || query.length > MAX_QUERY_CHARS) {
        return `\`query\` must be 1 to ${MAX_QUERY_CHARS} characters`;
      }
      if (regex === null) return '`regex` must be a boolean';
      if (maxHits === null) return `\`max_hits\` must be an integer from 1 to ${MAX_SEARCH_HITS}`;
      return { op, path, query, regex, max_hits: maxHits };
    }
  }
}

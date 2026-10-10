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

import { REPO } from './github-rules';

export const OPS = [
  'prepare',
  'list',
  'read',
  'write',
  'edit',
  // The operator's file pane: remove a file, or move one without replacing anything, each after
  // comparing the file's digest when the harness sends one.
  'delete',
  'rename',
  'search',
  // A `shell_tools` command, run in the sandbox by the shell tool's own exec path.
  'exec',
  // A thread's repository, cloned into its empty /workspace with the person's token (`github.ts`).
  'clone',
  // The harness reading that repository: git run by its own `_git_run`, output kept from the start.
  'git',
  // Sizes for a repository listing.
  'lstat',
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
/** The shell tool's bounds (felix/tools/shell.py `ShellArgs`, `MAX_INTEGRATION_TIMEOUT_S`). */
export const MAX_ARGV_ITEMS = 256;
export const MAX_ARGV_BYTES = 64_000;
export const MAX_STDIN_CHARS = 256_000;
export const MAX_EXEC_TIMEOUT_MS = 3_600_000;
/** The harness's git output cap (`_GIT_OUTPUT_CAP`) and listing ceiling (`LIST_MAX`). */
export const MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024;
export const MAX_LSTAT_PATHS = 10_000;
/** A write's base64 body plus JSON framing, rounded up. Anything larger is not a tool call. */
export const MAX_BODY_BYTES = 1024 * 1024;

/**
 * The sandbox's size, from the `WORKSPACE_INSTANCE` var: the named types `ctx.container.start()`
 * takes under the `durable_object` scheduling policy (`basic` is not one of them). `standard-1`
 * (1/2 vCPU, 4 GiB, 8 GB) unless set: on `lite` (1/16 vCPU, 256 MiB, 2 GB) starting the helper and
 * git takes seconds per operation, a repository listing about 15, and git or a package install can
 * run out of memory. CPU is billed on use; memory and disk as provisioned while a sandbox is awake.
 */
export const INSTANCE_TYPES = ['lite', 'standard-1', 'standard-2', 'standard-3', 'standard-4'] as const;
export type InstanceType = (typeof INSTANCE_TYPES)[number];
export const DEFAULT_INSTANCE: InstanceType = 'standard-1';

/** The instance type `raw` names, the default when it is unset, or null when it names none. */
export function instanceType(raw: string | undefined): InstanceType | null {
  const value = (raw ?? '').trim();
  if (value === '') return DEFAULT_INSTANCE;
  return (INSTANCE_TYPES as readonly string[]).includes(value) ? (value as InstanceType) : null;
}

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
  | { op: 'delete'; path: string; expected_sha256: string | null }
  | { op: 'rename'; path: string; to_path: string; expected_sha256: string | null }
  | { op: 'search'; path: string; query: string; regex: boolean; max_hits: number }
  | { op: 'exec'; argv: string[]; cwd: string; stdin?: string; timeout_ms: number }
  | { op: 'clone'; repo: string; branch: string; token: string }
  | { op: 'git'; args: string[]; stdin?: string; limit: number }
  | { op: 'lstat'; paths: string[] }
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
  | 'payload_too_large'
  | 'conflict'
  | 'workspace_changed'
  | 'target_exists'
  | 'clone_failed';

/** What the sandbox's `felix-fs` helper prints, and what the Durable Object returns. */
export type HelperAnswer =
  | { ok: true; result: Record<string, unknown> }
  | {
      ok: false;
      error: ErrorCode;
      message: string;
      kind?: string;
      // `workspace_changed` only: the file's digest and size now, null when it is gone (or, for the
      // digest, over the read cap).
      sha256?: string | null;
      bytes?: number | null;
    };

/** The HTTP status each answer travels under. The body carries the code; the status is for logs. */
export const STATUS: Record<ErrorCode, number> = {
  bad_request: 400,
  unauthorized: 401,
  permission_denied: 403,
  not_found: 404,
  not_a_directory: 409,
  not_a_file: 409,
  payload_too_large: 413,
  conflict: 409,
  workspace_changed: 409,
  target_exists: 409,
  clone_failed: 502,
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

/** A lowercase hex SHA-256, absent (null), or not one (undefined). */
function digest(body: Body, field: string): string | null | undefined {
  const value = body[field] ?? null;
  if (value === null) return null;
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : undefined;
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
    case 'delete': {
      const path = str(body, 'path');
      const expected = digest(body, 'expected_sha256');
      if (path === null) return '`path` must be a string';
      if (expected === undefined) return '`expected_sha256` must be 64 lowercase hex characters';
      return { op, path, expected_sha256: expected };
    }
    case 'rename': {
      const path = str(body, 'path');
      const toPath = str(body, 'to_path');
      const expected = digest(body, 'expected_sha256');
      if (path === null) return '`path` must be a string';
      if (toPath === null) return '`to_path` must be a string';
      if (expected === undefined) return '`expected_sha256` must be 64 lowercase hex characters';
      return { op, path, to_path: toPath, expected_sha256: expected };
    }
    case 'exec': {
      const argv = body.argv;
      const cwd = str(body, 'cwd', '.');
      const timeoutMs = int(body, 'timeout_ms', 300_000, 1, MAX_EXEC_TIMEOUT_MS);
      const stdin = body.stdin;
      if (
        !Array.isArray(argv) ||
        argv.length === 0 ||
        argv.length > MAX_ARGV_ITEMS ||
        !argv.every((a) => typeof a === 'string') ||
        new TextEncoder().encode(argv.join('')).length > MAX_ARGV_BYTES
      ) {
        return `\`argv\` must be 1 to ${MAX_ARGV_ITEMS} strings, at most ${MAX_ARGV_BYTES} bytes`;
      }
      if (cwd === null) return '`cwd` must be a string';
      if (timeoutMs === null) return `\`timeout_ms\` must be an integer from 1 to ${MAX_EXEC_TIMEOUT_MS}`;
      if (stdin !== undefined && (typeof stdin !== 'string' || stdin.length > MAX_STDIN_CHARS)) {
        return `\`stdin\` must be a string of at most ${MAX_STDIN_CHARS} characters`;
      }
      return {
        op,
        argv: argv as string[],
        cwd,
        timeout_ms: timeoutMs,
        ...(stdin === undefined ? {} : { stdin: stdin as string }),
      };
    }
    case 'clone': {
      const repo = str(body, 'repo');
      const branch = str(body, 'branch');
      const token = str(body, 'token');
      if (repo === null || !REPO.test(repo)) return '`repo` must be `owner/name`';
      // A ref git accepts and cannot read as an option: no leading `-`, no `..`, no control bytes.
      if (
        branch === null ||
        !/^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,254}$/.test(branch) ||
        branch.includes('..')
      ) {
        return '`branch` must be a branch name';
      }
      if (token === null || token.length === 0 || token.length > 512) return '`token` must be a token';
      return { op, repo, branch, token };
    }
    case 'git': {
      const args = body.args;
      const limit = int(body, 'limit', MAX_GIT_OUTPUT_BYTES, 1, MAX_GIT_OUTPUT_BYTES);
      const stdin = body.stdin;
      if (
        !Array.isArray(args) ||
        args.length === 0 ||
        args.length > MAX_ARGV_ITEMS ||
        !args.every((a) => typeof a === 'string')
      ) {
        return `\`args\` must be 1 to ${MAX_ARGV_ITEMS} strings`;
      }
      if (limit === null) return `\`limit\` must be an integer from 1 to ${MAX_GIT_OUTPUT_BYTES}`;
      if (stdin !== undefined && stdin !== null && typeof stdin !== 'string') {
        return '`stdin` must be a base64 string';
      }
      return {
        op,
        args: args as string[],
        limit,
        ...(typeof stdin === 'string' ? { stdin } : {}),
      };
    }
    case 'lstat': {
      const paths = body.paths;
      if (!Array.isArray(paths) || paths.length > MAX_LSTAT_PATHS || !paths.every((p) => typeof p === 'string')) {
        return `\`paths\` must be at most ${MAX_LSTAT_PATHS} strings`;
      }
      return { op, paths: paths as string[] };
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

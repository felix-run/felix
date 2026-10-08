/**
 * The rules the `github.com` intercept enforces (`github.ts`), kept apart so they can be tested in
 * Node: a grant is git's read-only fetch of one repository, and nothing else.
 */
export type GitHubProps =
  | { mode: 'deny' }
  | { mode: 'grant'; token: string; repo: string };

/** `owner/name` as GitHub spells them: the grant compares case-insensitively, as GitHub does. */
export const REPO = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;

/** Whether `url` is git's fetch for `repo`, and nothing else. */
export function isFetchOf(request: Request, repo: string): boolean {
  const url = new URL(request.url);
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port !== '') return false;
  const path = url.pathname.toLowerCase();
  const base = `/${repo.toLowerCase()}`;
  const under = (suffix: string) => path === `${base}${suffix}` || path === `${base}.git${suffix}`;
  if (request.method === 'GET' && under('/info/refs')) {
    return url.searchParams.get('service') === 'git-upload-pack' && [...url.searchParams.keys()].length === 1;
  }
  return request.method === 'POST' && under('/git-upload-pack') && url.search === '';
}

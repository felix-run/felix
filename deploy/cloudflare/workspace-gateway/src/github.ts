/**
 * The one way a sandbox reaches GitHub: an outbound HTTPS intercept for `github.com`.
 *
 * The sandbox runs with the internet off, so `github.com` resolves at all only because the Durable
 * Object registers this intercept for it (every other name fails to resolve). The runtime ends the
 * TLS connection and hands the request here as an ordinary `Request`; whatever this returns is what
 * the container sees.
 *
 * - **Denied unless granted.** The Durable Object registers it with `{mode: 'deny'}` when the
 *   Container starts, and with a grant only for the length of one clone, then denies again.
 * - **A grant is one repository, read-only.** It allows git's smart-HTTP fetch for the granted
 *   repository and nothing else: `GET {repo}/info/refs?service=git-upload-pack` and
 *   `POST {repo}/git-upload-pack`. Never `git-receive-pack`: publishing is done by the harness
 *   through GitHub's API, so nothing in a sandbox ever pushes.
 * - **The token never enters the container.** It travels in this entrypoint's props, which the
 *   container cannot read, and is added to the request here, after anything the container sent as
 *   `Authorization` is dropped.
 *
 * What a grant still allows: while it is registered, any process in the sandbox can fetch that one
 * repository with the person's access. That is the clone's own permission, for the clone's duration.
 */
import { WorkerEntrypoint } from 'cloudflare:workers';
import { type GitHubProps, isFetchOf } from './github-rules';

function refuse(why: string): Response {
  return new Response(`blocked by the workspace gateway: ${why}\n`, { status: 403 });
}


export class GitHubGateway extends WorkerEntrypoint<unknown, GitHubProps> {
  override async fetch(request: Request): Promise<Response> {
    const props = this.ctx.props;
    if (props.mode !== 'grant') return refuse('no clone is in progress');
    if (!isFetchOf(request, props.repo)) return refuse(`only fetching ${props.repo} is allowed`);
    const headers = new Headers(request.headers);
    headers.delete('authorization');
    headers.set('authorization', `Basic ${btoa(`x-access-token:${props.token}`)}`);
    // `manual`: a redirect is GitHub's to make and the container's to follow -- through this
    // intercept again, where it is held to the same rule.
    return fetch(new Request(request, { headers, redirect: 'manual' }));
  }
}

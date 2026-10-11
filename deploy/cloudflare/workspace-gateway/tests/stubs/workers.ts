// `cloudflare:workers` for Node: the Durable Object base class keeps its state and env, nothing more.
export class DurableObject<Env = unknown> {
  constructor(
    readonly ctx: unknown,
    readonly env: Env,
  ) {}
}

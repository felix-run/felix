# Felix diagrams

Mermaid sources for the parts of Felix that are genuinely hard to describe in prose — branching,
ordering across participants, lifecycles with illegal transitions, and boundaries. Sources live in
[`diagrams/`](diagrams/) as `.mmd` files. Nothing here restates a paragraph that would have been
clearer as a paragraph.

Each file names its source of truth in a `%%` comment on the second line. **When you change that
file, change the diagram in the same commit** — a stale diagram is worse than no diagram, because it
is believed.

## The set

### Start here

| Diagram | The question it answers |
|---|---|
| [`compile-pipeline`](diagrams/compile-pipeline.mmd) | How does a `felix/v1` manifest become a runnable, governed agent? |
| [`governance-stack`](diagrams/governance-stack.mmd) | What does a tool call pass through, in what order, and where can it be denied? |
| [`request-path`](diagrams/request-path.mmd) | How does `POST /chat` reach the model and the session log? |
| [`react-turn`](diagrams/react-turn.mmd) | What happens inside one streaming turn, step by step? |

### Structure

| Diagram | The question it answers |
|---|---|
| [`surfaces`](diagrams/surfaces.mmd) | Which protocols and schedulers reach the same compiled agent? |
| [`workspace-deps`](diagrams/workspace-deps.mmd) | Which package may import which, and which rule is enforced by a test? |
| [`plugin-seam`](diagrams/plugin-seam.mmd) | How does an optional feature attach without core naming it? |
| [`manifest-resolution`](diagrams/manifest-resolution.mmd) | Which four layers does a manifest name resolve through? |

### State

| Diagram | The question it answers |
|---|---|
| [`session-state`](diagrams/session-state.mmd) | How is chat state stored, and how is it turned back into messages? |
| [`data-model`](diagrams/data-model.mmd) | Which tables hold Felix state, and what relates them? |
| [`durable-run`](diagrams/durable-run.mmd) | Which states does a durable fiber move through, including the retry ladder? |
| [`approval-lifecycle`](diagrams/approval-lifecycle.mmd) | What happens to a tool call that needs a human decision? |

### Operations

| Diagram | The question it answers |
|---|---|
| [`worker-cadence`](diagrams/worker-cadence.mmd) | What runs periodically, on what schedule, and what fires it? |
| [`deploy-topology`](diagrams/deploy-topology.mmd) | What runs where in a deployment, and what does each part need? |
| [`ci-pipeline`](diagrams/ci-pipeline.mmd) | What does CI run on a push, and what decides whether each job runs? |

## Rendering

Sources are checked in; rendered images are not, so there is nothing to go stale between a source
and its PNG. GitHub renders ```` ```mermaid ```` fences inline, and most Markdown viewers render the
`.mmd` files directly. To produce images:

```bash
npx -y @mermaid-js/mermaid-cli@11 -i docs/diagrams/compile-pipeline.mmd -o out.svg
```

## Conventions

Every diagram holds to the same small vocabulary, so the shapes and colors mean the same thing
across the set:

| Element | Means |
|---|---|
| `[Rectangle]` | A process or component in this repo |
| `[(Cylinder)]` | Durable state — Postgres, Valkey, an object store |
| `{Diamond}` | A branch: a config value or a condition decides the path |
| `([Stadium])` | Something outside Felix, or a terminal |
| `[[Subroutine]]` | A step drawn in full in its own diagram |
| Blue fill | The one or two nodes the diagram exists to point at |
| Dashed border | Called but not controlled |

Every diagram carries `accTitle` and `accDescr`. The description is written to be the thing a reader
gets if the image never loads, which is also the fastest way to find out that a diagram has no
point — if it is hard to write, the diagram is unclear.

## Checking a change

Both checks come from the `marmalade` plugin and need no repo tooling:

```bash
P=~/.claude/plugins/cache/marmalade/marmalade/0.1.3/scripts
python3 $P/marmalade_lint.py docs/diagrams   # Mermaid syntax
python3 $P/marmalade_slop.py docs/diagrams --detail balanced   # density, labels, focal set
```

The whole set currently scores 100/100 against the `balanced` budget of 12 nodes. A diagram that
needs more than twelve nodes is usually two diagrams.

# felix-plugin-example

A third-party Felix plugin that touches every extension seam, in one package. Felix discovers it
through the `felix.plugins` entry point in `pyproject.toml`. No change to Felix is needed, and core
never imports it by name. Use it as the starting point for your own primitives: a tool, a pattern,
a model provider, a session strategy.

## Try it

```bash
uv pip install -e examples/felix-plugin-example
make dev
curl -s http://localhost:8080/example/ping
```

To remove it again: `uv pip uninstall felix-plugin-example`.

## What it registers

| Seam | Name | Where it shows up |
|---|---|---|
| Tool | `example__greet` | list it in a manifest's `spec.tools` |
| Agent-loop hook | `_before_tool` | blocks every call to `example__forbidden`, a tool it also registers |
| HTTP route | `GET /example/ping` | mounted beside Felix's own routes |
| Worker task | `example_heartbeat` | a cron task; runs only while `felix-scheduler` and `felix-worker` both run |
| Pattern | `example-echo` | `spec.pattern: example-echo` |
| Model provider | `example-echo` | `FELIX_MODEL_ROUTES={"echo-model":{"provider":"example-echo","model":"echo-1"}}` |
| Object store | `example-null` | `FELIX_OBJECT_STORE=example-null` |
| Session strategy | `example-pairs` | `spec.session.strategy: example-pairs` |
| Manifest config | `spec.extensions.example.greeting` | read by the `example-echo` pattern; core passes it through |

Each seam is a short section of `src/felix_plugin_example/__init__.py`. Its comments say why it is
shaped the way it is. `tests/unit/test_example_plugin.py` loads it straight from disk and exercises each seam.

## Making your own

1. Copy this directory and rename the package and the entry-point name. The group,
   `felix.plugins`, must stay as it is.
2. Keep `register(registry)` as the single entry point. Delete the seams you don't need.
3. Import Felix inside functions, as this package does, so importing the plugin stays cheap.

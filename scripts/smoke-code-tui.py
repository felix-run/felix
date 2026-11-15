"""Built TUI -> real HTTP API -> durable fiber -> approvals -> local shell, no vendor."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import pty
import shutil
import socket
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
KEY = "sk-real-stack-smoke-not-a-secret"
COMMAND = "printf 'real smoke ok\\n' > real-smoke.txt; cat real-smoke.txt"


async def main(directory):
    # No developer .env, inherited configuration or credentials enter this run.
    os.chdir(directory)
    for name in list(os.environ):
        if name.startswith("FELIX_"):
            del os.environ[name]
    import httpx
    import uvicorn
    from felix.durability import fibers as F
    from felix.patterns.model_registry import register_model_provider
    from felix_ai.providers.scripted import ScriptedTurn
    from felix_ai.types import ToolCall
    from tests.support.e2e import (
        DEFAULT_ROUTE,
        ProviderSpy,
        assert_routes_to_the_script,
        scripted_model_routes,
    )
    from tests.support.factories import make_settings

    Path(directory, "AGENTS.md").write_text("Root smoke instructions.")
    Path(directory, "src").mkdir()
    Path(directory, "src", "CLAUDE.md").write_text("Nested smoke instructions.")
    spy = ProviderSpy(
        queue=[
            ScriptedTurn(
                content="",
                tool_calls=[ToolCall(id="real-smoke-info", name="local_info", args={})],
                stop_reason="tool_use",
            ),
            ScriptedTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="real-smoke-instructions", name="local_instructions", args={"path": "src/new.py"}
                    )
                ],
                stop_reason="tool_use",
            ),
            ScriptedTurn(
                content="",
                tool_calls=[ToolCall(id="real-smoke-command", name="local_shell", args={"command": COMMAND})],
                stop_reason="tool_use",
            ),
            ScriptedTurn(content="Real stack smoke passed."),
        ]
    )
    register_model_provider("scripted", spy.factory())
    routes = json.dumps(scripted_model_routes())
    os.environ.update(
        FELIX_DATABASE_URL="memory://smoke",
        FELIX_OBJECT_STORE="memory",
        FELIX_REDIS_URL="",
        FELIX_MODEL_ROUTES=routes,
        FELIX_DEFAULT_MODEL_ID=DEFAULT_ROUTE,
        FELIX_MEMORY_EMBEDDER="none",
    )
    settings = make_settings(
        host="127.0.0.1",
        auth_mode="api_key",
        auth_api_keys=json.dumps(
            {KEY: {"tenant_id": "default", "sub": "smoke-developer", "scopes": ["admin"]}}
        ),
        default_model_id=DEFAULT_ROUTE,
        model_routes=routes,
        model_provider_options="",
        anthropic_api_key="",
        openai_api_key="",
        search_api_key="",
        market_api_key="",
        redis_url="",
        memory_embedder="none",
        fiber_poll_seconds=0.05,
        rate_limit=10000,
    )
    assert_routes_to_the_script(settings)
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(128)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False, timeout_graceful_shutdown=2
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    stop_worker = asyncio.Event()
    worker = asyncio.create_task(F.run_fiber_loop(settings, stop_worker))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
    os.set_blocking(master, False)
    captured = bytearray()
    loop = asyncio.get_running_loop()

    def read_tty():
        try:
            block = os.read(master, 65536)
        except BlockingIOError, OSError:
            return
        captured.extend(block)

    loop.add_reader(master, read_tty)
    process = None

    async def until(predicate, description, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            if process is not None and process.returncode is not None:
                raise AssertionError(f"TUI exited before {description}: {process.returncode}")
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {description}")
            await asyncio.sleep(0.03)

    try:
        await until(lambda: server.started, "API startup")
        env = dict(
            os.environ,
            TERM="xterm-256color",
            XDG_CONFIG_HOME=directory,
            XDG_STATE_HOME=directory,
            FELIX_NO_NOTIFY="1",
            PAGER="cat",
            TMPDIR=directory,
        )
        process = await asyncio.create_subprocess_exec(
            BUN,
            str(BINARY),
            "--origin",
            f"http://127.0.0.1:{port}",
            "--key",
            KEY,
            "--thread",
            "real-tui-smoke",
            "--manifest",
            "code",
            "--shell",
            "Run the local smoke command.",
            cwd=directory,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {KEY}"}, timeout=5
        ) as client:
            # Match the literal key labels in the raw stream; never flatten or
            # infer a visual frame from terminal control sequences.
            await until(lambda: b"y approve" in captured, "real harness approval banner")
            assert not Path(directory, "real-smoke.txt").exists(), "shell ran before harness approval"
            os.write(master, b"y")
            await until(lambda: b"y allow" in captured, "local shell confirmation banner")
            assert not Path(directory, "real-smoke.txt").exists(), "shell ran before local confirmation"
            os.write(master, b"y")
            await until(lambda: Path(directory, "real-smoke.txt").exists(), "native command write")

            def completed():
                return any(row.get("status") == "completed" for row in F._memory_fibers.values())

            await until(completed, "durable run completion")
            row = next(row for row in F._memory_fibers.values() if row.get("status") == "completed")
            response = await client.get(f"/chat/runs/{row['id']}")
            assert response.status_code == 200, response.text
            run = response.json()
            assert run["final"]["content"] == "Real stack smoke passed.", run
            assert Path(directory, "real-smoke.txt").read_text() == "real smoke ok\n"
            approvals = (
                await client.get(
                    "/approvals", params={"thread_id": "default:real-tui-smoke", "status": "approved"}
                )
            ).json()
            rows = approvals.get("approvals") or approvals.get("items") or []
            assert any(
                row.get("status") == "approved" and row.get("tool_name") == "local_shell" for row in rows
            ), approvals
            assert "real smoke ok" in str(spy.prompts[-1]), "shell output did not reach the real agent loop"
            info_text = next(message.content for message in spy.prompts[1] if '{"client"' in message.content)
            capabilities, _ = json.JSONDecoder().raw_decode(info_text[info_text.index('{"client"') :])
            assert capabilities["shell"]["mode"] == "native", capabilities
            assert capabilities["workspace"]["root"] == str(Path(directory).resolve()), capabilities
            instruction_text = next(
                message.content for message in spy.prompts[2] if '{"target"' in message.content
            )
            instructions, _ = json.JSONDecoder().raw_decode(
                instruction_text[instruction_text.index('{"target"') :]
            )
            assert instructions["complete"], instructions
            assert [file["content"] for file in instructions["instructions"]] == [
                "Root smoke instructions.",
                "Nested smoke instructions.",
            ], instructions
            assert len(spy.prompts) == 4, f"unexpected model calls: {len(spy.prompts)}"
            await until(lambda: b"Real stack smoke passed." in captured, "final answer on terminal")
            await until(lambda: b"Workspace results" in captured, "coding result summary on terminal")
            await asyncio.sleep(0.2)
            os.write(master, b"/work\r")
            await until(lambda: b"loaded thread history" in captured, "thread coding report in pager")
            await asyncio.sleep(0.2)
            os.write(master, b"/quit\r")
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode == 0, process.returncode
            print(
                json.dumps(
                    {
                        "passed": True,
                        "real_http_api": True,
                        "real_durable_worker": True,
                        "harness_approval": True,
                        "local_confirmation": True,
                        "file_content_verified": True,
                        "tool_result_reached_model": True,
                        "client_capabilities_verified": True,
                        "repository_instructions_verified": True,
                        "coding_summary_verified": True,
                        "coding_work_report_verified": True,
                        "model_calls": len(spy.prompts),
                        "vendor_calls": 0,
                        "tui_exit_code": process.returncode,
                    }
                )
            )
    except BaseException:
        print("TTY diagnostic tail:", repr(bytes(captured[-600:])))
        raise
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode is None:
                process.kill()
                await process.wait()
        loop.remove_reader(master)
        os.close(master)
        stop_worker.set()
        await worker
        server.should_exit = True
        await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tui",
        type=Path,
        default=REPO.parent / "felix-web/apps/tui/dist/felix.js",
        help="Built companion TUI (default: sibling felix-web checkout)",
    )
    args = parser.parse_args()
    BINARY = args.tui.resolve()
    BUN = shutil.which("bun")
    if not BUN:
        parser.error("Bun must be installed to run the terminal client")
    if not BINARY.is_file():
        parser.error(f"{BINARY} is missing; run pnpm tui:build in felix-web first")
    with tempfile.TemporaryDirectory(prefix="felix-real-code-smoke-") as directory:
        asyncio.run(main(directory))

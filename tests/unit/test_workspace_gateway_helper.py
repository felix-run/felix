"""The workspace gateway's `felix-fs` helper, against a real directory.

The helper (`deploy/cloudflare/workspace-gateway/helper/felix_fs.py`) does the file operations inside
a hosted workspace's sandbox, with the local backend's rules: these hold it to them one operation at a
time, and `test_the_ported_functions_are_the_harness_functions` holds its copied code to the source it
was copied from, so the two backends cannot drift apart silently.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "deploy/cloudflare/workspace-gateway/helper/felix_fs.py"
_spec = importlib.util.spec_from_file_location("felix_fs", HELPER)
assert _spec is not None and _spec.loader is not None
felix_fs = importlib.util.module_from_spec(_spec)
sys.modules["felix_fs"] = felix_fs
_spec.loader.exec_module(felix_fs)


class HelperCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self._root = felix_fs.ROOT
        felix_fs.ROOT = self.ws

    def tearDown(self) -> None:
        felix_fs.ROOT = self._root
        self._tmp.cleanup()

    def call(self, op: str, **req: object) -> dict:
        return felix_fs.run({"op": op, **req})

    def ok(self, op: str, **req: object) -> dict:
        out = self.call(op, **req)
        self.assertTrue(out["ok"], out)
        return out["result"]

    def refused(self, op: str, code: str, **req: object) -> str:
        out = self.call(op, **req)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"], code, out)
        return out["message"]


class ListTests(HelperCase):
    def test_entries_are_sorted_case_folded_with_sizes_and_links_named(self) -> None:
        (self.ws / "b.txt").write_text("bb")
        (self.ws / "A.txt").write_text("a")
        (self.ws / "dir").mkdir()
        os.symlink("/etc", self.ws / "link")
        out = self.ok("list", path=".")
        self.assertEqual(out["path"], ".")
        self.assertEqual(
            out["entries"],
            [
                {"path": "A.txt", "type": "file", "size": 1},
                {"path": "b.txt", "type": "file", "size": 2},
                {"path": "dir", "type": "dir"},
                {"path": "link", "type": "symlink"},
            ],
        )

    def test_a_missing_directory_is_not_found_and_a_file_is_not_a_directory(self) -> None:
        (self.ws / "f").write_text("x")
        self.refused("list", "not_found", path="nope")
        self.refused("list", "not_a_directory", path="f")


class ReadTests(HelperCase):
    def test_a_window_and_the_size(self) -> None:
        (self.ws / "f.txt").write_text("hello world")
        out = self.ok("read", path="f.txt", offset=6, limit=3)
        self.assertEqual((out["path"], out["size"], base64.b64decode(out["data"])), ("f.txt", 11, b"wor"))
        past = self.ok("read", path="f.txt", offset=50, limit=10)
        self.assertEqual(base64.b64decode(past["data"]), b"")

    def test_bytes_come_back_as_they_are(self) -> None:
        (self.ws / "bin").write_bytes(b"\xff\x00\r\n")
        self.assertEqual(base64.b64decode(self.ok("read", path="bin")["data"]), b"\xff\x00\r\n")

    def test_a_directory_is_not_a_file_and_an_absent_one_is_not_found(self) -> None:
        (self.ws / "d").mkdir()
        self.assertEqual(self.refused("read", "not_a_file", path="d"), "d")
        self.refused("read", "not_found", path="nope.txt")

    def test_a_symlink_is_refused_not_followed(self) -> None:
        secret = self.base / "secret.txt"
        secret.write_text("outside")
        os.symlink(secret, self.ws / "s.txt")
        (self.ws / "d").mkdir()
        os.symlink(self.base, self.ws / "d" / "up")
        for path in ("s.txt", "d/up/secret.txt"):
            message = self.refused("read", "invalid_path", path=path)
            self.assertIn("symlinks are not followed", message)

    def test_escaping_and_absolute_paths_are_refused(self) -> None:
        self.assertEqual(self.refused("read", "invalid_path", path="../x"), "path escapes workspace root")
        self.assertEqual(
            self.refused("read", "invalid_path", path="/etc/passwd"), "absolute paths are not allowed"
        )

    def test_a_window_over_the_cap_is_refused(self) -> None:
        (self.ws / "f").write_text("x")
        self.refused("read", "invalid_path", path="f", limit=felix_fs._MAX_READ_BYTES + 1)


class WriteTests(HelperCase):
    def _data(self, text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    def test_creates_parents_overwrites_and_appends(self) -> None:
        out = self.ok("write", path="a/b/c.txt", data=self._data("one"))
        self.assertEqual(out, {"path": "a/b/c.txt", "bytes": 3})
        self.ok("write", path="a/b/c.txt", data=self._data("two"))
        self.ok("write", path="a/b/c.txt", data=self._data("+"), append=True)
        self.assertEqual((self.ws / "a/b/c.txt").read_text(), "two+")

    def test_writes_through_no_symlink(self) -> None:
        target = self.base / "outside.txt"
        os.symlink(target, self.ws / "s.txt")
        self.refused("write", "invalid_path", path="s.txt", data=self._data("x"))
        self.assertFalse(target.exists())

    def test_a_directory_is_refused_as_the_local_backend_refuses_it(self) -> None:
        (self.ws / "d").mkdir()
        # The open fails before the regular-file check, as it does locally: an internal error.
        out = self.call("write", path="d", data=self._data("x"))
        self.assertEqual((out["error"], out["kind"]), ("io_error", "IsADirectoryError"))
        self.assertTrue(out["message"].startswith("[Errno 21]"), out)
        self.refused("write", "not_a_file", path=".", data=self._data("x"))


class EditTests(HelperCase):
    def test_replaces_exactly_and_keeps_bytes_and_mode(self) -> None:
        f = self.ws / "run.sh"
        f.write_bytes(b"echo old\r\nkeep\r\n")
        f.chmod(0o755)
        out = self.ok("edit", path="run.sh", old="old", new="new")
        self.assertEqual(out, {"path": "run.sh", "replacements": 1, "bytes": 16})
        self.assertEqual(f.read_bytes(), b"echo new\r\nkeep\r\n")
        self.assertEqual(f.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in self.ws.iterdir()], ["run.sh"], "no temporary file is left")

    def test_the_local_backends_refusals_word_for_word(self) -> None:
        (self.ws / "a.txt").write_text("x x")
        (self.ws / "bin").write_bytes(b"\xff")
        self.assertEqual(
            self.refused("edit", "edit_refused", path="a.txt", old="y", new="z"),
            "old_string not found in a.txt",
        )
        self.assertEqual(
            self.refused("edit", "edit_refused", path="a.txt", old="x", new="x"),
            "old_string and new_string are identical in a.txt",
        )
        self.assertEqual(
            self.refused("edit", "edit_refused", path="a.txt", old="x", new="y"),
            "old_string appears 2 times in a.txt — extend it with surrounding lines until it is unique, "
            "or pass replace_all",
        )
        self.assertEqual(
            self.refused("edit", "edit_refused", path="bin", old="a", new="b"), "not UTF-8 text: bin"
        )
        self.assertEqual(self.ok("edit", path="a.txt", old="x", new="y", replace_all=True)["replacements"], 2)

    def test_a_missing_file_is_not_found(self) -> None:
        self.refused("edit", "not_found", path="nope.txt", old="a", new="b")


class SearchTests(HelperCase):
    def test_literal_and_regex_in_walk_order(self) -> None:
        (self.ws / "b.txt").write_text("needle two\n")
        (self.ws / "a").mkdir()
        (self.ws / "a" / "x.txt").write_text("first needle\nnothing\n")
        hits = self.ok("search", query="needle", path=".")["hits"]
        self.assertEqual([(h["path"], h["line"]) for h in hits], [("a/x.txt", 1), ("b.txt", 1)])
        hits = self.ok("search", query=r"needle\s+two", regex=True)["hits"]
        self.assertEqual([h["path"] for h in hits], ["b.txt"])

    def test_does_not_descend_into_or_read_through_a_link(self) -> None:
        outside = self.base / "out"
        outside.mkdir()
        (outside / "s.txt").write_text("needle")
        os.symlink(outside, self.ws / "link")
        self.assertEqual(self.ok("search", query="needle")["hits"], [])

    def test_an_invalid_regex_is_a_bad_request(self) -> None:
        self.assertIn("invalid regex", self.refused("search", "bad_request", query="(", regex=True))

    def test_the_hit_cap_holds(self) -> None:
        (self.ws / "f.txt").write_text("needle\n" * 30)
        self.assertEqual(len(self.ok("search", query="needle", max_hits=5)["hits"]), 5)


class GitTests(HelperCase):
    """The `git` op is `_git_exec` in /workspace: stdout from the start, base64, capped."""

    def git(self, *args: str, **req: object) -> dict:
        return self.ok("git", args=["-c", "user.name=t", "-c", "user.email=t@t", *args], **req)

    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q", "-b", "main")
        (self.ws / "a.txt").write_text("one\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "first")

    def test_output_exit_code_and_stdin(self) -> None:
        out = self.git("log", "--format=%s")
        self.assertEqual(
            (base64.b64decode(out["out"]), out["code"], out["truncated"]), (b"first\n", 0, False)
        )
        hashed = self.git("hash-object", "--stdin", stdin=base64.b64encode(b"hi\n").decode())
        self.assertEqual(len(base64.b64decode(hashed["out"]).strip()), 40)
        self.assertNotEqual(self.git("rev-parse", "--verify", "nope")["code"], 0)

    def test_a_limit_cuts_stdout_and_says_so(self) -> None:
        out = self.git("log", "--format=%H", limit=5)
        self.assertEqual((len(base64.b64decode(out["out"])), out["truncated"]), (5, True))

    def test_bad_arguments_are_refused(self) -> None:
        self.refused("git", "bad_request", args=[])
        self.refused("git", "bad_request", args=["status"], limit=0)


class LstatTests(HelperCase):
    def test_sizes_links_and_absent_paths(self) -> None:
        (self.ws / "f.txt").write_text("12345")
        os.symlink("/etc/passwd", self.ws / "link")
        out = self.ok("lstat", paths=["f.txt", "link", "gone"])
        self.assertEqual(
            out["stats"], [{"kind": "file", "size": 5}, {"kind": "symlink", "size": len("/etc/passwd")}, None]
        )

    def test_escaping_and_absolute_paths_are_refused(self) -> None:
        self.refused("lstat", "invalid_path", paths=["../x"])
        self.refused("lstat", "invalid_path", paths=["/etc/passwd"])


class ProtocolTests(HelperCase):
    def test_main_answers_one_request_from_stdin(self) -> None:
        (self.ws / "f.txt").write_text("hi")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = felix_fs.main(io.BytesIO(json.dumps({"op": "read", "path": "f.txt"}).encode()))
        self.assertEqual(code, 0)
        self.assertEqual(base64.b64decode(json.loads(buf.getvalue())["result"]["data"]), b"hi")

    def test_a_request_that_is_not_an_object_is_a_bad_request(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            felix_fs.main(io.BytesIO(b"[1, 2]"))
        self.assertEqual(json.loads(buf.getvalue())["error"], "bad_request")

    def test_an_unknown_op_is_a_bad_request(self) -> None:
        self.refused("rm", "bad_request")


class PortedCodeTests(unittest.TestCase):
    """The helper's copied functions are the harness's, compared as syntax trees.

    A tree, not text, so formatting and docstrings may differ. Anything else that differs fails here, which is the point -- the hosted and local backends are
    meant to be one implementation of the rules.
    """

    SOURCES = (
        "packages/harness/src/felix/tools/workspace.py",
        "packages/harness/src/felix/tools/workspace_local.py",
        "packages/harness/src/felix/tools/workspace_backend.py",
        "packages/harness/src/felix/tools/shell.py",
        "packages/harness/src/felix/tools/github_publish.py",
    )
    PORTED = (
        "SymlinkRefusedError",
        "workspace_parts",
        "open_at",
        "_open_root",
        "open_workspace_parent",
        "open_workspace_dir",
        "NotAFileError",
        "open_regular",
        "_pread",
        "_dir_batch",
        "_by_name",
        "_write_all",
        "_child_rel",
        "_read_window",
        "_create_edit_temp",
        "_replace_file",
        "_scan_text",
        "_scan_file",
        "_scan_tree",
        "_search",
        # The file pane's delete and rename, which the helper answers in one process so the compare
        # and the change cannot be split by another call to the sandbox.
        "WorkspaceChanged",
        "_current_state",
        "_source_state",
        "_still_regular",
        "_delete_checked",
        "_rename_checked",
        # shell.py: the one exec path, so a sandboxed command is bounded and killed the same way.
        # `_child_env` is deliberately not here: the sandbox has no harness environment to scrub.
        "_Stream",
        "_Budget",
        "_kill_group",
        "_drain",
        "_feed",
        "resolve_cwd",
        "exec_argv",
        # github_publish.py: the git a listing and `publish_commits` read a sandboxed repository
        # with. `_git_run` is not here: on the harness it is the dispatcher that sends a hosted
        # repository's calls to the gateway, and `_git_exec` is the half that runs git.
        "_git_env",
        "_GitResult",
        "_git_exec",
    )

    @staticmethod
    def _defs(path: Path) -> dict[str, str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                # Docstrings are the harness's to word; the code under them is what must agree.
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    node.body = body[1:] or [ast.Pass()]
                out[node.name] = ast.dump(node, annotate_fields=False, include_attributes=False)
        return out

    def test_the_ported_functions_are_the_harness_functions(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        source: dict[str, str] = {}
        for rel in self.SOURCES:
            source.update(self._defs(repo / rel))
        helper = self._defs(HELPER)
        drifted = [name for name in self.PORTED if helper.get(name) != source.get(name)]
        self.assertEqual(drifted, [], "edit the helper's copy to match, or say here why it differs")

    def test_the_helpers_constants_are_the_harness_constants(self) -> None:
        from felix.tools import workspace

        for name in (
            "_MAX_READ_BYTES",
            "_MAX_WRITE_BYTES",
            "_MAX_EDIT_FILE_BYTES",
            "_MAX_LIST_ENTRIES",
            "_MAX_SEARCH_HITS",
            "_MAX_SEARCH_FILE_BYTES",
            "_MAX_QUERY_CHARS",
            "_MAX_SEARCH_LINE_CHARS",
            "_SEARCH_BUDGET_S",
            "_MAX_SEARCH_DEPTH",
            "_MAX_DIR_BATCH",
            "_EDIT_TMP_PREFIX",
        ):
            self.assertEqual(getattr(felix_fs, name), getattr(workspace, name), name)

    def test_the_helpers_exec_bounds_are_the_shell_tools(self) -> None:
        from felix.manifests.schema import MAX_INTEGRATION_TIMEOUT_MS
        from felix.tools import shell

        for name in (
            "MAX_OUTPUT_BYTES",
            "MAX_TOTAL_OUTPUT_BYTES",
            "_READ_CHUNK",
            "_MAX_STDIN_CHARS",
            "_DRAIN_AFTER_KILL_S",
        ):
            self.assertEqual(getattr(felix_fs, name), getattr(shell, name), name)
        self.assertEqual(felix_fs._MAX_EXEC_TIMEOUT_MS, MAX_INTEGRATION_TIMEOUT_MS)

    def test_the_helpers_git_is_the_publish_tools(self) -> None:
        from felix.tools import github_publish

        for name in ("_GIT_TIMEOUT_S", "_GIT_OUTPUT_CAP", "_GIT_PRELUDE"):
            self.assertEqual(getattr(felix_fs, name), getattr(github_publish, name), name)

"""The filesystem object store does its file I/O off the event loop, and still refuses bad keys."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from felix.config import Settings
from felix.storage.fs import FilesystemObjectStore


def _store(tmp_path: Path) -> FilesystemObjectStore:
    return FilesystemObjectStore(Settings(database_url="memory://fs-store", object_store_path=str(tmp_path)))


async def test_every_operation_runs_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = FilesystemObjectStore._path

    def spy(self: FilesystemObjectStore, key: str) -> Path:
        seen.append(threading.get_ident())
        return real(self, key)

    monkeypatch.setattr(FilesystemObjectStore, "_path", spy)
    # The I/O itself, not only the path resolution: a `get` that resolved in a thread and read
    # on the loop would pass a check on `_path` alone.
    for name in ("read_bytes", "write_bytes", "is_file", "unlink"):
        original = getattr(Path, name)

        def watched(self: Path, *a: object, _original=original, **kw: object) -> object:
            seen.append(threading.get_ident())
            return _original(self, *a, **kw)

        monkeypatch.setattr(Path, name, watched)
    await store.put("a/b.txt", b"hi")
    assert await store.exists("a/b.txt")
    assert await store.get("a/b.txt") == b"hi"
    await store.delete("a/b.txt")
    assert await store.get("a/b.txt") is None
    assert len(seen) > 5 and loop_thread not in seen


async def test_a_traversal_key_is_still_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for op in (
        store.get("../escape"),
        store.exists("a/../../x"),
        store.put("..", b"x"),
        store.delete("/../y"),
    ):
        with pytest.raises(ValueError, match="invalid object key"):
            await op

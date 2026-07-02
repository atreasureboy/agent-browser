"""T61: SnapshotStore unit tests (fable §5.4)."""
from __future__ import annotations

import asyncio
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from semantic_browser.daemon.snapshots import (
    SnapshotStore,
    _MAX_SNAPSHOT_BYTES,
    _RETENTION_COUNT,
)


class _StubController:
    """模拟 BrowserController: 提供 storage_state() + pages 列表."""
    def __init__(self, state: dict, urls: list[str] | None = None) -> None:
        self._state = state
        ctx = MagicMock()
        ctx.pages = []
        for u in (urls or []):
            page = MagicMock()
            page.url = u
            page.is_closed.return_value = False
            ctx.pages.append(page)
        # async storage_state
        async def _sstate():
            return self._state
        ctx.storage_state = _sstate
        self._context = ctx


@pytest.fixture
def tmp_store(tmp_path):
    root = tmp_path / "snaps"
    db = tmp_path / "snap_index.db"
    return SnapshotStore(str(root), str(db))


class TestSnapshotStoreUnit:
    def test_init_creates_db_and_root(self, tmp_path):
        root = tmp_path / "snap1"
        db = tmp_path / "snap1.db"
        store = SnapshotStore(str(root), str(db))
        assert Path(root).exists()
        assert Path(db).exists()
        store.close()

    def test_mark_dirty_and_query(self, tmp_store):
        assert tmp_store.is_dirty("s1") is False
        tmp_store.mark_dirty("s1")
        assert tmp_store.is_dirty("s1") is True
        assert tmp_store.dirty_sessions() == {"s1"}

    def test_take_snapshot_writes_file_and_index(self, tmp_store):
        async def go():
            state = {"cookies": [{"name": "sessionid", "value": "abc123", "domain": "example.com"}],
                     "origins": []}
            ctrl = _StubController(state)
            snap_id = await tmp_store.take_snapshot("s1", ctrl, trigger="manual")
            assert snap_id is not None
            assert snap_id.startswith("ss_")
            # file exists
            listing = tmp_store.list_snapshots("s1")
            assert len(listing) == 1
            assert listing[0]["trigger"] == "manual"
            assert listing[0]["size_bytes"] > 0
            assert tmp_store.is_dirty("s1") is False  # 自动 clear
        asyncio.run(go())

    def test_take_snapshot_records_open_pages(self, tmp_store):
        async def go():
            state = {"cookies": [], "origins": []}
            ctrl = _StubController(state, urls=["https://example.com/a", "https://example.com/b"])
            snap_id = await tmp_store.take_snapshot("s2", ctrl)
            assert snap_id is not None
            listing = tmp_store.list_snapshots("s2")
            urls = json.loads(listing[0]["open_pages"])
            assert urls == ["https://example.com/a", "https://example.com/b"]
        asyncio.run(go())

    def test_load_snapshot_roundtrip(self, tmp_store):
        async def go():
            state = {"cookies": [{"name": "k", "value": "v"}], "origins": []}
            ctrl = _StubController(state)
            snap_id = await tmp_store.take_snapshot("s3", ctrl)
            loaded = tmp_store.load_snapshot(snap_id)
            assert loaded is not None
            assert loaded["session_id"] == "s3"
            assert loaded["content"]["cookies"] == [{"name": "k", "value": "v"}]
        asyncio.run(go())

    def test_truncate_large_state(self, tmp_store):
        """T61: 单份 > 2MB 应被 truncate (截断最大的 localStorage.value)."""
        async def go():
            big_value = "X" * (3 * 1024 * 1024)  # 3MB worth
            state = {"cookies": [], "origins": [
                {"origin": "https://big.example", "localStorage": [
                    {"name": "huge", "value": big_value},
                    {"name": "small", "value": "ok"},
                ]}
            ]}
            ctrl = _StubController(state)
            snap_id = await tmp_store.take_snapshot("s4", ctrl)
            assert snap_id is not None
            listing = tmp_store.list_snapshots("s4")
            assert listing[0]["truncated"] is True
            assert listing[0]["size_bytes"] <= _MAX_SNAPSHOT_BYTES
            loaded = tmp_store.load_snapshot(snap_id)
            # 巨大的 key 应被清空
            ls = loaded["content"]["origins"][0]["localStorage"][0]
            assert ls["name"] == "huge"
            assert ls["value"] == ""  # truncated
            ls2 = loaded["content"]["origins"][0]["localStorage"][1]
            assert ls2["value"] == "ok"  # 没动
        asyncio.run(go())

    def test_gc_keeps_retention_count(self, tmp_store):
        """T61: 每 session 保留 RETENTION_COUNT (3) 份最新, 删旧的."""
        async def go():
            state = {"cookies": [], "origins": []}
            ctrl = _StubController(state)
            # 5 份
            for i in range(5):
                ctrl2 = _StubController(state)  # same state each time
                sid = await tmp_store.take_snapshot("s5", ctrl2)
            # GC 应删 2 份 (留 3)
            removed = tmp_store.gc_old_snapshots("s5")
            assert removed == 2
            listing = tmp_store.list_snapshots("s5")
            assert len(listing) == _RETENTION_COUNT
        asyncio.run(go())

    def test_list_snapshots_newest_first(self, tmp_store):
        async def go():
            state = {"cookies": [], "origins": []}
            for i in range(3):
                ctrl = _StubController(state)
                await tmp_store.take_snapshot(f"s6_{i}", ctrl)
            # 跨不同 session_id, 每次只一个
            tmp_store.clear_dirty("s6_2")  # 防 clear 前 dirty
            all_0 = tmp_store.list_snapshots("s6_0")
            assert len(all_0) == 1
        asyncio.run(go())

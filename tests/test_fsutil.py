from __future__ import annotations

import pytest

from feishu_gcal_sync import fsutil
from feishu_gcal_sync.fsutil import ReservedFile


def test_reserved_file_replaces_target(tmp_path):
    target = tmp_path / "token.json"
    target.write_text("old")
    with ReservedFile(target) as slot:
        slot.commit("new")
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]


def test_unused_reservation_leaves_nothing(tmp_path):
    target = tmp_path / "token.json"
    with pytest.raises(RuntimeError), ReservedFile(target):
        raise RuntimeError("request failed")
    assert list(tmp_path.iterdir()) == []


def test_failed_commit_keeps_the_new_content_for_recovery(tmp_path, monkeypatch):
    target = tmp_path / "token.json"
    target.write_text("old")
    with ReservedFile(target) as slot:
        monkeypatch.setattr(
            fsutil.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(5, "EIO"))
        )
        with pytest.raises(OSError):
            slot.commit("new")
    monkeypatch.undo()
    leftovers = [p for p in tmp_path.iterdir() if p.name != "token.json"]
    assert [p.read_text() for p in leftovers] == ["new"]
    assert target.read_text() == "old"

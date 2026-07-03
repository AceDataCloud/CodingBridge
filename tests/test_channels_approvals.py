"""Tests for the cross-process tool-approval store."""

from __future__ import annotations

import json
import time

import pytest

from coding_bridge.channels.approvals import ApprovalStore


def test_create_list_decide_poll_cleanup(tmp_path):
    store = ApprovalStore(tmp_path / "appr")
    store.create("abc123", {"tool": "Bash", "input_preview": "git status"})

    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == "abc123"
    assert pending[0]["tool"] == "Bash"
    assert "created_at" in pending[0]

    assert store.poll_decision("abc123") is None
    assert store.decide("abc123", "allow") is True
    assert store.poll_decision("abc123") == "allow"
    # once decided it is no longer surfaced as pending
    assert store.list_pending() == []

    store.cleanup("abc123")
    assert store.poll_decision("abc123") is None


def test_decide_unknown_request_is_false(tmp_path):
    store = ApprovalStore(tmp_path / "appr")
    assert store.decide("never-created", "allow") is False


def test_decide_rejects_bad_verdict(tmp_path):
    store = ApprovalStore(tmp_path / "appr")
    store.create("r1", {})
    assert store.decide("r1", "maybe") is False
    assert store.poll_decision("r1") is None


def test_invalid_ids_are_rejected(tmp_path):
    store = ApprovalStore(tmp_path / "appr")
    assert store.valid_id("../escape") is False
    assert store.valid_id("a/b") is False
    assert store.valid_id("ok_ID-123") is True
    with pytest.raises(ValueError):
        store.create("../escape", {})
    assert store.decide("../escape", "allow") is False
    assert store.poll_decision("../escape") is None


def test_stale_pending_not_listed(tmp_path):
    root = tmp_path / "appr"
    root.mkdir()
    (root / "old.request.json").write_text(
        json.dumps({"id": "old", "created_at": time.time() - 99999}), encoding="utf-8"
    )
    store = ApprovalStore(root, ttl=600.0)
    assert store.list_pending() == []


def test_list_pending_sorted_oldest_first(tmp_path):
    store = ApprovalStore(tmp_path / "appr")
    store.create("first", {})
    time.sleep(0.01)
    store.create("second", {})
    assert [p["id"] for p in store.list_pending()] == ["first", "second"]

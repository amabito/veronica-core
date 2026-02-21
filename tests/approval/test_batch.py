"""Tests for ApprovalBatcher (Task H)."""
from __future__ import annotations

import threading

import pytest

from veronica_core.approval.batch import ApprovalBatcher, BatchedRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batcher(**kwargs) -> ApprovalBatcher:
    return ApprovalBatcher(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArgsHash:
    def test_same_inputs_produce_same_hash(self) -> None:
        h1 = ApprovalBatcher.compute_args_hash("RULE", "file_write", ["a.py"])
        h2 = ApprovalBatcher.compute_args_hash("RULE", "file_write", ["a.py"])
        assert h1 == h2

    def test_different_args_produce_different_hash(self) -> None:
        h1 = ApprovalBatcher.compute_args_hash("RULE", "file_write", ["a.py"])
        h2 = ApprovalBatcher.compute_args_hash("RULE", "file_write", ["b.py"])
        assert h1 != h2

    def test_different_action_produces_different_hash(self) -> None:
        h1 = ApprovalBatcher.compute_args_hash("RULE", "file_write", ["x"])
        h2 = ApprovalBatcher.compute_args_hash("RULE", "shell_exec", ["x"])
        assert h1 != h2

    def test_different_rule_produces_different_hash(self) -> None:
        h1 = ApprovalBatcher.compute_args_hash("RULE_A", "file_write", ["x"])
        h2 = ApprovalBatcher.compute_args_hash("RULE_B", "file_write", ["x"])
        assert h1 != h2

    def test_hash_is_64_hex_chars(self) -> None:
        h = ApprovalBatcher.compute_args_hash("R", "a", ["x"])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestAddAndBatching:
    def test_first_add_creates_batch_with_count_1(self) -> None:
        batcher = _batcher()
        batch = batcher.add("RULE", "file_write", ["a.py"])
        assert batch.count == 1
        assert batch.rule_id == "RULE"
        assert batch.action == "file_write"

    def test_second_add_increments_count(self) -> None:
        batcher = _batcher()
        batcher.add("RULE", "file_write", ["a.py"])
        batch = batcher.add("RULE", "file_write", ["a.py"])
        assert batch.count == 2

    def test_different_args_creates_separate_batch(self) -> None:
        batcher = _batcher()
        b1 = batcher.add("RULE", "file_write", ["a.py"])
        b2 = batcher.add("RULE", "file_write", ["b.py"])
        assert b1.args_hash != b2.args_hash
        assert len(batcher.pending()) == 2

    def test_on_batch_ready_called_for_first_only(self) -> None:
        calls: list[BatchedRequest] = []
        batcher = _batcher(on_batch_ready=calls.append)
        batcher.add("RULE", "file_write", ["a.py"])
        batcher.add("RULE", "file_write", ["a.py"])
        batcher.add("RULE", "file_write", ["a.py"])
        assert len(calls) == 1

    def test_on_batch_ready_called_for_each_unique_group(self) -> None:
        calls: list[BatchedRequest] = []
        batcher = _batcher(on_batch_ready=calls.append)
        batcher.add("RULE", "file_write", ["a.py"])
        batcher.add("RULE", "file_write", ["b.py"])
        assert len(calls) == 2


class TestGetClearPending:
    def test_get_returns_batch(self) -> None:
        batcher = _batcher()
        batch = batcher.add("RULE", "shell", ["ls"])
        fetched = batcher.get(batch.args_hash)
        assert fetched is batch

    def test_get_unknown_hash_returns_none(self) -> None:
        batcher = _batcher()
        assert batcher.get("0" * 64) is None

    def test_clear_removes_batch(self) -> None:
        batcher = _batcher()
        batch = batcher.add("RULE", "shell", ["ls"])
        batcher.clear(batch.args_hash)
        assert batcher.get(batch.args_hash) is None

    def test_pending_returns_all_batches(self) -> None:
        batcher = _batcher()
        batcher.add("RULE", "file_write", ["a.py"])
        batcher.add("RULE", "file_write", ["b.py"])
        batcher.add("RULE", "shell", ["ls"])
        assert len(batcher.pending()) == 3

    def test_pending_empty_initially(self) -> None:
        batcher = _batcher()
        assert batcher.pending() == []


class TestThreadSafety:
    def test_concurrent_adds_count_correctly(self) -> None:
        batcher = _batcher()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                batcher.add("RULE", "file_write", ["shared.py"])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        batches = batcher.pending()
        assert len(batches) == 1
        assert batches[0].count == 50

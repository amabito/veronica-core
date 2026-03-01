"""Thread-safety tests for AuditLog under concurrent writes (S-3).

Verifies that the hash chain remains valid and no entries are lost
when 10 threads each append 100 entries simultaneously.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from veronica_core.audit.log import AuditLog


class TestAuditLogThreadSafety:
    """AuditLog must maintain chain integrity under concurrent writes."""

    def test_concurrent_writes_produce_valid_hash_chain(self, tmp_path: Path) -> None:
        """GIVEN 10 threads each appending 100 entries simultaneously,
        WHEN all threads complete,
        THEN verify_chain() returns True (no chain corruption).
        """
        log_path = tmp_path / "concurrent.jsonl"
        log = AuditLog(path=log_path)

        num_threads = 10
        entries_per_thread = 100
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(entries_per_thread):
                    log.write("CONCURRENT_EVENT", {"thread": thread_id, "seq": i})
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Writer threads raised errors: {errors}"
        assert log.verify_chain() is True, "Hash chain must be valid after concurrent writes"

    def test_concurrent_writes_no_lost_entries(self, tmp_path: Path) -> None:
        """GIVEN 10 threads each appending 100 entries simultaneously,
        WHEN all threads complete,
        THEN exactly 1000 entries are present in the log file (no lost writes).
        """
        log_path = tmp_path / "no_loss.jsonl"
        log = AuditLog(path=log_path)

        num_threads = 10
        entries_per_thread = 100

        def writer(thread_id: int) -> None:
            for i in range(entries_per_thread):
                log.write("ENTRY", {"thread": thread_id, "seq": i})

        threads = [
            threading.Thread(target=writer, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = [
            line for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        expected_count = num_threads * entries_per_thread
        assert len(lines) == expected_count, (
            f"Expected {expected_count} entries, got {len(lines)} (lost writes detected)"
        )

    def test_concurrent_writes_chain_valid_after_reopen(self, tmp_path: Path) -> None:
        """GIVEN concurrent writes from 5 threads,
        WHEN a fresh AuditLog instance opens the same file,
        THEN verify_chain() returns True on the reopened instance.
        """
        log_path = tmp_path / "reopen_check.jsonl"
        log = AuditLog(path=log_path)

        num_threads = 5
        entries_per_thread = 50

        def writer(thread_id: int) -> None:
            for i in range(entries_per_thread):
                log.write("THREAD_EVENT", {"tid": thread_id, "i": i})

        threads = [
            threading.Thread(target=writer, args=(t,)) for t in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Reopen
        reopened = AuditLog(path=log_path)
        assert reopened.verify_chain() is True, (
            "Reopened log must pass chain verification after concurrent writes"
        )

    def test_two_concurrent_writers_no_duplicate_hash_links(self, tmp_path: Path) -> None:
        """GIVEN two threads writing simultaneously,
        WHEN both finish,
        THEN no two consecutive entries share the same prev_hash (no race in chain linking).
        """
        log_path = tmp_path / "dedup_chain.jsonl"
        log = AuditLog(path=log_path)

        barrier = threading.Barrier(2)

        def writer(thread_id: int) -> None:
            barrier.wait()
            for i in range(50):
                log.write("RACE_TEST", {"t": thread_id, "i": i})

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = [
            line for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 100

        # Verify prev_hash chain is sequential (no two entries share same prev_hash)
        # Each prev_hash should appear exactly once as a "previous" pointer
        # (except genesis "000...0" which is the first)
        assert log.verify_chain() is True, "Chain must be valid with no race conditions"

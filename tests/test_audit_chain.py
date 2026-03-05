"""Tests for AuditChain -- tamper-proof hash chain for safety events."""

from __future__ import annotations

import copy
import threading

import pytest

from veronica_core.compliance.audit_chain import (
    AuditChain,
    AuditEntry,
    _GENESIS_HASH,
    _compute_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_clock(start: float = 1000.0, step: float = 1.0):
    """Deterministic clock for testing."""
    t = [start]

    def tick() -> float:
        now = t[0]
        t[0] += step
        return now

    return tick


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


class TestAuditChainBasics:
    def test_empty_chain_verifies(self) -> None:
        chain = AuditChain()
        assert chain.verify()
        assert len(chain) == 0
        assert chain.entries() == []

    def test_single_entry(self) -> None:
        chain = AuditChain(clock=_make_clock())
        entry = chain.append({"event": "HALT", "reason": "budget"})
        assert entry.sequence == 0
        assert entry.timestamp == 1000.0
        assert entry.prev_hash == _GENESIS_HASH
        assert len(entry.entry_hash) == 64
        assert chain.verify()

    def test_multiple_entries_linked(self) -> None:
        chain = AuditChain(clock=_make_clock())
        e0 = chain.append({"a": 1})
        e1 = chain.append({"b": 2})
        e2 = chain.append({"c": 3})

        assert e1.prev_hash == e0.entry_hash
        assert e2.prev_hash == e1.entry_hash
        assert e0.sequence == 0
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert chain.verify()
        assert len(chain) == 3

    def test_verify_entry(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"x": 1})
        chain.append({"y": 2})
        assert chain.verify_entry(0)
        assert chain.verify_entry(1)

    def test_verify_entry_out_of_range(self) -> None:
        chain = AuditChain()
        with pytest.raises(IndexError):
            chain.verify_entry(0)

    def test_entries_returns_copy(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        entries = chain.entries()
        assert len(entries) == 1
        entries.clear()
        assert len(chain) == 1  # original unaffected


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


class TestAuditChainSerialization:
    def test_export_import_roundtrip(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"event": "HALT"})
        chain.append({"event": "ALLOW"})

        exported = chain.export_json()
        restored = AuditChain.from_json(exported, clock=_make_clock(start=2000.0))
        assert len(restored) == 2
        assert restored.verify()

        orig_entries = chain.entries()
        rest_entries = restored.entries()
        for o, r in zip(orig_entries, rest_entries):
            assert o.entry_hash == r.entry_hash

    def test_import_tampered_chain_raises(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"event": "HALT"})
        chain.append({"event": "ALLOW"})

        exported = chain.export_json()
        # Tamper with data
        exported[0]["data"]["event"] = "TAMPERED"

        with pytest.raises(ValueError, match="integrity"):
            AuditChain.from_json(exported)

    def test_import_empty_chain(self) -> None:
        chain = AuditChain.from_json([])
        assert len(chain) == 0
        assert chain.verify()


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestAuditChainTamperDetection:
    def test_modified_data_detected(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"event": "HALT"})
        chain.append({"event": "ALLOW"})

        # Tamper with first entry's data via internal access
        tampered = AuditEntry(
            sequence=chain._entries[0].sequence,
            timestamp=chain._entries[0].timestamp,
            prev_hash=chain._entries[0].prev_hash,
            data={"event": "FORGED"},
            entry_hash=chain._entries[0].entry_hash,  # old hash
        )
        chain._entries[0] = tampered
        assert not chain.verify()

    def test_modified_hash_detected(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})

        # Tamper with entry hash
        old = chain._entries[0]
        tampered = AuditEntry(
            sequence=old.sequence,
            timestamp=old.timestamp,
            prev_hash=old.prev_hash,
            data=old.data,
            entry_hash="deadbeef" * 8,
        )
        chain._entries[0] = tampered
        assert not chain.verify()

    def test_swapped_entries_detected(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})
        chain.append({"c": 3})

        # Swap entries 1 and 2
        chain._entries[1], chain._entries[2] = chain._entries[2], chain._entries[1]
        assert not chain.verify()

    def test_deleted_entry_detected(self) -> None:
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})
        chain.append({"c": 3})

        # Delete middle entry
        del chain._entries[1]
        assert not chain.verify()


# ---------------------------------------------------------------------------
# Adversarial tests
# ---------------------------------------------------------------------------


class TestAdversarialAuditChain:
    """Adversarial tests -- attacker mindset."""

    def test_concurrent_appends_maintain_integrity(self) -> None:
        """Multiple threads appending must produce a valid chain."""
        chain = AuditChain()
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    chain.append({"thread": n, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(chain) == 200
        assert chain.verify()

    def test_replay_attack_old_entries_fail(self) -> None:
        """Replaying old entries at the end of the chain must fail verification."""
        chain = AuditChain(clock=_make_clock())
        chain.append({"event": "A"})
        chain.append({"event": "B"})

        # Replay entry 0 at position 2
        replayed = chain._entries[0]
        chain._entries.append(replayed)
        assert not chain.verify()

    def test_forge_entry_with_correct_hash_but_wrong_prev(self) -> None:
        """Forging an entry with a valid self-hash but wrong prev_hash link."""
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})

        # Forge entry with wrong prev_hash
        forged_hash = _compute_hash(1, 1001.0, _GENESIS_HASH, {"b": 2})
        forged = AuditEntry(
            sequence=1,
            timestamp=1001.0,
            prev_hash=_GENESIS_HASH,  # wrong -- should link to entry 0
            data={"b": 2},
            entry_hash=forged_hash,
        )
        chain._entries[1] = forged
        assert not chain.verify()

    def test_data_with_nested_objects(self) -> None:
        """Complex nested data should hash deterministically."""
        chain = AuditChain(clock=_make_clock())
        data = {
            "nested": {"deep": [1, 2, {"key": "val"}]},
            "unicode": "\u200b\x00\xff",
            "float": 3.14159,
            "none": None,
            "bool": True,
        }
        chain.append(data)
        assert chain.verify()

        # Same data should produce same hash
        chain2 = AuditChain(clock=_make_clock())
        chain2.append(copy.deepcopy(data))
        assert chain.entries()[0].entry_hash == chain2.entries()[0].entry_hash

    def test_data_key_order_does_not_affect_hash(self) -> None:
        """json.dumps(sort_keys=True) ensures key order independence."""
        chain1 = AuditChain(clock=_make_clock())
        chain1.append({"z": 1, "a": 2, "m": 3})

        chain2 = AuditChain(clock=_make_clock())
        chain2.append({"a": 2, "m": 3, "z": 1})

        assert chain1.entries()[0].entry_hash == chain2.entries()[0].entry_hash

    def test_empty_data_dict(self) -> None:
        """Empty event payload should work."""
        chain = AuditChain(clock=_make_clock())
        entry = chain.append({})
        assert chain.verify()
        assert entry.data == {}

    def test_very_large_data(self) -> None:
        """Large payload should not break hashing."""
        chain = AuditChain(clock=_make_clock())
        big_data = {"key_" + str(i): "x" * 1000 for i in range(100)}
        chain.append(big_data)
        assert chain.verify()

    def test_import_with_corrupted_sequence(self) -> None:
        """Exported chain with wrong sequence numbers should fail import."""
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})

        exported = chain.export_json()
        exported[1]["sequence"] = 99  # corrupt sequence

        with pytest.raises(ValueError, match="integrity"):
            AuditChain.from_json(exported)

    def test_import_with_swapped_hashes(self) -> None:
        """Swapping entry_hash between entries should fail import."""
        chain = AuditChain(clock=_make_clock())
        chain.append({"a": 1})
        chain.append({"b": 2})

        exported = chain.export_json()
        exported[0]["entry_hash"], exported[1]["entry_hash"] = (
            exported[1]["entry_hash"],
            exported[0]["entry_hash"],
        )

        with pytest.raises(ValueError, match="integrity"):
            AuditChain.from_json(exported)

    def test_non_serializable_data_uses_str_fallback(self) -> None:
        """Non-JSON-serializable data should use str() fallback in hashing."""
        chain = AuditChain(clock=_make_clock())
        chain.append({"obj": object()})
        assert chain.verify()

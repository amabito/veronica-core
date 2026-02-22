"""Regression tests for PartialResultBuffer and PartialBufferOverflow."""

from __future__ import annotations

import pytest

from veronica_core.partial import PartialBufferOverflow, PartialResultBuffer, _MAX_BYTES, _MAX_CHUNKS


class TestPartialBufferOverflowOnChunkCount:
    def test_append_raises_on_chunk_count(self):
        buf = PartialResultBuffer()
        # Fill up to the limit with 1-byte chunks to avoid byte limit
        for _ in range(_MAX_CHUNKS):
            buf.append("x")
        # The next append must raise PartialBufferOverflow
        with pytest.raises(PartialBufferOverflow):
            buf.append("x")

    def test_chunk_count_overflow_is_value_error_subclass(self):
        buf = PartialResultBuffer()
        for _ in range(_MAX_CHUNKS):
            buf.append("x")
        with pytest.raises(ValueError):
            buf.append("x")

    def test_chunk_count_truncation_point(self):
        buf = PartialResultBuffer()
        for _ in range(_MAX_CHUNKS):
            buf.append("x")
        with pytest.raises(PartialBufferOverflow) as exc_info:
            buf.append("x")
        assert exc_info.value.truncation_point == "chunk_count"


class TestPartialBufferOverflowOnByteSize:
    def test_append_raises_on_byte_size(self):
        buf = PartialResultBuffer()
        # Append a chunk just under the limit
        big_chunk = "a" * (_MAX_BYTES - 1)
        buf.append(big_chunk)
        # This one pushes over the byte limit
        with pytest.raises(PartialBufferOverflow):
            buf.append("aa")

    def test_byte_size_truncation_point(self):
        buf = PartialResultBuffer()
        big_chunk = "a" * (_MAX_BYTES - 1)
        buf.append(big_chunk)
        with pytest.raises(PartialBufferOverflow) as exc_info:
            buf.append("aa")
        assert exc_info.value.truncation_point == "byte_size"


class TestPartialBufferOverflowEvidenceFields:
    def test_chunk_count_overflow_has_evidence_fields(self):
        buf = PartialResultBuffer()
        for _ in range(_MAX_CHUNKS):
            buf.append("x")
        with pytest.raises(PartialBufferOverflow) as exc_info:
            buf.append("x")
        exc = exc_info.value
        assert hasattr(exc, "kept_chunks")
        assert hasattr(exc, "total_chunks")
        assert hasattr(exc, "kept_bytes")
        assert hasattr(exc, "total_bytes")
        assert hasattr(exc, "truncation_point")
        assert exc.kept_chunks == _MAX_CHUNKS
        assert exc.total_chunks == _MAX_CHUNKS + 1

    def test_byte_size_overflow_has_evidence_fields(self):
        buf = PartialResultBuffer()
        chunk = "a" * 100
        buf.append(chunk)
        # Manufacture an overflow: append chunk that alone exceeds remaining space
        big = "b" * _MAX_BYTES
        with pytest.raises(PartialBufferOverflow) as exc_info:
            buf.append(big)
        exc = exc_info.value
        assert exc.truncation_point == "byte_size"
        assert exc.kept_bytes == 100  # only the first chunk was kept
        assert exc.total_bytes > _MAX_BYTES

    def test_to_dict_includes_truncated_after_overflow(self):
        buf = PartialResultBuffer()
        for _ in range(_MAX_CHUNKS):
            buf.append("x")
        with pytest.raises(PartialBufferOverflow):
            buf.append("x")
        d = buf.to_dict()
        assert d.get("truncated") is True

    def test_to_dict_no_truncated_key_when_normal(self):
        buf = PartialResultBuffer()
        buf.append("hello")
        d = buf.to_dict()
        assert "truncated" not in d

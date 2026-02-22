"""VERONICA Partial Result Preservation - Captures partial output on interruption.

Note: This module replaces the legacy VeronicaPersistence partial-save logic
with a dedicated streaming buffer for LLM output preservation.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Any, Dict
import logging

logger = logging.getLogger(__name__)

# Hard limits to prevent DoS via unbounded streaming buffers.
_MAX_CHUNKS: int = 10_000
_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB


class PartialBufferOverflow(ValueError):
    """Raised when PartialResultBuffer exceeds its chunk or byte limit.

    Subclasses ValueError for backward compatibility with callers that
    catch ValueError. Carries structured evidence so callers can emit
    a SafetyEvent with full context.

    Attributes:
        total_bytes: Total bytes accumulated before overflow.
        kept_bytes: Bytes that were kept (same as total_bytes at overflow).
        total_chunks: Number of chunks accumulated before overflow.
        kept_chunks: Chunks that were kept (same as total_chunks at overflow).
        truncation_point: "chunk_count" or "byte_size" indicating which
            limit was hit.
    """

    def __init__(
        self,
        message: str,
        total_bytes: int,
        kept_bytes: int,
        total_chunks: int,
        kept_chunks: int,
        truncation_point: str,
    ) -> None:
        super().__init__(message)
        self.total_bytes = total_bytes
        self.kept_bytes = kept_bytes
        self.total_chunks = total_chunks
        self.kept_chunks = kept_chunks
        self.truncation_point = truncation_point


@dataclass
class PartialResultBuffer:
    """Captures and preserves partial results during LLM streaming or multi-step execution.

    When an LLM call is interrupted (timeout, abort, budget exceeded),
    accumulated chunks are preserved instead of silently discarded.

    Limits:
        - Maximum 10,000 chunks (raises ValueError when exceeded).
        - Maximum 10 MB total bytes across all chunks (raises ValueError when exceeded).

    Example:
        buf = PartialResultBuffer()
        for chunk in llm_stream:
            buf.append(chunk)
        buf.mark_complete()
        # On interruption: buf.get_partial() returns accumulated text
    """

    _chunks: List[str] = field(default_factory=list, init=False)
    _metadata: Dict[str, Any] = field(default_factory=dict, init=False)
    _is_complete: bool = field(default=False, init=False)
    _total_bytes: int = field(default=0, init=False)
    _is_partial_overflow: bool = field(default=False, init=False)

    def append(self, chunk: str) -> None:
        """Append a streaming chunk.

        Args:
            chunk: Text chunk from LLM stream

        Raises:
            PartialBufferOverflow: If chunk count or total byte size limit is
                exceeded. Subclasses ValueError for backward compatibility.
        """
        if len(self._chunks) >= _MAX_CHUNKS:
            self._is_partial_overflow = True
            raise PartialBufferOverflow(
                f"PartialResultBuffer exceeded max chunk count ({_MAX_CHUNKS}). "
                "Possible streaming DoS.",
                total_bytes=self._total_bytes,
                kept_bytes=self._total_bytes,
                total_chunks=len(self._chunks) + 1,
                kept_chunks=len(self._chunks),
                truncation_point="chunk_count",
            )
        chunk_bytes = len(chunk.encode("utf-8"))
        if self._total_bytes + chunk_bytes > _MAX_BYTES:
            self._is_partial_overflow = True
            raise PartialBufferOverflow(
                f"PartialResultBuffer exceeded max byte size ({_MAX_BYTES} bytes). "
                "Possible streaming DoS.",
                total_bytes=self._total_bytes + chunk_bytes,
                kept_bytes=self._total_bytes,
                total_chunks=len(self._chunks) + 1,
                kept_chunks=len(self._chunks),
                truncation_point="byte_size",
            )
        self._chunks.append(chunk)
        self._total_bytes += chunk_bytes

    def mark_complete(self) -> None:
        """Mark the result as complete (not partial)."""
        self._is_complete = True

    def get_partial(self) -> str:
        """Get accumulated partial result as a single string."""
        return "".join(self._chunks)

    @property
    def chunk_count(self) -> int:
        """Number of chunks accumulated."""
        return len(self._chunks)

    @property
    def is_complete(self) -> bool:
        """True if result was fully received."""
        return self._is_complete

    @property
    def is_partial(self) -> bool:
        """True if chunks exist but result is incomplete."""
        return len(self._chunks) > 0 and not self._is_complete

    def set_metadata(self, key: str, value: Any) -> None:
        """Attach metadata (e.g., model, token count, abort reason).

        Args:
            key: Metadata key
            value: Metadata value
        """
        self._metadata[key] = value

    @property
    def metadata(self) -> Dict[str, Any]:
        """Get copy of metadata dict."""
        return dict(self._metadata)

    def clear(self) -> None:
        """Clear buffer for reuse."""
        self._chunks.clear()
        self._metadata.clear()
        self._is_complete = False
        self._total_bytes = 0

    def to_dict(self) -> Dict:
        """Serialize buffer state."""
        result: Dict = {
            "partial_text": self.get_partial(),
            "chunk_count": self.chunk_count,
            "is_complete": self._is_complete,
            "metadata": dict(self._metadata),
        }
        if self._is_partial_overflow:
            result["truncated"] = True
        return result

"""ComplianceExporter -- async batch export of SafetyEvents to a compliance backend.

Zero required dependencies beyond stdlib.  Uses ``urllib.request`` for HTTP
when ``httpx`` is not installed.  All network I/O runs in a background daemon
thread so that LLM call latency is never affected.

**Fail-safe**: every public method catches all exceptions and logs at DEBUG
level.  Compliance export must never crash the host application.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
import urllib.request
import weakref
from typing import Any, Dict, List, Optional, Tuple

from veronica_core.compliance.serializers import serialize_snapshot
from veronica_core.containment.execution_context import (
    ChainMetadata,
    ContextSnapshot,
    ExecutionContext,
)

logger = logging.getLogger("veronica_core.compliance")

# ---------------------------------------------------------------------------
# Optional httpx import
# ---------------------------------------------------------------------------

_httpx_client: Any = None

try:
    import httpx as _httpx_mod

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# Sentinel for shutdown
# ---------------------------------------------------------------------------

_SHUTDOWN = object()


class ComplianceExporter:
    """Batch exporter for veronica-core SafetyEvents and chain snapshots.

    Parameters
    ----------
    api_key:
        Bearer token sent in the ``Authorization`` header.
    endpoint:
        URL of the ``POST /api/ingest`` endpoint.
    batch_size:
        Maximum payloads per HTTP request.
    flush_interval_s:
        Seconds between automatic flush attempts.
    max_queue:
        Maximum queued payloads before oldest are dropped.
    timeout_s:
        HTTP request timeout in seconds.
    max_retries:
        Retries per failed HTTP request (0 = no retries).
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://audit.veronica-core.dev/api/ingest",
        *,
        batch_size: int = 50,
        flush_interval_s: float = 10.0,
        max_queue: int = 1000,
        timeout_s: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._max_queue = max_queue
        self._timeout_s = timeout_s
        self._max_retries = max_retries

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._closed = False
        self._lock = threading.Lock()
        self._attached: List[Tuple[weakref.ref, Optional[Any]]] = []

        # httpx client (reuses connections)
        self._httpx: Any = None
        if _HAS_HTTPX:
            try:
                self._httpx = _httpx_mod.Client(timeout=timeout_s)
            except Exception:
                pass

        # Background flush thread
        self._thread = threading.Thread(
            target=self._run_loop,
            name="veronica-compliance-exporter",
            daemon=True,
        )
        self._thread.start()

        # Flush on interpreter exit
        atexit.register(self._atexit_flush)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, ctx: ExecutionContext) -> None:
        """Register *ctx* for automatic snapshot export on flush/close.

        The exporter keeps a weak reference to *ctx*.  When ``flush()``
        or ``close()`` is called (including the atexit handler), any
        still-alive attached contexts are snapshotted and queued.
        """
        try:
            meta = getattr(ctx, "_metadata", None)
            self._attached.append((weakref.ref(ctx), meta))
        except Exception:
            logger.debug("compliance: attach failed", exc_info=True)

    def export_snapshot(
        self,
        snapshot: ContextSnapshot,
        metadata: Optional[ChainMetadata] = None,
        graph: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue a snapshot for batched export.  Never raises."""
        try:
            payload = serialize_snapshot(snapshot, metadata=metadata, graph=graph)
            self._enqueue(payload)
        except Exception:
            logger.debug("compliance: export_snapshot failed", exc_info=True)

    def flush(self) -> None:
        """Force-send all queued payloads.  Blocks until done or timeout."""
        try:
            self._flush_batch()
        except Exception:
            logger.debug("compliance: flush failed", exc_info=True)

    def close(self) -> None:
        """Stop background thread and flush remaining payloads."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        # Export snapshots from attached contexts before shutdown
        self._drain_attached()

        try:
            self._queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            pass

        self._thread.join(timeout=self._timeout_s * 2)

        # Final drain
        self.flush()

        if self._httpx is not None:
            try:
                self._httpx.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drain_attached(self) -> None:
        """Export snapshots from all attached contexts, then clear the list."""
        attached = self._attached
        self._attached = []
        for ref, meta in attached:
            try:
                ctx = ref()
                if ctx is None:
                    continue
                snapshot = ctx.get_snapshot()
                graph = ctx.get_graph_snapshot()
                self.export_snapshot(snapshot, metadata=meta, graph=graph)
            except Exception:
                logger.debug("compliance: drain_attached failed", exc_info=True)

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        """Put payload on the queue, dropping oldest if full."""
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # Drop oldest to make room
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass

    def _run_loop(self) -> None:
        """Background thread main loop."""
        while not self._closed:
            try:
                # Wait for data or timeout
                try:
                    item = self._queue.get(timeout=self._flush_interval)
                    if item is _SHUTDOWN:
                        break
                    # Got an item -- collect more up to batch_size
                    batch = [item]
                    while len(batch) < self._batch_size:
                        try:
                            next_item = self._queue.get_nowait()
                            if next_item is _SHUTDOWN:
                                self._send_batch(batch)
                                return
                            batch.append(next_item)
                        except queue.Empty:
                            break
                    self._send_batch(batch)
                except queue.Empty:
                    # Timeout -- flush whatever we have
                    self._flush_batch()
            except Exception:
                logger.debug("compliance: run_loop error", exc_info=True)
                time.sleep(1.0)

    def _flush_batch(self) -> None:
        """Drain the queue and send everything."""
        batch: List[Dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
                if item is _SHUTDOWN:
                    break
                batch.append(item)
            except queue.Empty:
                break
        if batch:
            self._send_batch(batch)

    def _send_batch(self, batch: List[Dict[str, Any]]) -> None:
        """Send a batch of payloads to the ingest endpoint."""
        for payload in batch:
            self._send_one(payload)

    def _send_one(self, payload: Dict[str, Any]) -> None:
        """Send a single payload with retries."""
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(1 + self._max_retries):
            try:
                if self._httpx is not None:
                    resp = self._httpx.post(
                        self._endpoint,
                        content=body,
                        headers=headers,
                    )
                    if resp.status_code < 400:
                        return
                    logger.debug(
                        "compliance: HTTP %d on attempt %d",
                        resp.status_code,
                        attempt + 1,
                    )
                else:
                    req = urllib.request.Request(
                        self._endpoint,
                        data=body,
                        headers=headers,
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                        if resp.status < 400:
                            return
                    logger.debug(
                        "compliance: HTTP %d on attempt %d",
                        resp.status,
                        attempt + 1,
                    )
            except Exception:
                logger.debug(
                    "compliance: send attempt %d failed",
                    attempt + 1,
                    exc_info=True,
                )

            if attempt < self._max_retries:
                time.sleep(0.5 * (attempt + 1))

    def _atexit_flush(self) -> None:
        """Best-effort flush on interpreter shutdown."""
        try:
            self._drain_attached()
            self._flush_batch()
        except Exception:
            pass

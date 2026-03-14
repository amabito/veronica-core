"""Shared nogil compatibility helpers for test suite.

This module exists because conftest.py cannot be imported directly by test
modules. The ``nogil_unstable`` marker is used to skip timing-sensitive tests
on free-threaded Python 3.13t where thread scheduling is less predictable.
"""

from __future__ import annotations

import sys

import pytest

_NOGIL = not getattr(sys, "_is_gil_enabled", lambda: True)()

nogil_unstable = pytest.mark.skipif(
    _NOGIL,
    reason="Timing-sensitive test unstable under free-threaded Python 3.13t (nogil)",
)

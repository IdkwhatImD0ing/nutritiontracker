"""Shared pytest config (BACKLOG 17): put backend/ on sys.path once so test
modules import backend code directly (``import auth``, ``import tracing``, ...)
without per-file shims."""

import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parents[1])
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

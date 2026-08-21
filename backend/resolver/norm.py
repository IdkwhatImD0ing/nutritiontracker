"""Phrase normalization (DESIGN.md §3.0).

norm(s) = NFKC → lowercase → strip punctuation → collapse whitespace
        → drop leading articles/possessives ("a", "an", "the", "my", "some")

Plural/singular variance is NOT normalized away (tier 2a absorbs it).
Quantities never reach norm() — decomposition strips them first.
"""

from __future__ import annotations

import re
import unicodedata

_LEADING = frozenset({"a", "an", "the", "my", "some"})
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    words = s.split(" ") if s else []
    while words and words[0] in _LEADING:
        words.pop(0)
    return " ".join(words)

"""Duplicate detection.

* Exact duplicates: identical bytes (SHA-256), handled in the pipeline.
* Near duplicates: the same content re-scanned, re-exported or re-printed to PDF
  produces different bytes. Those are found by cosine similarity of a local,
  deterministic hashed bag-of-words fingerprint (no network, no PHI egress).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence

FINGERPRINT_DIM = 512
MIN_TOKENS = 8
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def fingerprint(text: str) -> list[float] | None:
    """L2-normalised hashed term-frequency vector; None when text is too short."""
    toks = tokens(text)
    if len(toks) < MIN_TOKENS:
        return None
    vec = [0.0] * FINGERPRINT_DIM
    for tok in toks:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        vec[int.from_bytes(digest, "big") % FINGERPRINT_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [round(v / norm, 6) for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def best_match(
    candidate: Sequence[float], others: Iterable[tuple[int, Sequence[float] | None]], threshold: float
) -> tuple[int, float] | None:
    """Return (id, score) of the most similar fingerprint at/above threshold."""
    if threshold <= 0:
        return None
    best: tuple[int, float] | None = None
    for other_id, vec in others:
        if not vec:
            continue
        score = cosine(candidate, vec)
        if score >= threshold and (best is None or score > best[1]):
            best = (other_id, score)
    return best

"""Pluggable semantic-similarity scorer for answer-quality evaluation.

Rationale: with an LLM-native agent, answers are non-deterministic natural
language, so quality is scored by semantic closeness to a reference answer
rather than by substring matching. Safety properties are asserted separately
and deterministically — never with similarity.

Embedding backend resolution order:
1. sentence-transformers (real embeddings) if installed.
2. A dependency-free char-ngram cosine fallback so the harness runs anywhere.
   The fallback is a stand-in for a real embedding model, not a replacement;
   install sentence-transformers for meaningful semantic scores.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Callable


def _char_ngrams(text: str, n: int = 2) -> Counter:
    cleaned = re.sub(r"\s+", "", text.lower())
    if len(cleaned) < n:
        return Counter([cleaned]) if cleaned else Counter()
    return Counter(cleaned[i:i + n] for i in range(len(cleaned) - n + 1))


def _lexical_similarity(a: str, b: str) -> float:
    va, vb = _char_ngrams(a), _char_ngrams(b)
    if not va or not vb:
        return 0.0
    shared = set(va) & set(vb)
    dot = sum(va[g] * vb[g] for g in shared)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


@lru_cache(maxsize=1)
def _resolve_backend() -> tuple[str, Callable[[str, str], float]]:
    try:
        from sentence_transformers import SentenceTransformer, util  # type: ignore

        model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

        def _st(a: str, b: str) -> float:
            ea, eb = model.encode([a, b])
            return float(util.cos_sim(ea, eb)[0][0])

        return "sentence-transformers", _st
    except Exception:
        return "lexical-fallback", _lexical_similarity


def backend_name() -> str:
    return _resolve_backend()[0]


def similarity(answer: str, reference: str) -> float:
    return _resolve_backend()[1](answer or "", reference or "")

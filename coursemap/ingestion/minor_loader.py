"""Loader for minors.json dataset."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"
MINORS_PATH = _DATASETS_DIR / "minors.json"


def load_minors() -> list[dict]:
    """Load minors.json and return list of minor dicts."""
    if not MINORS_PATH.exists():
        logger.warning("minors.json not found at %s", MINORS_PATH)
        return []
    with open(MINORS_PATH, encoding="utf-8") as f:
        minors = json.load(f)
    logger.info("Loaded %d minors", len(minors))
    return minors


def search_minors(query: str, minors: list[dict]) -> list[dict]:
    """
    Return minors matching the query string.

    Matches against the minor name. Supports partial word matching - each
    whitespace-separated word in the query must appear somewhere in the name.
    Falls back to substring matching if no word-match results are found.
    Case-insensitive.
    """
    if not query:
        return minors
    words = query.strip().lower().split()
    if not words:
        return minors

    def _word_match(m: dict) -> bool:
        target = m.get("name", "").lower()
        return all(any(w in tok for tok in target.split()) for w in words)

    results = [m for m in minors if _word_match(m)]
    if results:
        return results
    # Fallback: substring match anywhere in name
    q = " ".join(words)
    return [m for m in minors if q in m.get("name", "").lower()]

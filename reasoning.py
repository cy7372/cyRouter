"""Reasoning replay cache for thinking models (DeepSeek / GLM style).

DeepSeek thinking mode requires the assistant's ``reasoning_content`` from
tool-call rounds to be passed back on subsequent turns. Most clients (pi,
opencode, ...) discard it, so cyRouter caches the real reasoning per
``tool_call_id`` when a response flows through and replays it when a later
request omits it — restoring the model's reasoning continuity instead of
letting it confabulate.

Contamination guards (lesson from OmniRoute issue #9573, where a cache-miss
placeholder was echoed back by the model and written into the cache, forming
a self-poisoning loop):

  * empty / whitespace-only reasoning is NEVER stored
  * known placeholder strings are NEVER stored
  * a cache miss never triggers any write-back

In-memory only (by design choice): TTL + LRU eviction, lost on restart —
the first tool round after a restart simply misses once and re-primes.
"""

from __future__ import annotations

import time
import logging

logger = logging.getLogger("cyRouter.reasoning")

# Placeholder strings injected by other middlewares (OmniRoute). If one ever
# arrives via a client history it must not be cached or replayed.
_PLACEHOLDER_MARKERS = ("prior reasoning summary unavailable",)


class ReasoningCache:
    """tool_call_id -> (reasoning_text, stored_at). TTL + LRU eviction."""

    def __init__(self, ttl: float = 7200.0, max_entries: int = 2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: dict[str, tuple[str, float]] = {}
        self._hits = 0
        self._misses = 0
        self._stored = 0
        self._rejected = 0

    # ── write path ────────────────────────────────────────────────────
    def store(self, tool_call_ids: list, reasoning: str) -> int:
        """Cache reasoning under every tool_call id. Returns entries stored.

        Rejects empty and placeholder reasoning outright — writing either
        back would poison future turns (OmniRoute #9573).
        """
        if not reasoning or not reasoning.strip():
            self._rejected += 1
            return 0
        low = reasoning.lower()
        if any(marker in low for marker in _PLACEHOLDER_MARKERS):
            logger.warning("reasoning cache: refused to store placeholder text")
            self._rejected += 1
            return 0
        now = time.time()
        self._purge_expired(now)
        ids = [t for t in tool_call_ids if t]
        while len(self._store) + len(ids) > self.max_entries and self._store:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            self._store.pop(oldest, None)
        for tcid in ids:
            self._store[tcid] = (reasoning, now)
        self._stored += len(ids)
        return len(ids)

    # ── read path ─────────────────────────────────────────────────────
    def lookup(self, tool_call_id: str) -> str | None:
        entry = self._store.get(tool_call_id)
        if entry is None:
            self._misses += 1
            return None
        reasoning, ts = entry
        if time.time() - ts > self.ttl:
            self._store.pop(tool_call_id, None)
            self._misses += 1
            return None
        self._hits += 1
        return reasoning

    def lookup_by_tool_calls(self, tool_calls: list) -> str | None:
        """Return cached reasoning for the first tool_call id that hits."""
        for tc in tool_calls or []:
            tcid = tc.get("id") if isinstance(tc, dict) else None
            if tcid:
                hit = self.lookup(tcid)
                if hit:
                    return hit
        return None

    # ── housekeeping ──────────────────────────────────────────────────
    def _purge_expired(self, now: float):
        expired = [k for k, (_, ts) in self._store.items() if now - ts > self.ttl]
        for k in expired:
            self._store.pop(k, None)

    def clear(self):
        self._store.clear()

    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "stored": self._stored,
            "rejected": self._rejected,
            "ttl_seconds": self.ttl,
            "max_entries": self.max_entries,
        }


_cache: ReasoningCache | None = None


def get_reasoning_cache() -> ReasoningCache:
    global _cache
    if _cache is None:
        _cache = ReasoningCache()
    return _cache

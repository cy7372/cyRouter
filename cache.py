from __future__ import annotations
import hashlib
import json
import time
import logging

logger = logging.getLogger("cyRouter.cache")


class SemanticCache:
    """Exact-match response cache keyed by (model, messages, params).

    Non-streaming only — streaming responses are inherently non-idempotent.
    TTL-based expiry + LRU eviction when max_entries is exceeded.
    """

    def __init__(self, ttl: int = 300, max_entries: int = 1000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: dict[str, dict] = {}  # key -> {response, stored_at, mtime}
        self._hits = 0
        self._misses = 0
        self._stale = 0

    @staticmethod
    def _build_key(body: dict) -> str:
        """Build a stable cache key from the request body.

        Only fields that affect the response go into the key:
        model, messages (content only), temperature, max_tokens, tools.
        """
        parts: dict = {}
        parts["model"] = body.get("model", "")
        msgs = body.get("messages", [])
        parts["messages"] = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            if isinstance(m, dict)
            else str(m)
            for m in msgs
        ]
        parts["temperature"] = body.get("temperature", 0.7)
        parts["max_tokens"] = body.get("max_tokens", 4096)
        if body.get("tools"):
            parts["tools_hash"] = hashlib.sha256(
                json.dumps(body["tools"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
        raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, body: dict) -> dict | None:
        key = self._build_key(body)
        entry = self._store.get(key)
        if not entry:
            self._misses += 1
            return None
        if time.time() - entry["stored_at"] > self.ttl:
            self._stale += 1
            return None
        entry["mtime"] = time.time()
        self._hits += 1
        return entry["response"]

    def set(self, body: dict, response: dict):
        key = self._build_key(body)
        if key in self._store or len(self._store) < self.max_entries:
            pass
        else:
            # LRU eviction: remove the entry with the lowest mtime.
            oldest_key = min(self._store, key=lambda k: self._store[k]["mtime"])
            del self._store[oldest_key]
            logger.debug("[cache] evicted %s for new entry", oldest_key[:12])
        self._store[key] = {
            "response": response,
            "stored_at": time.time(),
            "mtime": time.time(),
        }

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "stale": self._stale,
            "hit_rate": round(self._hits / max(total, 1), 3),
            "max_entries": self.max_entries,
            "ttl": self.ttl,
        }

    def reload(self, cfg: dict):
        cc = cfg.get("semantic_cache", {})
        if not isinstance(cc, dict) or not cc.get("enabled"):
            self.max_entries = 0
            return
        self.ttl = int(cc.get("ttl", 300))
        self.max_entries = int(cc.get("max_entries", 1000))
        # Evict excess if max_entries shrunk
        while len(self._store) > self.max_entries:
            oldest_key = min(self._store, key=lambda k: self._store[k]["mtime"])
            del self._store[oldest_key]

    @property
    def enabled(self) -> bool:
        return self.max_entries > 0


_cache: SemanticCache | None = None


def get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache()
        try:
            from .config import load_config
        except ImportError:
            from config import load_config
        _cache.reload(load_config())
    return _cache

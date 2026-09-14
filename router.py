from __future__ import annotations

import asyncio
import contextvars
import datetime
import json
import logging
import os
import time

try:
    from .builtin_pricing import effective_pricing
    from .config import DATA_DIR, decrypt_value, load_config
    from .providers.base import BaseProvider
    from .providers.claude import ClaudeProvider
    from .providers.openai import OpenAIProvider
except ImportError:
    from builtin_pricing import effective_pricing
    from config import DATA_DIR, decrypt_value, load_config
    from providers.base import BaseProvider
    from providers.claude import ClaudeProvider
    from providers.openai import OpenAIProvider

logger = logging.getLogger("cyRouter")

# Context var: set by auth_middleware, read by _log_request so every
# log entry carries which API key was used without touching all the
# log-building call sites across 4 endpoint handlers.
current_key_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "key_name", default=""
)

PROVIDER_CLASSES = {
    "openai": OpenAIProvider,
    "anthropic": ClaudeProvider,
}

# protocol → { provider_name → default_url }
PROVIDER_DEFAULTS = {
    "openai": {
        "openai": "https://api.openai.com/v1",
        "aliyun-dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # legacy name
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "deepseek": "https://api.deepseek.com",
    },
    "anthropic": {
        "claude": "https://api.anthropic.com",
        "aliyun-dashscope": "https://dashscope.aliyuncs.com/apps/anthropic",
        "qwen": "https://dashscope.aliyuncs.com/apps/anthropic",  # legacy name
        "zhipu": "https://open.bigmodel.cn/api/anthropic",
        "deepseek": "https://api.deepseek.com/anthropic",
    },
}

# Legacy type aliases — map old combined types to (protocol, provider)
_PROVIDER_ALIASES = {
    "qwen": ("openai", "qwen"),
    "zhipu": ("openai", "zhipu"),
    "zhipu-coding": ("anthropic", "zhipu"),
    "deepseek": ("openai", "deepseek"),
    "deepseek-anthropic": ("anthropic", "deepseek"),
    "claude": ("anthropic", "claude"),
}

# Persistent request log (JSONL file)
# Retention: keep today's logs (0:00–24:00). During the first hour after midnight
# (00:00–01:00), also retain yesterday's logs so they can still be reviewed.
_LOG_PATH = DATA_DIR / "request_log.jsonl"
_DAILY_STATS_PATH = DATA_DIR / "daily_stats.json"

# ── Token-plan local usage ledger ─────────────────────────────────
# Plan-style providers (Aliyun Token Plan) expose no quota API, so we track
# the cache-discounted theoretical cost of the requests they serve, per day,
# in a small rolling ledger. The dashboard converts it to an estimated
# credits usage bar (see "token_plan_quota" config).
_TOKEN_PLAN_USAGE_PATH = DATA_DIR / "token_plan_usage.json"
_token_plan_usage_cache: dict = {}
_token_plan_usage_dirty = False


def _load_token_plan_usage() -> dict:
    global _token_plan_usage_cache
    if _token_plan_usage_cache:
        return _token_plan_usage_cache
    if _TOKEN_PLAN_USAGE_PATH.exists():
        try:
            with open(_TOKEN_PLAN_USAGE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _token_plan_usage_cache = loaded
        except (json.JSONDecodeError, OSError):
            pass
    return _token_plan_usage_cache


def _flush_token_plan_usage():
    global _token_plan_usage_dirty
    if not _token_plan_usage_dirty:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(_TOKEN_PLAN_USAGE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_token_plan_usage_cache or {}, f, ensure_ascii=False)
        os.replace(tmp, _TOKEN_PLAN_USAGE_PATH)
        _token_plan_usage_dirty = False
    except OSError as e:
        logger.warning("Failed to flush token-plan usage: %s", e)


def _token_plan_provider_urls(cfg: dict) -> set[str]:
    """All upstream base URLs identifying the configured token-plan provider."""
    tp = cfg.get("token_plan_quota") or {}
    pname = tp.get("provider")
    if not pname:
        return set()
    urls: set[str] = set()
    for p in cfg.get("providers", []):
        if p.get("name") == pname:
            if p.get("base_url"):
                urls.add(p["base_url"])
            urls.update((p.get("base_urls") or {}).values())
    return urls


def _get_log_cutoff() -> float:
    """Return the earliest timestamp to retain.

    Normally this is today at 00:00. Between 00:00 and 01:00 it is
    yesterday at 00:00, giving a 1-hour grace window for viewing the
    previous day's logs before they are purged.
    """
    import datetime as _dt

    now = time.time()
    today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = today.timestamp()
    # localtime to check the hour
    tm = time.localtime(now)
    if tm.tm_hour < 1:
        return today_start - 86400  # yesterday 00:00
    return today_start


def _load_log_entries() -> list[dict]:
    if not _LOG_PATH.exists():
        return []
    cutoff = _get_log_cutoff()
    entries = []
    try:
        with open(_LOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff:
                        entries.append(entry)
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        # Race or permission error mid-read: return whatever we have so far.
        logger.warning("log read failed (_load_log_entries), %d partial", len(entries))
    return entries


def _rewrite_log_file(entries: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_LOG_PATH, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Compaction write failed — log and swallow so the request path is
        # not affected; the next compaction cycle retries.
        logger.warning("log rewrite failed", exc_info=True)


def get_log_entries(limit: int = 50, session_id: str | None = None) -> list[dict]:
    """Read the last `limit` entries from the log file efficiently.

    If session_id is provided, only entries with matching x-cyrouter-session-id
    are returned (still limited to `limit`).
    """
    if not _LOG_PATH.exists():
        return []
    cutoff = _get_log_cutoff()
    entries: list[dict] = []
    # Read from end of file using a deque-like approach
    try:
        with open(_LOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) < cutoff:
                        continue
                    if (
                        session_id is not None
                        and entry.get("session_id", "") != session_id
                    ):
                        continue
                    entries.append(entry)
                    if len(entries) > limit * 2:
                        entries = entries[-limit:]
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        # Read failure (race/permission): degrade to partial result instead
        # of surfacing a 500 to the dashboard API caller.
        logger.warning("log read failed (get_log_entries), %d partial", len(entries))
    return entries[-limit:]


def get_log_stats(session_id: str | None = None) -> list[dict]:
    """Lightweight read: only extract fields needed for stats (skip heavy input/output).

    If session_id is provided, only entries with matching x-cyrouter-session-id
    are returned.
    """
    if not _LOG_PATH.exists():
        return []
    cutoff = _get_log_cutoff()
    entries = []
    try:
        with open(_LOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) < cutoff:
                        continue
                    if (
                        session_id is not None
                        and entry.get("session_id", "") != session_id
                    ):
                        continue
                    entries.append(
                        {
                            "timestamp": entry.get("timestamp", 0),
                            "model": entry.get("model", ""),
                            "status": entry.get("status", ""),
                            "usage": entry.get("usage"),
                            "session_id": entry.get("session_id", ""),
                            "trace_id": entry.get("trace_id", ""),
                            "key_name": entry.get("key_name", ""),
                            "provider": entry.get("provider", ""),
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        logger.warning("log read failed (get_log_stats), %d partial", len(entries))
    return entries


_MAX_LOG_FIELD = 500
_MAX_LOG_REASONING = 2000  # thinking content is longer; keep more for diagnosis
_MAX_LOG_MESSAGES = 20


def _truncate_entry(entry: dict) -> dict:
    """Truncate large input/output/reasoning fields to keep log file small."""
    inp = entry.get("input")
    if isinstance(inp, list):
        if len(inp) > _MAX_LOG_MESSAGES:
            entry["messages"] = len(inp)
            entry["input"] = inp[-_MAX_LOG_MESSAGES:]
        for m in entry["input"]:
            c = m.get("content", "")
            if isinstance(c, str) and len(c) > _MAX_LOG_FIELD:
                m["content"] = c[:_MAX_LOG_FIELD] + "..."
    out = entry.get("output", "")
    if isinstance(out, str) and len(out) > _MAX_LOG_FIELD:
        entry["output"] = out[:_MAX_LOG_FIELD] + "..."
    reasoning = entry.get("reasoning", "")
    if isinstance(reasoning, str) and len(reasoning) > _MAX_LOG_REASONING:
        entry["reasoning"] = reasoning[:_MAX_LOG_REASONING] + "..."
    return entry


_last_compact_ts: float = 0.0
_compact_pending: bool = False
_log_write_lock: asyncio.Lock = asyncio.Lock()


def _schedule_compact():
    """Mark that compaction is needed. Runs off the async event loop."""
    global _compact_pending
    _compact_pending = True


def _do_compact():
    """Perform the actual compaction (call from thread pool)."""
    global _last_compact_ts, _compact_pending
    try:
        _rewrite_log_file(_load_log_entries())
        _last_compact_ts = time.time()
    except Exception:
        pass
    _compact_pending = False


async def _maybe_compact_async():
    """Non-blocking compaction: run in a background thread if needed."""
    global _compact_pending
    if not _compact_pending:
        return
    _compact_pending = False
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_compact)


def _append_log_sync(entry: dict):
    """Synchronous append (called from thread pool)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Logging must never break the actual request — swallow disk errors
        # after recording them so the response path stays unaffected.
        logger.warning("log append failed", exc_info=True)


async def _log_request(entry: dict):
    """Async log writer: serialises appends via lock, runs in thread pool."""
    global _last_compact_ts
    kn = current_key_name.get()
    if kn:
        entry["key_name"] = kn
    _truncate_entry(entry)
    loop = asyncio.get_event_loop()
    async with _log_write_lock:
        await loop.run_in_executor(None, _append_log_sync, entry)
        await loop.run_in_executor(None, _update_daily_stat, entry)
    # Check if compaction is needed (cheap stat only)
    now = time.time()
    if _LOG_PATH.exists() and (
        _LOG_PATH.stat().st_size > 20 * 1024 * 1024 or (now - _last_compact_ts) > 3600
    ):
        _schedule_compact()


# ── Daily stats persistence ──────────────────────────────────────────
_daily_stats_cache: dict = {}  # {"2026-06-09": {"cost": ..., "in": ..., "out": ...}, ...}
_daily_stats_dirty: bool = False


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


def _load_daily_stats() -> dict:
    global _daily_stats_cache
    if _daily_stats_cache:
        return _daily_stats_cache
    if _DAILY_STATS_PATH.exists():
        try:
            with open(_DAILY_STATS_PATH, encoding="utf-8", errors="replace") as f:
                _daily_stats_cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            _daily_stats_cache = {}
    return _daily_stats_cache


def _flush_daily_stats():
    global _daily_stats_dirty
    if not _daily_stats_dirty:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = str(_DAILY_STATS_PATH) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_daily_stats_cache, f, ensure_ascii=False)
        os.replace(tmp_path, _DAILY_STATS_PATH)
        _daily_stats_dirty = False
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("Failed to flush daily stats: %s", e)


def _normalize_usage(usage: dict) -> dict:
    """Normalize usage so input_tokens always equals total input.

    Anthropic's API: input_tokens = cache_miss + cache_read + cache_creation (total)
    DeepSeek's API:  input_tokens = cache_miss only (excludes cache)

    This function detects the DeepSeek convention and fixes it so downstream
    code can safely assume input_tokens >= cache_read + cache_creation.
    """
    cr = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation_input_tokens", 0)
    if cr + cc == 0:
        return usage
    inp = usage.get("input_tokens", 0)
    # DeepSeek convention: cache tokens not included in input_tokens
    if cr > inp or (inp > 0 and cr + cc > inp):
        usage["input_tokens"] = inp + cr + cc
    return usage


def compute_token_cost(
    inp: int, out: int, cr: int, cc: int, price: dict, *, cached: bool = False
) -> float:
    """Per-token cost (元) for one request.

    Centralized billing formula shared by the dashboard (/api/stats), the
    usage endpoint (/api/usage), and the daily-stats aggregator
    (_update_daily_stat / rebuild_today_stats) so every site agrees. Keeping
    it in one place prevents dashboard-vs-daily-stats mismatches that arise
    when each site reimplements the arithmetic.

    cached=False → full theoretical: every input billed at price["input"].
    cached=True  → cache-discounted actual: cache-read at price["cache_input"],
                   cache-creation at price["cache_creation"], rest at input.
    """
    if cached:
        cache_price = price.get("cache_input", price["input"])
        cc_price = price.get("cache_creation", price["input"])
        normal_in = inp - cr - cc
        return (
            normal_in * price["input"]
            + cr * cache_price
            + cc * cc_price
            + out * price["output"]
        ) / 1_000_000
    return (inp * price["input"] + out * price["output"]) / 1_000_000


# Empty day template — copied per day (never shared/mutated) so setdefault
# can't alias two days onto one dict.
_DAY_EMPTY = {
    "cost": 0.0,
    "pay_cost": 0.0,
    "in": 0,
    "out": 0,
    "cache_read": 0,
    "requests": 0,
}


def _entry_metrics(entry: dict, cfg: dict) -> dict:
    """Normalize one completed request log entry into accumulation fields.

    Single source of cost/token/plan-coverage math shared by the live
    accumulator (_update_daily_stat) and the startup rebuild
    (rebuild_today_stats) — previously duplicated in both, with the hazard
    documented in AGENTS.md ("改这两处任何一处必须同步另一处").
    """
    usage = _normalize_usage(entry.get("usage") or {})
    inp = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    cr = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation_input_tokens", 0)
    model = _resolve_model_alias(entry.get("model", ""), cfg)
    # config pricing > builtin catalog (2026-09-10: fresh installs used to
    # show ¥0/— for every model — pricing lived only in the local config).
    p = effective_pricing(cfg, model)
    if p:
        # cost = full theoretical (matches /api/stats theoretical_cost);
        # pay_cost = cache-discounted actual spend (non-plan only).
        cost = compute_token_cost(inp, out, cr, cc, p, cached=False)
        pay_cost = compute_token_cost(inp, out, cr, cc, p, cached=True)
    else:
        cost = pay_cost = 0
    # Requests served by a plan-covered provider (Aliyun Token Plan) are
    # prepaid by the subscription — no per-token outlay, same treatment as
    # plan_monthly models. Note coverage is per-serving-provider: the same
    # model can be plan-covered on one provider and pay-as-you-go on another.
    prov_url = entry.get("provider") or ""
    plan_covered = bool(p and p.get("plan_monthly")) or bool(
        prov_url and prov_url in _token_plan_provider_urls(cfg)
    )
    return {
        "inp": inp,
        "out": out,
        "cr": cr,
        "cc": cc,
        "cost": cost,
        "pay_cost": pay_cost,
        "prov_url": prov_url,
        "plan_covered": plan_covered,
        "p": p,
    }


def _accumulate_day_stat(day: dict, m: dict) -> None:
    """Accumulate one request's metrics into a day dict (cost/tokens/provider)."""
    day["cost"] += m["cost"]
    if not m["plan_covered"]:
        day["pay_cost"] += m["pay_cost"]
    # Cache savings: how much cache-read tokens saved vs full input price.
    if m["p"] and m["cr"] > 0:
        cache_price = m["p"].get("cache_input", m["p"]["input"])
        day["cache_saved"] = (
            day.get("cache_saved", 0.0)
            + m["cr"] * (m["p"]["input"] - cache_price) / 1_000_000
        )
    # Plan savings: value covered by subscription (prepaid, not out-of-pocket).
    if m["plan_covered"] and m["pay_cost"] > 0:
        day["plan_saved"] = day.get("plan_saved", 0.0) + m["pay_cost"]
    day["in"] += m["inp"]
    day["out"] += m["out"]
    day["cache_read"] += m["cr"]
    day["requests"] += 1
    # Per-provider cost/token breakdown — feeds local quota estimation for
    # plan-style providers with no quota API (token_plan_quota / local_quota).
    if m["prov_url"]:
        pe = day.setdefault("by_provider", {}).setdefault(
            m["prov_url"], {"cost": 0.0, "requests": 0}
        )
        pe["cost"] += m["cost"]
        pe["requests"] += 1
        pe["in"] = pe.get("in", 0) + m["inp"]
        pe["out"] = pe.get("out", 0) + m["out"]
        pe["cache_read"] = pe.get("cache_read", 0) + m["cr"]


def _update_daily_stat(entry: dict):
    """Update in-memory daily stats from a completed request log entry."""
    global _daily_stats_dirty
    if entry.get("status") != "ok":
        return
    ts = entry.get("timestamp", 0)
    day_key = time.strftime("%Y-%m-%d", time.localtime(ts))
    cfg = load_config()
    m = _entry_metrics(entry, cfg)
    day = _load_daily_stats().setdefault(day_key, dict(_DAY_EMPTY))
    _accumulate_day_stat(day, m)
    # Token-plan ledger: cache-discounted cost of what the plan provider
    # actually served (pay_cost basis ≈ real consumption). Only the live path
    # maintains it — rebuilding from the compacted log would double-count.
    if m["pay_cost"] > 0 and m["prov_url"] in _token_plan_provider_urls(cfg):
        tp_day = _load_token_plan_usage().setdefault(
            day_key, {"cost": 0.0, "requests": 0}
        )
        tp_day["cost"] += m["pay_cost"]
        tp_day["requests"] += 1
        global _token_plan_usage_dirty
        _token_plan_usage_dirty = True
        _flush_token_plan_usage()
    _daily_stats_dirty = True
    _flush_daily_stats()


def get_daily_stats(year: int = 0, month: int = 0) -> list[dict]:
    """Return daily stats for a given month, merging persisted + live data."""
    import calendar

    now = time.localtime()
    if year == 0:
        year = now.tm_year
    if month == 0:
        month = now.tm_mon
    last_day = calendar.monthrange(year, month)[1]

    persisted = _load_daily_stats()
    result = []
    for d in range(1, last_day + 1):
        key = f"{year}-{month:02d}-{d:02d}"
        rec = persisted.get(key, {})
        result.append(
            {
                "day": d,
                "requests": rec.get("requests", 0),
                "in": rec.get("in", 0),
                "out": rec.get("out", 0),
                "cache_read": rec.get("cache_read", 0),
                "cost": rec.get("cost", 0.0),
                "pay_cost": rec.get("pay_cost", 0.0),
            }
        )
    return result


def get_recent_daily_stats(days: int = 7, offset: int = 0) -> list[dict]:
    """Return daily stats for N days ending `offset` days ago (offset=0 includes today)."""
    if days <= 0:
        days = 7
    if offset < 0:
        offset = 0
    persisted = _load_daily_stats()
    end = datetime.date.today() - datetime.timedelta(days=offset)
    result = []
    for i in range(days - 1, -1, -1):
        d = end - datetime.timedelta(days=i)
        key = f"{d.year}-{d.month:02d}-{d.day:02d}"
        rec = persisted.get(key, {})
        result.append(
            {
                "date": key,
                "day": d.day,
                "month": d.month,
                "requests": rec.get("requests", 0),
                "in": rec.get("in", 0),
                "out": rec.get("out", 0),
                "cache_read": rec.get("cache_read", 0),
                # Same semantics as /api/stats: cost = actual spend (pay_cost),
                # theoretical_cost = full-price. Both pages now agree.
                "cost": rec.get("pay_cost", 0.0),
                "pay_cost": rec.get("pay_cost", 0.0),
                "theoretical_cost": rec.get("cost", 0.0),
                "cache_saved_cost": rec.get("cache_saved", 0.0),
                "plan_saved": rec.get("plan_saved", 0.0),
            }
        )
    return result


def rebuild_today_stats():
    """Rebuild today's daily stats from request logs (call on startup)."""
    global _daily_stats_dirty, _daily_stats_cache
    today_key = _today_key()
    stats = _load_daily_stats()
    cfg = load_config()

    day = dict(_DAY_EMPTY)
    for e in get_log_stats():
        if e.get("status") != "ok":
            continue
        key = time.strftime("%Y-%m-%d", time.localtime(e.get("timestamp", 0)))
        if key != today_key:
            continue
        _accumulate_day_stat(day, _entry_metrics(e, cfg))

    stats[today_key] = day
    _daily_stats_cache = stats
    _daily_stats_dirty = True
    _flush_daily_stats()


def _resolve_protocol(p_cfg: dict) -> tuple[str, str]:
    """Resolve provider config to (protocol, provider_name).
    Supports new format: type="anthropic", provider="deepseek"
    And legacy format: type="deepseek-anthropic"
    """
    ptype = p_cfg.get("type", "openai")
    provider_name = p_cfg.get("provider", "")

    # Legacy: combined type like "deepseek-anthropic"
    if ptype in _PROVIDER_ALIASES:
        proto, pname = _PROVIDER_ALIASES[ptype]
        if not provider_name:
            provider_name = pname
        return proto, provider_name

    # New format: type is just the protocol
    return ptype, provider_name


_provider_cache: dict[str, BaseProvider] = {}
_provider_cache_ts: float = 0.0
_PROVIDER_CACHE_TTL = 30.0


def _invalidate_provider_cache():
    global _provider_cache, _provider_cache_ts
    _provider_cache.clear()
    _provider_cache_ts = 0.0


def _build_provider(p_cfg: dict) -> BaseProvider:
    global _provider_cache, _provider_cache_ts
    now = time.time()
    if now - _provider_cache_ts > _PROVIDER_CACHE_TTL:
        _provider_cache.clear()
        _provider_cache_ts = now
    name = p_cfg["name"]
    if name in _provider_cache:
        return _provider_cache[name]

    protocol, _ = _resolve_protocol(p_cfg)
    cls = PROVIDER_CLASSES.get(protocol)
    if not cls:
        raise ValueError(f"Unknown protocol: {protocol}")
    api_key = p_cfg.get("api_key", "")
    if p_cfg.get("encrypted"):
        api_key = decrypt_value(api_key)
    base_url = (
        p_cfg.get("base_url")
        or p_cfg.get("base_urls", {}).get(protocol)
        or PROVIDER_DEFAULTS.get(protocol, {}).get(p_cfg.get("provider", ""))
    )
    provider = cls(
        name=name,
        api_key=api_key,
        base_url=base_url,
    )
    _provider_cache[name] = provider
    return provider


def _resolve_model_alias(model: str, cfg: dict) -> str:
    """Apply user-configured model_aliases (supports glob like claude-haiku-*).

    Aliases take precedence over explicit provider model lists — this lets the
    user reroute traffic for models that no provider declares (e.g. Claude Code
    sends claude-haiku-* for background tasks even when the user has no Claude
    provider configured).
    """
    aliases = cfg.get("model_aliases") or {}
    if not aliases:
        return model
    # Exact match first
    if model in aliases:
        return aliases[model]
    # Glob match (fnmatch-style, * matches anything)
    import fnmatch

    for pattern, target in aliases.items():
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatchcase(model, pattern):
                return target
    return model


def _lookup_model_chain(model: str, cfg: dict) -> list | None:
    """Ordered provider chain from the unified ``cfg["models"]`` map.

    Exact match first, then fnmatch glob patterns. Returns the chain
    (list of provider names, or a single name) or None when the model
    isn't in the map.
    """
    models_map = cfg.get("models") or {}
    if not isinstance(models_map, dict):
        return None
    chain = models_map.get(model)
    if chain:
        return chain
    import fnmatch

    for pattern, c in models_map.items():
        if ("*" in pattern or "?" in pattern) and fnmatch.fnmatchcase(model, pattern):
            return c
    return None


def _build_chain_candidates(
    names: list, model: str, cfg: dict
) -> list[tuple[BaseProvider, str | None]]:
    """Build provider objects for an ordered provider-name chain.

    Providers missing from config or failing to build are skipped with an
    error log. The model id is forwarded to each candidate, UNLESS the
    provider declares a per-provider ``model_map`` (e.g. a backend whose own
    model id differs from the cyRouter-facing name: ``kimi-k3`` → ``k3``).
    The mapped id flows into both the SDK ``ChatRequest`` and the raw-proxy
    upstream tuple, so a chain like ``["kimi code", "opencode-go"]``
    correctly sends ``k3`` to kimi and ``kimi-k3`` to opencode-go.
    """
    by_name = {p["name"]: p for p in cfg.get("providers", [])}
    cands: list[tuple[BaseProvider, str | None]] = []
    for name in names:
        p_cfg = by_name.get(name)
        if p_cfg is None:
            logger.error(f"models: unknown provider '{name}' for model '{model}'")
            continue
        try:
            pmap = p_cfg.get("model_map") or {}
            mapped = pmap.get(model, model) if isinstance(pmap, dict) else model
            cands.append((_build_provider(p_cfg), mapped))
        except Exception as e:
            logger.error(f"Failed to build provider {name}: {e}")
    return cands


def _get_provider_candidates(model: str) -> list[tuple[BaseProvider, str | None]]:
    """Ordered list of (provider, mapped_model) candidates for a model.

    Unified routing model: ``cfg["models"]`` maps each model id (or glob
    pattern) to an ordered provider-name chain. The request layer tries the
    chain in sequence, moving to the next provider when the current one
    errors before producing any content (e.g. quota exhausted). The first
    entry is the default provider; the rest are tried on failure — one
    concept, one ordered list.

    Unknown models return [] → callers answer a truthful 404 (2026-09-04:
    the legacy catch-all "first buildable provider" silently misrouted
    typo'd model names to providers[0]; the legacy model_fallbacks /
    per-provider models / route-rules layers were dead code).
    """
    cfg = load_config()

    # 1. Apply model_aliases first — overrides everything else
    resolved = _resolve_model_alias(model, cfg)
    if resolved != model:
        logger.info(f"model alias: {model} -> {resolved}")
        model = resolved

    # 2. Unified models map (exact match, then glob)
    chain = _lookup_model_chain(model, cfg)
    if chain:
        if isinstance(chain, str):
            chain = [chain]
        return _build_chain_candidates(chain, model, cfg)

    return []


def _get_provider_for_model(model: str) -> tuple[BaseProvider | None, str | None]:
    """Primary (first) candidate for a model; see _get_provider_candidates."""
    cands = _get_provider_candidates(model)
    return cands[0] if cands else (None, None)

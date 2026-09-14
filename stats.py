"""Unified statistics service — single source of truth for all metrics.

Both /api/stats (dashboard 总览) and /api/stats/recent (Token 统计页) pull
from here. Today's totals come from daily_stats.json (never loses data to
log compaction). Per-model/per-key breakdowns come from request_log.jsonl
(display only — may be slightly stale after compaction, but totals are exact).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("cyRouter.stats")

try:
    from .router import (
        _load_daily_stats,
        _today_key,
        load_config,
    )
except ImportError:
    from router import (
        _load_daily_stats,
        _today_key,
        load_config,
    )


def get_today_overview(cfg: dict | None = None) -> dict:
    """Today's aggregate stats from daily_stats.json.

    This is the single source of truth — both the dashboard and the
    token-stats page use this for today's totals, guaranteeing they match.

    daily_stats.json is incrementally updated by _update_daily_stat on
    every completed request and rebuilt from logs on startup. It never
    loses data to log compaction (unlike request_log.jsonl).
    """
    if cfg is None:
        cfg = load_config()
    stats = _load_daily_stats()
    today = _today_key()
    d = stats.get(today, {})

    cost = float(d.get("cost", 0.0))
    pay_cost = float(d.get("pay_cost", 0.0))
    cache_saved = float(d.get("cache_saved", 0.0))
    plan_saved = float(d.get("plan_saved", 0.0))

    return {
        "requests": d.get("requests", 0),
        "input_tokens": d.get("in", 0),
        "output_tokens": d.get("out", 0),
        "cache_read_tokens": d.get("cache_read", 0),
        # UI semantics: "cost" = actual spend (pay_cost), NOT theoretical.
        # daily_stats["cost"] = theoretical (full price); ["pay_cost"] = actual.
        "cost": round(pay_cost, 4),
        "token_cost": round(pay_cost, 4),
        "theoretical_cost": round(cost, 4),
        "pay_cost": round(pay_cost, 4),
        "cache_saved_cost": round(cache_saved, 4),
        "plan_saved": round(plan_saved, 4),
    }

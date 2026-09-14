"""Unit tests for ``router._normalize_usage``.

The function reconciles two upstream conventions for ``input_tokens``:

* Anthropic: ``input_tokens`` already includes cache_read + cache_creation
  (i.e. it is the *total* input).
* DeepSeek:  ``input_tokens`` is the cache-miss portion only and excludes
  cache tokens.

After normalization, downstream code can safely assume
``input_tokens >= cache_read_input_tokens + cache_creation_input_tokens``.

Also includes two integration-style tests that feed the normalized usage
through a representative cost-calculation formula to confirm the invariant
holds (``normal_in >= 0``).
"""

from __future__ import annotations

import pytest

from router import _normalize_usage


# ── Anthropic format (input_tokens already includes cache) ────────────


def test_anthropic_no_cache():
    usage = {"input_tokens": 100, "output_tokens": 50}
    assert _normalize_usage(usage) == {"input_tokens": 100, "output_tokens": 50}


def test_anthropic_with_cache_balanced():
    # 1000 >= 600 + 100 → Anthropic convention, unchanged.
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
    }
    assert _normalize_usage(usage) == {
        "input_tokens": 1000,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
    }


def test_anthropic_cache_creation_only():
    # 500 >= 200 → unchanged.
    usage = {"input_tokens": 500, "cache_creation_input_tokens": 200}
    assert _normalize_usage(usage) == {
        "input_tokens": 500,
        "cache_creation_input_tokens": 200,
    }


def test_anthropic_cache_read_only():
    usage = {"input_tokens": 300, "cache_read_input_tokens": 100}
    assert _normalize_usage(usage) == {
        "input_tokens": 300,
        "cache_read_input_tokens": 100,
    }


# ── DeepSeek format (input_tokens excludes cache) ─────────────────────


def test_deepseek_cache_exceeds_input():
    # cr=600 > inp=100 → input_tokens corrected to 700.
    usage = {"input_tokens": 100, "cache_read_input_tokens": 600, "output_tokens": 50}
    result = _normalize_usage(usage)
    assert result["input_tokens"] == 700
    assert result["cache_read_input_tokens"] == 600
    assert result["output_tokens"] == 50


def test_deepseek_combined_exceeds_input():
    # cr+cc=600 > inp=200 → input_tokens corrected to 800.
    usage = {
        "input_tokens": 200,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 100,
    }
    result = _normalize_usage(usage)
    assert result["input_tokens"] == 800
    assert result["cache_read_input_tokens"] == 500
    assert result["cache_creation_input_tokens"] == 100


def test_deepseek_realistic():
    # DeepSeek typical: huge cache hit, input_tokens is just the miss portion.
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 50000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 200,
    }
    result = _normalize_usage(usage)
    assert result["input_tokens"] == 51000
    assert result["cache_read_input_tokens"] == 50000
    assert result["output_tokens"] == 200


# ── Boundary cases ────────────────────────────────────────────────────


def test_zero_cache_returns_unchanged():
    # cr+cc == 0 → early return, untouched.
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assert _normalize_usage(usage) == {
        "input_tokens": 100,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_missing_input_tokens_with_cache():
    # inp defaults to 0; cr=500 > 0 → input_tokens set to 500.
    usage = {"cache_read_input_tokens": 500}
    result = _normalize_usage(usage)
    assert result["input_tokens"] == 500
    assert result["cache_read_input_tokens"] == 500


def test_empty_dict():
    assert _normalize_usage({}) == {}


def test_none_values_handled():
    # KNOWN BOUNDARY BUG: ``input_tokens: None`` is not coerced to 0, so the
    # comparison ``cr > inp`` raises TypeError. This test pins the current
    # behaviour; if router.py is later hardened to treat None as 0, this
    # test should be updated to assert the corrected value instead.
    usage = {"input_tokens": None, "cache_read_input_tokens": 100}
    with pytest.raises(TypeError):
        _normalize_usage(usage)


def test_mutation_behavior():
    # The function mutates the dict in place AND returns the same object.
    usage = {"input_tokens": 100, "cache_read_input_tokens": 600}
    result = _normalize_usage(usage)
    assert result is usage  # same object
    assert usage["input_tokens"] == 700  # original dict was mutated


# ── Cost-calculation integration ──────────────────────────────────────
#
# Representative per-million pricing (USD). The exact numbers don't matter
# for these tests — what matters is that ``normal_in`` (the non-cache input
# portion) stays non-negative after normalization.


def _cost(usage, pricing):
    """Inline cost formula mirroring the downstream billing logic."""
    inp = usage.get("input_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    normal_in = inp - cr - cc
    assert normal_in >= 0, "normal_in must be non-negative after normalization"
    return (
        normal_in * pricing["input"]
        + cr * pricing["cache_read"]
        + cc * pricing["cache_creation"]
        + out * pricing["output"]
    ) / 1e6


def test_downstream_cost_calc_anthropic():
    pricing = {
        "input": 3.0,
        "cache_read": 0.3,
        "cache_creation": 3.75,
        "output": 15.0,
    }
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
    }
    normalized = _normalize_usage(usage)
    # normal_in = 1000 - 600 - 100 = 300 >= 0
    cost = _cost(normalized, pricing)
    assert cost > 0
    # Sanity check the arithmetic.
    expected = (300 * 3.0 + 600 * 0.3 + 100 * 3.75 + 50 * 15.0) / 1e6
    assert cost == pytest.approx(expected)


def test_downstream_cost_calc_deepseek():
    pricing = {
        "input": 3.0,
        "cache_read": 0.3,
        "cache_creation": 3.75,
        "output": 15.0,
    }
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 50000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 200,
    }
    # Without normalization, normal_in would be 1000 - 50000 - 0 = -49000
    # (negative → billing bug). After normalization input_tokens = 51000,
    # so normal_in = 51000 - 50000 - 0 = 1000 >= 0.
    normalized = _normalize_usage(usage)
    assert normalized["input_tokens"] == 51000
    cost = _cost(normalized, pricing)
    assert cost > 0
    expected = (1000 * 3.0 + 50000 * 0.3 + 0 * 3.75 + 200 * 15.0) / 1e6
    assert cost == pytest.approx(expected)

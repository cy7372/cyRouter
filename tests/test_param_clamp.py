"""Unit tests for `_apply_param_clamp` (provider param_drop / param_map / param_clamp).

Regression guard for the kimi-k3 temperature-rejection bug: kimi's coding
endpoint only allows a single temperature value (which FLUCTUATES over time —
0.6, then 1, then 0.6 again), so non-matching client values get a hard 400.
The robust fix is `param_drop: ["temperature"]` on the provider (omit the
field entirely — "omitting always works"), rather than `param_clamp` (force a
fixed value that goes stale when the allowed value changes).

`param_map` covers the middle case: the upstream accepts the FIELD but
rejects certain VALUES (volcano glm-5.3 rejects reasoning_effort:"none" —
"this model always thinks" — while accepting low/high/max). The gateway
translates the value so a client's "minimal thinking" intent survives
provider failover instead of surfacing as a mid-stream 400 / empty reply
(the 2026-09-07 compact-cache empty-summary incident).

These tests lock in that the dropping/mapping/clamping logic itself never
regresses; the *config* (choice of which providers get which treatment)
lives in config.json.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402


def _cfg_with(providers: list) -> dict:
    return {"providers": providers}


def test_param_drop_removes_field(monkeypatch):
    """param_drop deletes the listed keys from the request body."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with([{"name": "kimi", "param_drop": ["temperature"]}]),
    )
    body = {"model": "k3-256k", "temperature": 0.7, "max_tokens": 100}
    main._apply_param_clamp(body, "kimi")
    assert "temperature" not in body, "param_drop must remove temperature"
    assert body["max_tokens"] == 100  # untouched


def test_param_clamp_forces_value(monkeypatch):
    """param_clamp overwrites the field with a fixed value."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with([{"name": "p", "param_clamp": {"temperature": 1}}]),
    )
    body = {"model": "m", "temperature": 0.3}
    main._apply_param_clamp(body, "p")
    assert body["temperature"] == 1


def test_drop_runs_before_clamp(monkeypatch):
    """If both are set, drop runs first then clamp — clamp can re-add."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [
                {
                    "name": "p",
                    "param_drop": ["temperature"],
                    "param_clamp": {"top_p": 0.9},
                }
            ]
        ),
    )
    body = {"temperature": 0.7, "top_p": 0.5}
    main._apply_param_clamp(body, "p")
    assert "temperature" not in body
    assert body["top_p"] == 0.9


def test_unknown_provider_is_noop(monkeypatch):
    """An unknown provider name must not raise or mutate the body."""
    monkeypatch.setattr(main, "load_config", lambda: _cfg_with([]))
    body = {"model": "m", "temperature": 0.5}
    main._apply_param_clamp(body, "does-not-exist")
    assert body == {"model": "m", "temperature": 0.5}


def test_provider_without_clamp_config_is_noop(monkeypatch):
    """A provider with neither param_drop nor param_clamp is passthrough."""
    monkeypatch.setattr(main, "load_config", lambda: _cfg_with([{"name": "plain"}]))
    body = {"temperature": 0.42}
    main._apply_param_clamp(body, "plain")
    assert body["temperature"] == 0.42


def test_per_provider_isolation(monkeypatch):
    """Each candidate in a chain applies its OWN param config — a kimi-backed
    primary and a kimi-backed fallback both need temperature dropped, and a
    neutral provider in the same chain must be left alone. This mirrors the
    kimi-k3 chain ['kimi code', 'opencode-go'] where both are kimi-backed."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [
                {"name": "kimi code", "param_drop": ["temperature"]},
                {"name": "opencode-go", "param_drop": ["temperature"]},
                {"name": "neutral"},  # no param config
            ]
        ),
    )
    # Simulate the candidate loop: each provider clamps its own copy of the body
    b1 = {"temperature": 0.7}
    main._apply_param_clamp(b1, "kimi code")
    assert "temperature" not in b1

    b2 = {"temperature": 0.7}
    main._apply_param_clamp(b2, "opencode-go")
    assert "temperature" not in b2

    b3 = {"temperature": 0.7}
    main._apply_param_clamp(b3, "neutral")
    assert b3["temperature"] == 0.7


def test_param_map_rewrites_mapped_value(monkeypatch):
    """param_map translates a rejected value: none -> low (volcano glm-5.3).

    Regression test for the 2026-09-07 compact-cache incident: reasoning_effort
    "none" passed zhipu-coding but 400'd on volcano ("该模型始终思考，不支持
    关闭思考；请使用 low、high 或 max"), surfacing to pi as an empty reply.
    """
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [
                {
                    "name": "volcano-apig",
                    "param_map": {"reasoning_effort": {"none": "low"}},
                }
            ]
        ),
    )
    body = {"model": "glm-5.3", "reasoning_effort": "none", "stream": True}
    main._apply_param_clamp(body, "volcano-apig")
    assert body["reasoning_effort"] == "low", "none must be rewritten to low"


def test_param_map_passes_through_unmapped_value(monkeypatch):
    """Values without a mapping rule pass through untouched (high stays high)."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [{"name": "p", "param_map": {"reasoning_effort": {"none": "low"}}}]
        ),
    )
    body = {"reasoning_effort": "high"}
    main._apply_param_clamp(body, "p")
    assert body["reasoning_effort"] == "high"


def test_param_map_ignores_non_string_values(monkeypatch):
    """Non-string values (e.g. zhipu's dict-shaped `thinking`) are skipped,
    never raise — a dict is unhashable and would crash rules.get()."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [{"name": "p", "param_map": {"thinking": {"none": "low"}}}]
        ),
    )
    body = {"thinking": {"type": "disabled"}}
    main._apply_param_clamp(body, "p")  # must not raise
    assert body["thinking"] == {"type": "disabled"}


def test_param_map_absent_field_is_noop(monkeypatch):
    """Mapping a field the request doesn't carry must not inject it."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [{"name": "p", "param_map": {"reasoning_effort": {"none": "low"}}}]
        ),
    )
    body = {"temperature": 0.5}
    main._apply_param_clamp(body, "p")
    assert "reasoning_effort" not in body


def test_drop_map_clamp_order(monkeypatch):
    """Full pipeline order: drop removes, map translates the survivor,
    clamp has the final say (can overwrite a mapped value)."""
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: _cfg_with(
            [
                {
                    "name": "p",
                    "param_drop": ["temperature"],
                    "param_map": {"reasoning_effort": {"none": "low"}},
                    "param_clamp": {"reasoning_effort": "high", "top_p": 0.9},
                }
            ]
        ),
    )
    body = {"temperature": 0.7, "reasoning_effort": "none", "top_p": 0.5}
    main._apply_param_clamp(body, "p")
    assert "temperature" not in body, "drop runs first"
    assert body["reasoning_effort"] == "high", "clamp overrides the mapped value"
    assert body["top_p"] == 0.9

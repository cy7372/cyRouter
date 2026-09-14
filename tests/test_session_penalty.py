"""Unit tests for the session-level provider penalty window (2026-09-07).

Root cause this guards against: the GLOBAL breaker counts consecutive
failures per (provider, model) across ALL sessions. Under parallel traffic
(multiple agents sharing the router), other sessions' successes keep
reseting that counter (trips oscillates 0↔1, circuit never opens) while one
session's long-thinking requests get silently killed every time — so the
killed session's recovery retries slam back into the same dying primary.

The penalty window fixes the aggregation: a kill records a per
(session_key, model) penalty that NO success can clear — only the time
window does. The provider is demoted to the LAST chain candidate for that
session (never hard-removed: single-candidate chains / panic semantics
stay intact).
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_penalty(monkeypatch):
    main._session_penalty.clear()
    # Isolate from the on-disk config: default enabled, 600s window.
    monkeypatch.setattr(main, "load_config", dict)
    yield
    main._session_penalty.clear()


def _record(sid="auto-abc123", provider="zhipu-coding", model="glm-5.3"):
    main._session_penalty_record(sid, provider, model)


# ── record / active ──


def test_record_then_active():
    _record()
    assert main._session_penalty_active("auto-abc123", "glm-5.3") == "zhipu-coding"


def test_record_ignores_empty_parts():
    _record(sid="")
    _record(provider="")
    _record(model="")
    assert main._session_penalty == {}


def test_penalty_survives_successes():
    """The defining property vs consecutive counting: nothing but time
    clears a penalty. (There is no success-reporting API at all — this test
    pins that absence for the session penalty store.)"""
    _record()
    # Simulate any amount of unrelated traffic between the kill and the
    # next request from this session — e.g. another session succeeding on
    # the same (provider, model).
    main._cb_ok("zhipu-coding", "glm-5.3")
    assert main._session_penalty_active("auto-abc123", "glm-5.3") == "zhipu-coding"


def test_penalty_scoped_per_session():
    _record(sid="sess-A")
    assert main._session_penalty_active("sess-B", "glm-5.3") is None


def test_penalty_scoped_per_model():
    _record()
    assert main._session_penalty_active("auto-abc123", "glm-5.3-flash") is None


def test_penalty_expires_with_window(monkeypatch):
    _record()
    ent = main._session_penalty[("auto-abc123", "glm-5.3")]
    # Age the record past the default 600s window.
    main._session_penalty[("auto-abc123", "glm-5.3")] = (
        ent[0],
        ent[1] - 601.0,
    )
    assert main._session_penalty_active("auto-abc123", "glm-5.3") is None
    # Expired entry is removed on read.
    assert ("auto-abc123", "glm-5.3") not in main._session_penalty


def test_disabled_via_config(monkeypatch):
    monkeypatch.setattr(
        main, "load_config", lambda: {"session_penalty": {"enabled": False}}
    )
    _record()
    assert main._session_penalty == {}
    assert main._session_penalty_active("auto-abc123", "glm-5.3") is None


def test_custom_duration_via_config(monkeypatch):
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: {"session_penalty": {"duration_seconds": 30}},
    )
    _record()
    key = ("auto-abc123", "glm-5.3")
    prov, ts = main._session_penalty[key]
    main._session_penalty[key] = (prov, ts - 31.0)
    assert main._session_penalty_active("auto-abc123", "glm-5.3") is None


# ── reorder ──


def test_reorder_demotes_penalized_to_last():
    _record(provider="zhipu-coding")
    names = ["zhipu-coding", "volcano-apig"]
    order = main._session_penalty_reorder("auto-abc123", "glm-5.3", names)
    assert order == [1, 0]


def test_reorder_stable_for_middle_provider():
    _record(provider="middle")
    names = ["first", "middle", "last"]
    order = main._session_penalty_reorder("auto-abc123", "glm-5.3", names)
    assert order == [0, 2, 1]


def test_reorder_noop_without_penalty():
    assert main._session_penalty_reorder("auto-abc123", "glm-5.3", ["a", "b"]) is None


def test_reorder_noop_when_provider_not_on_chain():
    _record(provider="gone-provider")
    assert main._session_penalty_reorder("auto-abc123", "glm-5.3", ["a", "b"]) is None


def test_reorder_noop_single_candidate():
    _record()
    assert (
        main._session_penalty_reorder("auto-abc123", "glm-5.3", ["zhipu-coding"])
        is None
    )


def test_reorder_noop_when_already_last():
    _record(provider="volcano-apig")
    names = ["zhipu-coding", "volcano-apig"]
    assert main._session_penalty_reorder("auto-abc123", "glm-5.3", names) is None


def test_reorder_noop_after_expiry():
    _record()
    key = ("auto-abc123", "glm-5.3")
    prov, ts = main._session_penalty[key]
    main._session_penalty[key] = (prov, ts - 601.0)
    assert (
        main._session_penalty_reorder(
            "auto-abc123", "glm-5.3", ["zhipu-coding", "volcano-apig"]
        )
        is None
    )


def test_reorder_empty_session_key():
    _record()
    # Empty session key never records anything; also a reorder with an
    # empty key must be a no-op even if a store entry leaked in.
    main._session_penalty[("", "glm-5.3")] = ("zhipu-coding", time.time())
    assert main._session_penalty_reorder("", "glm-5.3", ["zhipu-coding", "b"]) is None


# ── pruning ──


def test_prune_on_overflow():
    monkeypatch_cfg = {}
    now = time.time()
    for i in range(300):
        main._session_penalty[(f"sess-{i}", "m")] = (
            "p",
            now - 700.0 if i < 200 else now,  # 200 stale + 100 fresh
        )
    _record(sid="new-session")  # triggers the >256 prune
    assert len(main._session_penalty) <= 101 + 1
    # Fresh entries survived.
    assert main._session_penalty_active("sess-250", "m") == "p"
    assert main._session_penalty_active("sess-5", "m") is None  # stale, gone

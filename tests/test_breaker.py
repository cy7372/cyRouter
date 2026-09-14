"""Unit tests for the circuit breaker: escalating cooldown, decay,
half-open probe escalation, active-probe uneject, and error classification
in main._cb_fail."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
from breaker import CircuitBreaker  # noqa: E402


def _breaker(**over):
    cfg = {
        "failure_threshold": 2,
        "cooldown_seconds": 120.0,
        "half_open_max": 1,
        "cooldown_max_seconds": 480.0,
        "cooldown_backoff": "linear",
        "decay_seconds": 600.0,
    }
    cfg.update(over)
    return CircuitBreaker(**cfg)


def _trip(b: CircuitBreaker, provider: str = "p", model: str = "m"):
    """Drive the breaker to OPEN from CLOSED (threshold consecutive fails)."""
    b.report_failure(provider, model)
    b.report_failure(provider, model)
    assert b.is_open(provider, model)  # sanity: now open


# ── Basic three-state machine ──


def test_closed_allows_requests():
    b = _breaker()
    assert b.allow("p", "m")
    b.report_failure("p", "m")  # 1 < threshold
    assert b.allow("p", "m")


def test_trips_after_threshold_and_blocks():
    b = _breaker()
    _trip(b)
    assert not b.allow("p", "m")
    assert b.is_open("p", "m")


def test_success_resets_failure_count():
    b = _breaker()
    b.report_failure("p", "m")
    b.report_success("p", "m")  # consecutive counter cleared
    b.report_failure("p", "m")  # only 1 again
    assert b.allow("p", "m")


# ── Escalating cooldown (Envoy: base × consecutive trips, capped) ──


def test_linear_cooldown_escalates_per_trip():
    b = _breaker()
    _trip(b)
    s = b._states["p|m"]
    assert b._cooldown_for(s) == 120.0  # trip #1

    # Half-open probe fails → trip #2 → 240s
    s["tripped_at"] = time.time() - 121  # age past cooldown
    assert b.allow("p", "m")  # OPEN → HALF_OPEN probe slot
    b.report_failure("p", "m")  # probe failed
    s = b._states["p|m"]
    assert s["trips"] == 2
    assert b._cooldown_for(s) == 240.0
    assert b.is_open("p", "m")
    # Still open at 121s into trip #2 (needs 240s)
    s["tripped_at"] = time.time() - 121
    assert not b.allow("p", "m")
    assert b.is_open("p", "m")
    # Past 240s → half-open again
    s["tripped_at"] = time.time() - 241
    assert b.allow("p", "m")


def test_cooldown_capped_at_max():
    b = _breaker()  # max 480
    _trip(b)
    s = b._states["p|m"]
    s["trips"] = 10  # 120×10 = 1200 → capped
    assert b._cooldown_for(s) == 480.0


def test_exponential_backoff():
    b = _breaker(cooldown_backoff="exponential")
    s = {"trips": 1}
    assert b._cooldown_for(s) == 120.0
    s["trips"] = 2
    assert b._cooldown_for(s) == 240.0
    s["trips"] = 3
    assert b._cooldown_for(s) == 480.0
    s["trips"] = 4
    assert b._cooldown_for(s) == 480.0  # capped


# ── Decay: sustained health lowers escalation ──


def test_half_open_success_closes_but_keeps_trips():
    b = _breaker()
    _trip(b)
    s = b._states["p|m"]
    s["tripped_at"] = time.time() - 121
    assert b.allow("p", "m")  # half-open probe
    b.report_success("p", "m")
    s = b._states["p|m"]
    assert s["state"] == "closed"
    assert s["trips"] == 1  # kept — refail will escalate to trip #2


def test_health_decays_trip_counter():
    b = _breaker()
    _trip(b)
    s = b._states["p|m"]
    s["tripped_at"] = time.time() - 121
    b.allow("p", "m")
    b.report_success("p", "m")  # closed, trips=1
    s["decay_anchor"] = time.time() - 601  # one decay window of health
    b.report_success("p", "m")
    assert ("p|m") not in b._states  # trips decayed to 0 → state dropped


# ── Active probe uneject (reset clears everything) ──


def test_reset_clears_escalation():
    b = _breaker()
    _trip(b)
    b._states["p|m"]["trips"] = 5
    b.reset("p", "m")  # active probe succeeded
    assert b.allow("p", "m")
    b.report_failure("p", "m")
    b.report_failure("p", "m")
    assert b._cooldown_for(b._states["p|m"]) == 120.0  # back to base


def test_is_open_is_read_only():
    """is_open must not transition OPEN→HALF_OPEN nor consume probe slots."""
    b = _breaker()
    _trip(b)
    s = b._states["p|m"]
    s["tripped_at"] = time.time() - 121  # cooldown over
    assert not b.is_open("p", "m")  # read-only: reports not-open…
    assert s["state"] == "open"  # …without transitioning state
    assert b.allow("p", "m")  # the real request performs the transition


# ── Error classification in main._cb_fail ──


@pytest.fixture(autouse=True)
def _clean_state():
    main._failed_provider_models.clear()
    yield
    main._failed_provider_models.clear()


def test_cb_fail_skips_4xx():
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        main._cb_fail("p1", "m1", status=400)
        main._cb_fail("p1", "m1", status=401)
        assert ("p1|m1") not in b._states  # client errors never trip
        main._cb_fail("p1", "m1", status=500)
        main._cb_fail("p1", "m1", status=502)
        assert b.is_open("p1", "m1")  # 5xx do
    finally:
        b.reset()
        b.reload(main.load_config())


def test_cb_fail_429_marks_provider_model_not_breaker():
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        main._cb_fail("p1", "m1", status=429)
        main._cb_fail("p1", "m1", status=429)
        assert ("p1|m1") not in b._states  # breaker untouched
        assert main._is_provider_model_failed("p1", "m1")  # fatal mark instead
    finally:
        b.reset()
        b.reload(main.load_config())


def test_cb_fail_sdk_exception_status():
    class FakeClientError(Exception):
        status_code = 400

    class FakeServerError(Exception):
        status_code = 503

    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        main._cb_fail("p1", "m1", exc=FakeClientError())
        main._cb_fail("p1", "m1", exc=FakeClientError())
        assert ("p1|m1") not in b._states
        main._cb_fail("p1", "m1", exc=FakeServerError())
        main._cb_fail("p1", "m1", exc=FakeServerError())
        assert b.is_open("p1", "m1")
    finally:
        b.reset()
        b.reload(main.load_config())


def test_cb_fail_no_status_counts_timeouts_and_eof():
    """Timeout / connection / stream-kill failures carry no status → count."""
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        main._cb_fail("p1", "m1")  # e.g. httpx.TimeoutException / EOF kill
        main._cb_fail("p1", "m1")
        assert b.is_open("p1", "m1")
    finally:
        b.reset()
        b.reload(main.load_config())


# ── Panic mode: last candidate never hard-walled ──


def test_cb_check_panic_last_candidate_flows_through():
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        b.report_failure("p", "m")
        b.report_failure("p", "m")  # OPEN
        assert b.is_open("p", "m")
        # last candidate → None (route anyway)
        assert main._cb_check("p", "m", last_candidate=True) is None
        # non-last candidate → 503 with Retry-After
        resp = main._cb_check("p", "m", last_candidate=False)
        assert resp is not None and resp.status_code == 503
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) > 0
    finally:
        b.reset()
        b.reload(main.load_config())


def test_panic_success_closes_open_early():
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
            }
        }
    )
    b.reset()
    try:
        b.report_failure("p", "m")
        b.report_failure("p", "m")  # OPEN, trips=1
        assert b.is_open("p", "m")
        b.report_success("p", "m")  # panic success → early close, trips kept
        s = b._states["p|m"]
        assert s["state"] == "closed"
        assert s["trips"] == 1  # escalation memory retained
        assert b.allow("p", "m")
    finally:
        b.reset()
        b.reload(main.load_config())


def test_panic_failure_escalates():
    b = main.get_breaker()
    b.reload(
        {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 2,
                "cooldown_seconds": 120,
                "cooldown_max_seconds": 1800,
            }
        }
    )
    b.reset()
    try:
        b.report_failure("p", "m")
        b.report_failure("p", "m")  # OPEN trips=1, cd 120
        # panic request fails twice → re-trip with escalation
        b.report_failure("p", "m")
        b.report_failure("p", "m")
        s = b._states["p|m"]
        assert s["state"] == "open"
        assert s["trips"] == 2
        assert b._cooldown_for(s) == 240.0
    finally:
        b.reset()
        b.reload(main.load_config())

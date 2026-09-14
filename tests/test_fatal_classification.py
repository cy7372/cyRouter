"""Regression tests for fatal-error classification of provider quota errors.

Kimi Code's billing-cycle exhaustion returns HTTP 403 with type
"access_terminated_error" — none of the legacy insufficient_quota literal
patterns matched, so it fell through the 4xx passthrough: never fatal-marked,
auto stayed pinned on kimi-k3, and the provider chain never skipped the dead
provider (2026-08-14).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402

KIMI_QUOTA_BODY = (
    '{"message":"You\'ve reached your usage limit for this billing cycle. '
    "Your quota will be refreshed in the next cycle. To continue now, "
    "purchase extra usage or upgrade your plan: "
    'https://www.kimi.com/membership/subscription?tab=quota",'
    '"type":"access_terminated_error"}'
)


def _clean():
    main._failed_models.clear()
    main._failed_provider_models.clear()


def test_kimi_access_terminated_classified_as_insufficient_quota():
    assert main._classify_error_body(KIMI_QUOTA_BODY) == (403, "insufficient_quota")


def test_cb_fail_quota_marks_model_and_provider():
    _clean()
    try:
        main._cb_fail("kimi code", "kimi-k3", status=403, body_text=KIMI_QUOTA_BODY)
        assert main._is_model_failed("kimi-k3")
        assert main._is_provider_model_failed("kimi code", "kimi-k3")
        # quota-class provider cooldown is the long one (3600s default)
        ts, cd = main._failed_provider_models[("kimi code", "kimi-k3")]
        assert cd == main._provider_fallback_cooldown("insufficient_quota")
    finally:
        _clean()


def test_auto_health_drops_after_fatal_mark():
    _clean()
    try:
        assert main._is_healthy("kimi-k3")  # before: marked live
        main._cb_fail("kimi code", "kimi-k3", status=403, body_text=KIMI_QUOTA_BODY)
        assert not main._is_healthy("kimi-k3")  # after: model-level mark
    finally:
        _clean()


def test_normalize_preserves_status_but_types_quota():
    nb, fs, _ = main._normalize_upstream_error(KIMI_QUOTA_BODY, 403)
    assert fs == 403
    assert nb["error"]["type"] == "insufficient_quota"
    # original message preserved verbatim (upstream transparency)
    assert "usage limit" in nb["error"]["message"]


def test_normal_403_still_passthrough():
    body = '{"error":{"message":"bad api key","type":"authentication_error"}}'
    c = main._classify_error_body(body)
    assert c is None  # not a quota error
    nb, fs, _ = main._normalize_upstream_error(body, 403)
    assert fs == 403
    assert nb["error"]["type"] == "authentication_error"


# ── Unified classifier + decision table (_classify_upstream_failure / _cb_fail) ──

C = main._classify_upstream_failure


def test_classifier_structured_fields_first():
    kimi = '{"error":{"message":"x","type":"access_terminated_error"}}'
    assert C(status=403, body_text=kimi) == "quota"
    assert (
        C(status=403, body_text='{"error":{"code":"billing_quota_exceeded"}}')
        == "quota"
    )
    assert (
        C(status=429, body_text='{"error":{"type":"rate_limit_exceeded"}}')
        == "rate_limit"
    )
    # message copy never consulted when structured fields decide
    assert (
        C(
            status=429,
            body_text='{"error":{"type":"a_quota_thing","message":"rate limit"}}',
        )
        == "quota"
    )


def test_classifier_status_fallbacks():
    assert C(status=429) == "rate_limit"
    assert C(status=401) == "auth"
    assert C(status=403) == "auth"
    assert C(status=404) == "not_found"
    assert C(status=408) == "timeout"
    assert C(status=500) == "server"
    assert C(status=400) == "client"
    assert C() == "network"  # timeout / EOF / unknown exception


def test_classifier_legacy_keyword_table_still_applies():
    assert C(status=429, body_text='{"error":{"message":"余额不足"}}') == "quota"


def test_classifier_reads_sdk_exception_body():
    class QuotaExc(Exception):
        status_code = 403
        body = {"error": {"type": "access_terminated_error"}}

    class ClientExc(Exception):
        status_code = 400

    assert C(exc=QuotaExc()) == "quota"
    assert C(exc=ClientExc()) == "client"


def test_decision_table_routes_categories():
    _clean()
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
    fails = []
    orig = b.report_failure
    b.report_failure = lambda provider, model: fails.append((provider, model))
    try:
        kimi = '{"error":{"type":"access_terminated_error"}}'
        main._cb_fail("p", "m1", status=403, body_text=kimi)  # quota
        assert main._is_model_failed("m1")
        assert main._failed_provider_models[("p", "m1")][1] == 3600.0
        main._cb_fail("p", "m2", status=401)  # auth: provider only, model intact
        assert not main._is_model_failed("m2")
        assert main._is_provider_model_failed("p", "m2")
        main._cb_fail("p", "m3", status=400)  # client: nothing
        assert not main._is_model_failed("m3")
        assert not main._is_provider_model_failed("p", "m3")
        main._cb_fail("p", "m4", status=502)  # server + network → breaker
        main._cb_fail("p", "m4", status=502)
        main._cb_fail("p", "m5")
        main._cb_fail("p", "m5")
        assert fails == [("p", "m4"), ("p", "m4"), ("p", "m5"), ("p", "m5")]
    finally:
        b.report_failure = orig
        b.reset()
        b.reload(main.load_config())
        _clean()

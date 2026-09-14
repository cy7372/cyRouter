"""Tests for live usage readings: _parse_usages_payload, _iso_to_epoch,
and the live-injection branch of _estimate_local_quota."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402

# Observed Kimi /usages payload (2026-08-26; values are percentage strings)
KIMI_PAYLOAD = {
    "user": {"userId": "u1", "membership": {"level": "LEVEL_ADVANCED"}},
    "usage": {
        "limit": "100",
        "used": "33",
        "remaining": "67",
        "resetTime": "2026-08-31T04:59:15.611860Z",
    },
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": "100",
                "remaining": "100",
                "resetTime": "2026-08-26T10:59:15.611860Z",
            },
        }
    ],
    "parallel": {"limit": "30"},
}


def test_parse_usages_payload():
    p = main._parse_usages_payload(KIMI_PAYLOAD)
    assert p["weekly"]["used_pct"] == 33.0
    assert p["weekly"]["remaining_pct"] == 67.0
    assert p["weekly"]["reset_time"] == "2026-08-31T04:59:15.611860Z"
    assert p["rate"]["hours"] == 5.0
    assert p["rate"]["remaining_pct"] == 100.0
    assert p["parallel_limit"] == "30"
    assert p["membership"] == "LEVEL_ADVANCED"


def test_parse_usages_payload_empty():
    assert main._parse_usages_payload({}) == {
        "weekly": None,
        "rate": None,
        "parallel_limit": None,
        "membership": None,
    }


def test_iso_to_epoch():
    assert main._iso_to_epoch("2026-08-31T04:59:15.611860Z") == 1788152355
    assert main._iso_to_epoch("") == 0
    assert main._iso_to_epoch("garbage") == 0


@pytest.fixture
def _cache():
    main._usages_cache.clear()
    yield main._usages_cache
    main._usages_cache.clear()


def test_estimate_local_quota_prefers_live(_cache):
    p_cfg = {
        "name": "kimi code",
        "base_urls": {"openai": "https://api.kimi.com/coding/v1"},
        "local_quota": {
            "window_days": 7,
            "limit_tokens": 331000000,
            "rate_window_hours": 5,
            "rate_limit_tokens": 67600000,
            "fetch_usages": True,
        },
    }
    _cache["kimi code"] = {
        "fetched_at": time.time(),
        **main._parse_usages_payload(KIMI_PAYLOAD),
    }
    out = main._estimate_local_quota({}, p_cfg, "kimi code", p_cfg["local_quota"])
    assert out["estimated"] is False
    assert out["source"] == "usages"
    assert [l["label"] for l in out["limits"]] == ["周期额度", "5h 频限"]
    assert out["limits"][0]["percentage"] == 33.0
    assert out["limits"][0]["nextResetTime"] == 1788152355
    # rate window: limit 100 - remaining 100 = 0% used
    assert out["limits"][1]["percentage"] == 0.0
    assert out["detail"]["parallel_limit"] == "30"


def test_estimate_local_quota_falls_back_when_stale(_cache):
    p_cfg = {
        "name": "kimi code",
        "base_urls": {"openai": "https://api.kimi.com/coding/v1"},
        "local_quota": {
            "window_days": 7,
            "limit_tokens": 331000000,
            "rate_window_hours": 5,
            "rate_limit_tokens": 67600000,
        },
    }
    _cache["kimi code"] = {
        "fetched_at": time.time() - main._USAGES_STALE_SECONDS - 1,
        **main._parse_usages_payload(KIMI_PAYLOAD),
    }
    out = main._estimate_local_quota({}, p_cfg, "kimi code", p_cfg["local_quota"])
    # falls back to local estimation (no live marker)
    assert out.get("source") != "usages"
    assert out["estimated"] is True


def test_estimate_local_quota_no_cache(_cache):
    p_cfg = {
        "name": "kimi code",
        "base_urls": {"openai": "https://api.kimi.com/coding/v1"},
        "local_quota": {"window_days": 7, "limit_tokens": 331000000},
    }
    out = main._estimate_local_quota({}, p_cfg, "kimi code", p_cfg["local_quota"])
    assert out["estimated"] is True
    assert out["limits"][0]["label"].endswith("估算")


# ── Subscription-exhausted detection (2026-09-11) ─────────────────────────
# Kimi Code with an expired subscription 429s even /usages with an explicit
# insufficient-balance body (observed live 2026-09-11). Consecutive signals
# confirm exhaustion; a 200 clears it immediately (renewal).

EXHAUSTED_429_BODY = (
    '{"code":"resource_exhausted","message":"insufficient balance",'
    '"details":[{"type":"common.error.v1.ErrorDetail",'
    '"value":"CHQSGQoFZW4tVVMSEENyZWRpdHMgdXNlZCB1cC4","debug":{'
    '"reason":"REASON_QUOTA_EXCEEDED","localizedMessage":{'
    '"locale":"en-US","message":"Credits used up."}}}]} '
)


def test_insufficient_balance_signal():
    assert main._insufficient_balance_signal(EXHAUSTED_429_BODY) is True
    # message-based detection
    assert (
        main._insufficient_balance_signal('{"message":"Insufficient Balance"}') is True
    )
    # plain rate-limit 429 — must NOT count toward exhaustion
    assert (
        main._insufficient_balance_signal(
            '{"error":{"type":"rate_limit_exceeded","message":"Too many requests"}}'
        )
        is False
    )
    # non-JSON / malformed
    assert main._insufficient_balance_signal("<html>429</html>") is False
    assert main._insufficient_balance_signal("") is False


class _FakeResp:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def get(self, *a, **kw):
        return self._resp


def _mock_usages_http(monkeypatch, resp):
    monkeypatch.setattr(
        main, "_get_http_client_for_url", lambda *a, **kw: _FakeClient(resp)
    )


_P_CFG = {
    "name": "kimi code",
    "base_urls": {"openai": "https://api.kimi.com/coding/v1"},
    "api_key": "sk-test",
    "local_quota": {"fetch_usages": True},
}


@pytest.fixture
def _streak():
    main._usages_cache.clear()
    main._usages_exhausted_streak.clear()
    main._usages_next_attempt.clear()
    yield
    main._usages_cache.clear()
    main._usages_exhausted_streak.clear()
    main._usages_next_attempt.clear()


@pytest.mark.asyncio
async def test_exhausted_confirmed_after_streak(monkeypatch, _streak):
    """Three consecutive balance-429s confirm exhaustion (no earlier)."""
    _mock_usages_http(monkeypatch, _FakeResp(429, text=EXHAUSTED_429_BODY))
    for i in range(main._USAGES_EXHAUSTED_CONFIRM - 1):
        assert await main._fetch_provider_usages(_P_CFG) is False
        assert (
            main._usages_cache.get("kimi code", {}).get("subscription") != "exhausted"
        )
    assert await main._fetch_provider_usages(_P_CFG) is False
    entry = main._usages_cache["kimi code"]
    assert entry["subscription"] == "exhausted"
    assert entry["exhausted_since"] > 0
    # backoff scheduled
    assert main._usages_next_attempt["kimi code"] > time.time()


@pytest.mark.asyncio
async def test_exhausted_stays_and_recovers(monkeypatch, _streak):
    """Confirmed exhaustion persists across further 429s; one 200 clears it."""
    _mock_usages_http(monkeypatch, _FakeResp(429, text=EXHAUSTED_429_BODY))
    for _ in range(main._USAGES_EXHAUSTED_CONFIRM):
        await main._fetch_provider_usages(_P_CFG)
    assert main._usages_cache["kimi code"]["subscription"] == "exhausted"
    # one more 429 — stays exhausted, entry refreshed
    await main._fetch_provider_usages(_P_CFG)
    assert main._usages_cache["kimi code"]["subscription"] == "exhausted"
    assert "fetched_at" not in main._usages_cache["kimi code"]
    # renewal: a 200 live reading replaces the exhausted entry entirely
    _mock_usages_http(monkeypatch, _FakeResp(200, json_data=KIMI_PAYLOAD))
    assert await main._fetch_provider_usages(_P_CFG) is True
    entry = main._usages_cache["kimi code"]
    assert "subscription" not in entry
    assert entry["fetched_at"] > 0
    assert "kimi code" not in main._usages_exhausted_streak
    assert "kimi code" not in main._usages_next_attempt


@pytest.mark.asyncio
async def test_plain_429_not_counted(monkeypatch, _streak):
    """Bare 429s (rate window / gateway) never confirm exhaustion."""
    _mock_usages_http(
        monkeypatch, _FakeResp(429, text='{"error":{"type":"rate_limit_exceeded"}}')
    )
    for _ in range(main._USAGES_EXHAUSTED_CONFIRM + 3):
        await main._fetch_provider_usages(_P_CFG)
    assert main._usages_cache.get("kimi code") is None
    assert main._usages_exhausted_streak.get("kimi code", {}).get("count", 0) == 0


def test_estimate_local_quota_exhausted(_cache):
    """Exhausted entry renders a definitive bar, not a meaningless estimate."""
    p_cfg = {
        "name": "kimi code",
        "base_urls": {"openai": "https://api.kimi.com/coding/v1"},
        "local_quota": {
            "window_days": 7,
            "limit_tokens": 331000000,
            "rate_window_hours": 5,
            "rate_limit_tokens": 67600000,
        },
    }
    _cache["kimi code"] = {
        "subscription": "exhausted",
        "exhausted_since": time.time() - 3600,
        "last_429_at": time.time(),
    }
    out = main._estimate_local_quota({}, p_cfg, "kimi code", p_cfg["local_quota"])
    assert out["subscription"] == "exhausted"
    assert out["estimated"] is False
    assert out["source"] == "usages"
    assert out["limits"] == [
        {
            "type": "TOKENS_LIMIT",
            "percentage": 100.0,
            "nextResetTime": 0,
            "label": "订阅已过期",
        }
    ]
    assert "续费后自动恢复" in out["detail"]["hint"]


class _FakeProvider:
    def __init__(self, name):
        self.name = name


def _fake_candidates(model):
    chain = {"glm-5.3": ["zhipu-coding"], "kimi-k3": ["kimi code"]}
    return [(_FakeProvider(p), None) for p in chain.get(model, [])]


def test_pressure_exhausted_is_full(monkeypatch, _cache):
    """An exhausted subscription = full pressure → auto prefers fallback."""
    monkeypatch.setattr(main, "_get_provider_candidates", _fake_candidates)
    _cache["kimi code"] = {
        "subscription": "exhausted",
        "exhausted_since": time.time() - 60,
    }
    assert main._model_quota_pressure("kimi-k3") == 100.0


def test_pressure_exhausted_min_with_healthy(_cache):
    """MIN semantics: a healthy second provider keeps a chained model usable.

    A hypothetical two-provider chain [exhausted, healthy] must report the
    healthy provider's usage, not the exhausted 100%.
    """

    def _two_provider_candidates(model):
        return [(_FakeProvider("kimi code"), None), (_FakeProvider("kimi alt"), None)]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(main, "_get_provider_candidates", _two_provider_candidates)
    try:
        _cache["kimi code"] = {
            "subscription": "exhausted",
            "exhausted_since": time.time() - 60,
        }
        _cache["kimi alt"] = {
            "fetched_at": time.time(),
            "weekly": {"used_pct": 10.0, "reset_time": None},
            "rate": None,
        }
        assert main._model_quota_pressure("kimi-k3") == 10.0
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_exhausted_persisted_and_restored(monkeypatch, _streak, tmp_path):
    """Exhaustion survives a restart: verdict is written to disk on confirm
    and restored into the cache at import time (2026-09-11: restart used to
    forget it and the UI showed a misleading estimate for ~15min)."""
    state_file = tmp_path / "usages_state.json"
    monkeypatch.setattr(main, "_USAGES_STATE_PATH", state_file)

    _mock_usages_http(monkeypatch, _FakeResp(429, text=EXHAUSTED_429_BODY))
    for _ in range(main._USAGES_EXHAUSTED_CONFIRM):
        await main._fetch_provider_usages(_P_CFG)
    assert state_file.exists(), "confirmed exhaustion must be persisted"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["kimi code"]["subscription"] == "exhausted"
    assert saved["kimi code"]["exhausted_since"] > 0

    # simulate restart: cache cleared, loader restores from disk
    main._usages_cache.clear()
    main._load_persisted_usages_state()
    restored = main._usages_cache["kimi code"]
    assert restored["subscription"] == "exhausted"
    assert restored["exhausted_since"] == saved["kimi code"]["exhausted_since"]


@pytest.mark.asyncio
async def test_renewal_clears_persisted_state(monkeypatch, _streak, tmp_path):
    """A live 200 after exhaustion rewrites the state file without the
    marker — the next restart must not resurrect the stale verdict."""
    state_file = tmp_path / "usages_state.json"
    monkeypatch.setattr(main, "_USAGES_STATE_PATH", state_file)

    _mock_usages_http(monkeypatch, _FakeResp(429, text=EXHAUSTED_429_BODY))
    for _ in range(main._USAGES_EXHAUSTED_CONFIRM):
        await main._fetch_provider_usages(_P_CFG)
    assert "kimi code" in json.loads(state_file.read_text(encoding="utf-8"))

    _mock_usages_http(monkeypatch, _FakeResp(200, json_data=KIMI_PAYLOAD))
    assert await main._fetch_provider_usages(_P_CFG) is True
    assert json.loads(state_file.read_text(encoding="utf-8")) == {}

    main._usages_cache.clear()
    main._load_persisted_usages_state()
    assert "kimi code" not in main._usages_cache

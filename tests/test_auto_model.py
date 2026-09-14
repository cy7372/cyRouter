"""Unit tests for `_resolve_auto_model` in main.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
from main import _AUTO_DEFAULTS  # noqa: E402
from main import _resolve_auto_model as _resolve_orig


def _modernize(cfg):
    """Fixtures historically used the legacy provider-level ``models`` list;
    routing now reads only the top-level ``models`` chain map (2026-09-04
    cleanup removed the legacy provider-models fallback layer)."""
    cfg = dict(cfg)
    models = dict(cfg.get("models") or {})
    for p in cfg.get("providers") or []:
        for m in p.get("models") or []:
            models.setdefault(m, [p["name"]])
    cfg["models"] = models
    return cfg


def _resolve_auto_model(body, cfg, **kwargs):
    return _resolve_orig(body, _modernize(cfg), **kwargs)


def _patch_health(monkeypatch):
    """Synthetic-cfg models have no chain in the REAL config, and
    _is_healthy's chain-liveness layer reads the real config — patch it
    down to the model-mark layer these tests exercise."""
    monkeypatch.setattr(main, "_is_healthy", lambda m: not main._is_model_failed(m))


SIMPLE = _AUTO_DEFAULTS["simple_model"]
COMPLEX = _AUTO_DEFAULTS["complex_model"]


@pytest.fixture(autouse=True)
def _clean_auto_state():
    """Derived fallback session keys make same-body tests collide across
    tests — always start/end with clean sticky + failed-model state."""
    main._session_model.clear()
    main._failed_models.clear()
    yield
    main._session_model.clear()
    main._failed_models.clear()


# ── Model selection ──


def test_short_message_without_tools_routes_to_simple():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, {}) == SIMPLE


def test_short_message_with_tools_routes_to_complex():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "search", "input_schema": {}}],
    }
    assert _resolve_auto_model(body, {}) == COMPLEX


def test_long_message_without_tools_routes_to_complex():
    body = {"messages": [{"role": "user", "content": "x" * 250}]}
    assert _resolve_auto_model(body, {}) == COMPLEX


def test_custom_threshold_from_config():
    cfg = {"auto": {"thinking_threshold_chars": 10}}
    body1 = {"messages": [{"role": "user", "content": "hi"}]}
    body2 = {"messages": [{"role": "user", "content": "explain quantum mechanics"}]}
    assert _resolve_auto_model(body1, cfg) == SIMPLE
    assert _resolve_auto_model(body2, cfg) == COMPLEX


# ── Thinking control ──


def test_short_message_to_glm_keeps_thinking_passthrough():
    # Thinking control is pure passthrough: auto resolution must NOT mutate
    # body["thinking"] — previously it injected {"type": "disabled"} on
    # short GLM turns to dodge the upstream truncation glitch; that papered
    # over an upstream bug now fixed properly (model remap + context
    # preflight), so the client's thinking setting is forwarded unchanged.
    cfg = {"auto": {"simple_model": "glm-5.2"}}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    target = _resolve_auto_model(body, cfg)
    assert target == "glm-5.2"
    assert "thinking" not in body


def test_long_message_to_glm_keeps_thinking():
    cfg = {"auto": {"simple_model": "glm-5.2"}}
    body = {"messages": [{"role": "user", "content": "x" * 250}]}
    target = _resolve_auto_model(body, cfg)
    assert target == "glm-5.2"
    assert "thinking" not in body


def test_tools_force_complex_but_short_message_keeps_thinking():
    body = {
        "messages": [{"role": "user", "content": "ok"}],
        "tools": [{"name": "read", "input_schema": {}}],
    }
    target = _resolve_auto_model(body, {})
    assert target == COMPLEX  # gl with tools → complex
    assert "thinking" not in body


def test_tools_with_long_message_keeps_thinking():
    body = {
        "messages": [
            {"role": "user", "content": "refactor the auth module" + "z" * 250}
        ],
        "tools": [{"name": "edit", "input_schema": {}}],
    }
    target = _resolve_auto_model(body, {})
    assert target == COMPLEX
    assert "thinking" not in body


def test_explicit_thinking_not_overridden():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled"},
    }
    _resolve_auto_model(body, {})
    assert body["thinking"] == {"type": "enabled"}


def test_non_glm_target_no_thinking_injection():
    cfg = {"auto": {"simple_model": "qwen-max"}}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    _resolve_auto_model(body, cfg)
    assert "thinking" not in body


# ── Fallback ──


def test_primary_missing_uses_fallback(monkeypatch):
    _patch_health(monkeypatch)
    cfg = {
        "auto": {
            "simple_model": "ghost",
            "simple_fallback": "qwen-turbo",
            "complex_model": "glm-5.2",
        },
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    assert (
        _resolve_auto_model({"messages": [{"role": "user", "content": "hi"}]}, cfg)
        == "qwen-turbo"
    )


def test_empty_fallback_returns_target_anyway():
    cfg = {
        "auto": {
            "simple_model": "ghost",
            "simple_fallback": "",
            "complex_model": "glm-5.2",
        },
        "providers": [{"name": "p1", "models": ["glm-5.2"]}],
    }
    # Both primary and fallback missing → returns target, will 404
    assert (
        _resolve_auto_model({"messages": [{"role": "user", "content": "hi"}]}, cfg)
        == "ghost"
    )


# ── Image routing ──


def test_image_routes_to_vision_model(monkeypatch):
    _patch_health(monkeypatch)
    cfg = {
        "auto": {"vision_model": "qwen-vl-max"},
        "providers": [{"name": "p1", "models": ["qwen-vl-max", "glm-5.2"]}],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image", "source": {"type": "base64", "data": "abc"}},
                ],
            },
        ]
    }
    assert _resolve_auto_model(body, cfg) == "qwen-vl-max"


def test_openai_image_url_routes_to_vision_model(monkeypatch):
    """OpenAI image_url / input_image formats must also trigger vision routing."""
    _patch_health(monkeypatch)
    cfg = {
        "auto": {"vision_model": "qwen-vl-max"},
        "providers": [{"name": "p1", "models": ["qwen-vl-max", "glm-5.2"]}],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            },
        ]
    }
    assert _resolve_auto_model(body, cfg) == "qwen-vl-max"
    body["messages"][0]["content"][1] = {"type": "input_image", "image_url": "data:..."}
    assert _resolve_auto_model(body, cfg) == "qwen-vl-max"


def test_image_without_vision_model_rejects():
    """No vision_model configured + no vision-capable candidate → explicit
    503 instead of falling through to a text-only model (which 400s on the
    image block upstream — the 2026-08-30 zhipu code-1210 incident)."""
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": {"type": "base64", "data": "abc"}},
                ],
            },
        ]
    }
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _resolve_auto_model(body, {})
    assert ei.value.status_code == 503


def test_image_vision_model_text_only_is_skipped(monkeypatch):
    """2026-09-11 incident: vision_model misconfigured to a TEXT-ONLY model
    (glm-5.3 — what the config drifted to after the kimi subscription
    expired). Step 1 must check vision capability, not just health: blindly
    forwarding the image turn made volcano-apig answer 400 code 1210
    ("messages.content.type 参数非法"). The turn must fall through to a
    vision-capable tier candidate instead."""
    import main

    _patch_health(monkeypatch)
    main._failed_models.clear()
    cfg = {
        "auto": {
            "vision_model": "glm-5.3",
            "simple_model": "glm-5.3",
            "complex_model": "glm-5.3",
            "vision_fallback": "glm-5.3-flash",
        },
        "model_meta": {"glm-5.3-flash": {"vision": True}},
        "providers": [
            {
                "name": "p1",
                "models": ["glm-5.3", "glm-5.3-flash"],
            }
        ],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            },
        ]
    }
    m = _resolve_auto_model(body, cfg)
    assert m != "glm-5.3", "text-only vision_model must never receive an image turn"
    # falls to the vision-capable vision_fallback (glm-5.3-flash keeps the image)
    assert m == "glm-5.3-flash"


def test_image_with_vision_model_down_uses_vision_capable_fallback(monkeypatch):
    """vision_model in failed-model cooldown → pick a vision-capable tier
    candidate (name heuristic counts: -vl) instead of a text-only model."""
    import main

    _patch_health(monkeypatch)
    main._failed_models.clear()
    main._mark_model_failed("qwen-vl-max")
    cfg = {
        "auto": {
            "vision_model": "qwen-vl-max",
            "complex_model": "glm-5.3",
            "complex_fallback": "qwen-vl-plus",
        },
        "providers": [
            {"name": "p1", "models": ["qwen-vl-max", "glm-5.3", "qwen-vl-plus"]}
        ],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image", "source": {"type": "base64", "data": "abc"}},
                ],
            },
        ]
    }
    assert _resolve_auto_model(body, cfg) == "qwen-vl-plus"
    main._failed_models.clear()


def test_image_with_vision_model_down_no_vision_candidate_503():
    """Exact incident shape: vision_model (kimi-k3, vision via model_meta)
    quota-exhausted → cooldown; tier candidates are text-only GLMs → the
    old code fell through to glm-5.3 and zhipu answered 400 code 1210.
    With no vision_fallback configured, must raise a truthful 503."""
    import main

    main._failed_models.clear()
    main._mark_model_failed("kimi-k3")
    cfg = {
        "auto": {
            "vision_model": "kimi-k3",
            "simple_model": "glm-5.3-mini",
            "complex_model": "glm-5.3",
            "simple_fallback": "kimi-k3",
            "complex_fallback": "kimi-k3",
        },
        "model_meta": {"kimi-k3": {"vision": True}},
        "providers": [{"name": "p1", "models": ["kimi-k3", "glm-5.3", "glm-5.3-mini"]}],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            },
        ]
    }
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _resolve_auto_model(body, cfg)
    assert ei.value.status_code == 503
    assert "图片路由失败" in ei.value.detail
    main._failed_models.clear()


def test_image_vision_fallback_text_model_strips_images(monkeypatch):
    """vision_fallback pointing at a TEXT-ONLY model (a FICTIONAL
    glm-5.3-mini — builtin_meta knows the real glm-5.3-flash is
    vision-capable, zhipu-verified 2026-08-29) → degraded routing: image
    blocks replaced by an honest placeholder so the text turn proceeds
    instead of a 400."""
    import main

    # _is_healthy's chain-liveness layer reads the REAL config; the
    # fictional model has no chain there — patch down to the model-mark
    # layer this test exercises.
    _patch_health(monkeypatch)
    main._failed_models.clear()
    main._mark_model_failed("kimi-k3")
    cfg = {
        "auto": {
            "vision_model": "kimi-k3",
            "vision_fallback": "glm-5.3-mini",
            "simple_model": "glm-5.3-mini",
            "complex_model": "glm-5.3",
            "simple_fallback": "kimi-k3",
            "complex_fallback": "kimi-k3",
        },
        "model_meta": {"kimi-k3": {"vision": True}},
        "providers": [{"name": "p1", "models": ["kimi-k3", "glm-5.3", "glm-5.3-mini"]}],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            },
        ]
    }
    assert _resolve_auto_model(body, cfg) == "glm-5.3-mini"
    blocks = body["messages"][0]["content"]
    assert blocks[1]["type"] == "text"
    assert "图片内容已省略" in blocks[1]["text"]
    # original text block untouched
    assert blocks[0]["text"] == "看图"
    main._failed_models.clear()


def test_image_vision_fallback_vision_model_keeps_images(monkeypatch):
    """vision_fallback that supports vision (name heuristic: -vl) → used
    as-is, image blocks preserved."""
    import main

    _patch_health(monkeypatch)
    main._failed_models.clear()
    main._mark_model_failed("kimi-k3")
    cfg = {
        "auto": {
            "vision_model": "kimi-k3",
            "vision_fallback": "qwen-vl-plus",
            "complex_model": "glm-5.3",
        },
        "model_meta": {"kimi-k3": {"vision": True}},
        "providers": [{"name": "p1", "models": ["kimi-k3", "glm-5.3", "qwen-vl-plus"]}],
    }
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image", "source": {"type": "base64", "data": "x"}},
                ],
            },
        ]
    }
    assert _resolve_auto_model(body, cfg) == "qwen-vl-plus"
    assert body["messages"][0]["content"][1]["type"] == "image"
    main._failed_models.clear()


def test_image_vision_fallback_down_503():
    """vision_model AND vision_fallback both in cooldown → 503."""
    import main

    main._failed_models.clear()
    main._mark_model_failed("kimi-k3")
    main._mark_model_failed("glm-5.3-flash")
    cfg = {
        "auto": {
            "vision_model": "kimi-k3",
            "vision_fallback": "glm-5.3-flash",
            "complex_model": "glm-5.3",
        },
        "model_meta": {"kimi-k3": {"vision": True}},
        "providers": [
            {"name": "p1", "models": ["kimi-k3", "glm-5.3", "glm-5.3-flash"]}
        ],
    }
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _resolve_auto_model(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,x"},
                            },
                        ],
                    }
                ]
            },
            cfg,
        )
    assert ei.value.status_code == 503
    main._failed_models.clear()


# ── Model failure ──


def test_failed_model_skipped_in_fresh_routing():
    import main

    main._failed_models.clear()
    main._mark_model_failed("glm-5.2")

    cfg = {
        "auto": {"complex_model": "glm-5.2", "complex_fallback": "deepseek-v4-pro"},
        "providers": [{"name": "p1", "models": ["glm-5.2", "deepseek-v4-pro"]}],
    }
    body = {"messages": [{"role": "user", "content": "x" * 250}]}
    assert _resolve_auto_model(body, cfg) == "deepseek-v4-pro"
    main._failed_models.clear()


def test_failed_model_cooldown_expires():
    import main

    main._failed_models.clear()
    main._failed_models["glm-5.2"] = main.time.time() - main._FAILED_MODEL_COOLDOWN - 1
    assert not main._is_model_failed("glm-5.2")
    assert "glm-5.2" not in main._failed_models


# ── Edge cases ──


def test_empty_body():
    assert _resolve_auto_model({}, {}) == SIMPLE


def test_content_as_list_of_blocks():
    body = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    }
    assert _resolve_auto_model(body, {}) == SIMPLE


def test_short_chinese_message():
    body = {"messages": [{"role": "user", "content": "你好"}]}
    assert _resolve_auto_model(body, {}) == SIMPLE


# ── Session stickiness ──


def test_session_upgrade_jumps_through_stickiness():
    """Upgrade always switches immediately, even within the sticky TTL —
    quality first. (Old behavior locked the session to the weak model.)"""
    import main

    main._session_model.clear()
    cfg = {
        "auto": {"simple_model": "qwen-turbo", "complex_model": "glm-5.2"},
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body1 = {"messages": [{"role": "user", "content": "hi"}], "_session_id": "sess-1"}
    assert _resolve_auto_model(body1, cfg) == "qwen-turbo"
    # Second request in same session: long → rule says complex → upgrade
    # through the sticky simple model.
    body2 = {
        "messages": [{"role": "user", "content": "x" * 500}],
        "_session_id": "sess-1",
    }
    assert _resolve_auto_model(body2, cfg) == "glm-5.2"
    main._session_model.clear()


def test_session_downgrade_stays_within_ttl():
    """Downgrade within the sticky TTL keeps the current (stronger) model
    unconditionally — no cost comparison needed."""
    import main

    main._session_model.clear()
    cfg = {
        "auto": {"simple_model": "qwen-turbo", "complex_model": "glm-5.2"},
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body1 = {
        "messages": [{"role": "user", "content": "x" * 500}],
        "_session_id": "sess-1b",
    }
    assert _resolve_auto_model(body1, cfg) == "glm-5.2"
    body2 = {"messages": [{"role": "user", "content": "hi"}], "_session_id": "sess-1b"}
    assert _resolve_auto_model(body2, cfg) == "glm-5.2"
    main._session_model.clear()


def test_session_stickiness_invalidated_on_unhealthy(monkeypatch):
    import main

    _patch_health(monkeypatch)
    main._session_model.clear()
    main._failed_models.clear()
    cfg = {
        "auto": {
            "simple_model": "qwen-turbo",
            "simple_fallback": "glm-5.2",
            "complex_model": "glm-5.2",
        },
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body1 = {"messages": [{"role": "user", "content": "hi"}], "_session_id": "sess-2"}
    t1 = _resolve_auto_model(body1, cfg)
    assert t1 == "qwen-turbo"
    # Mark sticky model failed → next request must re-pick.
    main._mark_model_failed("qwen-turbo")
    body2 = {"messages": [{"role": "user", "content": "hi"}], "_session_id": "sess-2"}
    t2 = _resolve_auto_model(body2, cfg)
    assert t2 != "qwen-turbo"  # re-picked because unhealthy
    main._failed_models.clear()
    main._session_model.clear()


# ── Model-level health: quota marks only, never transient failures ──


def test_pick_model_ignores_circuit_open_model():
    """Transient failures (stream kills, timeouts) trip the provider-level
    breaker but must NOT make auto switch models — only quota marks do."""
    import main
    from breaker import get_breaker

    b = get_breaker()
    b._states.clear()
    # Force a known threshold — the global singleton reads from runtime config
    # where the breaker may be disabled (failure_threshold=0), making range()
    # run zero iterations and never trip the circuit.
    orig_threshold = b.failure_threshold
    b.failure_threshold = 3
    try:
        # Trip the breaker for "ghost" model.
        for _ in range(b.failure_threshold):
            b.report_failure("ghost-prov", "ghost")
        avail = {"ghost", "qwen-turbo"}
        # Circuit open ≠ unhealthy at model level: auto stays on ghost;
        # the provider chain handles failover to another provider instead.
        picked = main._pick_model("ghost", "qwen-turbo", avail)
        assert picked == "ghost"
    finally:
        b.failure_threshold = orig_threshold
        b._states.clear()


def test_pick_model_skips_quota_failed_model():
    """Quota exhaustion (insufficient_quota / rate_limit_exceeded) marks the
    model failed → _pick_model moves to the fallback (GLM↔kimi ping-pong)."""
    import main

    main._mark_model_failed("ghost")
    try:
        avail = {"ghost", "kimi-k3"}
        assert main._pick_model("ghost", "kimi-k3", avail) == "kimi-k3"
    finally:
        main._failed_models.pop("ghost", None)


# ── Fallback session key (prefix-hash) ──


def test_auto_session_key_stable_for_same_prefix():
    body1 = {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
        ]
    }
    # Same prefix, appended turns → same key (cache prefix unchanged).
    body2 = {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "next question"},
        ]
    }
    assert main._auto_session_key(body1) == main._auto_session_key(body2)
    assert main._auto_session_key(body1).startswith("auto-")


def test_auto_session_key_changes_on_different_prefix():
    body1 = {"messages": [{"role": "user", "content": "hello"}]}
    body2 = {"messages": [{"role": "user", "content": "goodbye"}]}
    assert main._auto_session_key(body1) != main._auto_session_key(body2)


def test_auto_session_key_empty_body():
    assert main._auto_session_key({}) == ""
    assert main._auto_session_key({"messages": []}) == ""


def test_fallback_session_key_enables_stickiness():
    """No _session_id header: the derived key still sticks the session —
    turn 2 (short tail after a long turn 1) is a downgrade within the TTL
    and stays on the complex model."""
    cfg = {
        "auto": {"simple_model": "qwen-turbo", "complex_model": "glm-5.2"},
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body1 = {"messages": [{"role": "user", "content": "x" * 500}]}
    assert _resolve_auto_model(body1, cfg) == "glm-5.2"
    body2 = {
        "messages": [
            {"role": "user", "content": "x" * 500},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "ok"},  # short tail → rule says simple
        ]
    }
    assert _resolve_auto_model(body2, cfg) == "glm-5.2"  # sticky via derived key


# ── Context-size-aware tier pick ──


def test_large_context_forces_complex_tier():
    """Short tail message but huge history → complex (context size matters)."""
    cfg = {
        "auto": {
            "simple_model": "qwen-turbo",
            "complex_model": "glm-5.2",
            "context_threshold_tokens": 1000,
        },
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body = {
        "messages": [
            {"role": "user", "content": "x" * 3000},
            {"role": "assistant", "content": "y" * 3000},
            {"role": "user", "content": "ok"},  # tiny tail, ~1500 tok total
        ]
    }
    assert _resolve_auto_model(body, cfg) == "glm-5.2"


def test_small_context_stays_simple():
    cfg = {
        "auto": {
            "simple_model": "qwen-turbo",
            "complex_model": "glm-5.2",
            "context_threshold_tokens": 1000,
        },
        "providers": [{"name": "p1", "models": ["qwen-turbo", "glm-5.2"]}],
    }
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, cfg) == "qwen-turbo"


# ── Cache-aware re-pick after sticky TTL expiry ──

_PRICING = {
    "cheap": {"input": 1.0, "output": 2.0, "cache_input": 0.1},
    "expensive": {"input": 100.0, "output": 200.0, "cache_input": 10.0},
}
_CACHE_CFG = {
    "auto": {"simple_model": "cheap", "complex_model": "expensive"},
    "providers": [{"name": "p1", "models": ["cheap", "expensive"]}],
    "pricing": _PRICING,
}


def _expire_sticky(sid: str):
    model, ts = main._session_model[sid]
    main._session_model[sid] = (model, ts - main._SESSION_STICKY_TTL - 1)


def _seed_sticky(sid: str, model: str):
    """Pre-seed an already-expired sticky entry (ts=0.0 is always expired)."""
    main._session_model[sid] = (model, 0.0)


def test_cache_aware_stays_on_warm_model_when_cheaper(monkeypatch):
    """Downgrade, TTL expired: rule pick is cold cheap, sticky is the warm
    complex model (NOT a fallback slot — a sticky FALLBACK with a healthy
    primary restores to the primary instead, see
    test_auto_restores_primary_after_recovery) → staying on the warm cache
    is cheaper."""
    _patch_health(monkeypatch)
    cfg = {
        "auto": {
            "simple_model": "cheap",
            "complex_model": "warm",
        },
        "providers": [{"name": "p1", "models": ["cheap", "warm"]}],
        "pricing": {
            **_PRICING,
            "warm": {"input": 1.0, "output": 2.0, "cache_input": 0.05},
        },
    }
    sid = "sess-cache-stay"
    _seed_sticky(sid, "warm")
    body = {"messages": [{"role": "user", "content": "ok"}], "_session_id": sid}
    # rule -> cheap (downgrade, tier 1→0); stay warm = ctx*(0.85*0.05+0.15*1.0)
    # + 2048*2 < switch cheap cold = ctx*1.0 + 2048*2 -> cache-aware stay
    assert _resolve_auto_model(body, cfg) == "warm"


def test_cache_aware_switches_when_rule_pick_cheaper():
    """Downgrade + TTL expired: staying on warm expensive costs far more than
    switching to cold cheap → switch wins."""
    sid = "sess-cache-switch"
    body1 = {
        "messages": [{"role": "user", "content": "x" * 500}],  # → expensive
        "_session_id": sid,
    }
    assert _resolve_auto_model(body1, _CACHE_CFG) == "expensive"
    _expire_sticky(sid)
    body2 = {"messages": [{"role": "user", "content": "ok"}], "_session_id": sid}
    assert _resolve_auto_model(body2, _CACHE_CFG) == "cheap"  # switch wins


def test_cache_aware_disabled_uses_rule():
    """cache_aware_routing=False → expired same-tier sticky ignored, rule pick."""
    cfg = {
        "auto": {
            "simple_model": "cheap",
            "simple_fallback": "warm",
            "complex_model": "expensive",
            "cache_aware_routing": False,
        },
        "providers": [{"name": "p1", "models": ["cheap", "warm", "expensive"]}],
        "pricing": {
            **_PRICING,
            "warm": {"input": 1.0, "output": 2.0, "cache_input": 0.05},
        },
    }
    sid = "sess-cache-off"
    _seed_sticky(sid, "warm")
    body = {"messages": [{"role": "user", "content": "ok"}], "_session_id": sid}
    # Same scenario as the stay test, but the flag is off → rule pick wins.
    assert _resolve_auto_model(body, cfg) == "cheap"


def test_cache_aware_no_pricing_uses_rule():
    """No pricing data → cannot compare costs → rule pick (no crash)."""
    cfg = {
        "auto": {
            "simple_model": "cheap",
            "simple_fallback": "warm",
            "complex_model": "expensive",
        },
        "providers": [{"name": "p1", "models": ["cheap", "warm", "expensive"]}],
    }
    sid = "sess-cache-noprice"
    _seed_sticky(sid, "warm")
    body = {"messages": [{"role": "user", "content": "ok"}], "_session_id": sid}
    assert _resolve_auto_model(body, cfg) == "cheap"


def test_cache_aware_plan_model_wins_downgrade(monkeypatch):
    """Downgrade from a plan-covered model: marginal cost 0 → stay even though
    the rule pick is a pay-per-token model."""
    _patch_health(monkeypatch)
    cfg = {
        "auto": {"simple_model": "cheap", "complex_model": "expensive"},
        "providers": [{"name": "p1", "models": ["cheap", "expensive"]}],
        "pricing": {
            **_PRICING,
            "expensive": {**_PRICING["expensive"], "plan_monthly": 469},
        },
    }
    sid = "sess-cache-plan"
    _seed_sticky(sid, "expensive")
    body = {"messages": [{"role": "user", "content": "ok"}], "_session_id": sid}
    assert _resolve_auto_model(body, cfg) == "expensive"  # plan marginal cost 0


def test_auto_turn_cost_math():
    cost_warm = main._auto_turn_cost("cheap", 1000, 2048, _CACHE_CFG, 0.85)
    cost_cold = main._auto_turn_cost("cheap", 1000, 2048, _CACHE_CFG, 0.0)
    assert cost_warm is not None and cost_cold is not None
    # warm: 1000*(0.85*0.1+0.15*1.0) + 2048*2 = 235 + 4096 = 4331 / 1e6
    assert abs(cost_warm - 4331 / 1e6) < 1e-9
    # cold: 1000*1.0 + 4096 = 5096 / 1e6
    assert abs(cost_cold - 5096 / 1e6) < 1e-9
    assert main._auto_turn_cost("ghost", 1000, 2048, _CACHE_CFG, 0.85) is None
    # Missing cache_input → treated as no discount.
    cfg2 = {"pricing": {"m": {"input": 2.0, "output": 4.0}}}
    cost2 = main._auto_turn_cost("m", 1000, 0, cfg2, 0.9)
    assert cost2 is not None
    assert abs(cost2 - 2000 / 1e6) < 1e-9
    # plan_monthly → marginal cost 0 regardless of usage.
    cfg3 = {"pricing": {"m": {"input": 2.0, "output": 4.0, "plan_monthly": 469}}}
    assert main._auto_turn_cost("m", 1000, 2048, cfg3, 0.85) == 0.0


def test_api_models_includes_auto_virtual_model(monkeypatch):
    """/api/models exposes the virtual auto model so clients (pi via
    cyrouter-sync) can select it. Limits are the MIN across tier candidates
    (safe for auto-compact when a session falls back to the smaller model);
    vision follows vision_model being configured."""
    import asyncio

    cfg = {
        "auto": {
            "simple_model": "glm-5.2",
            "complex_model": "glm-5.2",
            "simple_fallback": "kimi-k3",
            "complex_fallback": "kimi-k3",
            "vision_model": "qwen-vl-max",
        },
        "providers": [
            {
                "name": "p1",
                "type": "openai",
                "models": ["glm-5.2", "kimi-k3", "qwen-vl-max"],
            }
        ],
        "model_meta": {
            "glm-5.2": {"context": 1000000, "max_output": 65536},
            "kimi-k3": {"context": 240000, "max_output": 32768},
            "qwen-vl-max": {"context": 128000, "max_output": 8192},
        },
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    result = asyncio.run(main.api_list_models())
    auto = next(m for m in result["models"] if m["id"] == "auto")
    # min(glm 1M, kimi 240K) — vision_model does not drag the min down.
    assert auto["context_window"] == 240000
    assert auto["max_output_tokens"] == 32768
    assert auto["capabilities"]["vision"] is True
    assert auto["pricing"] is None


def test_api_models_omits_auto_when_unconfigured(monkeypatch):
    import asyncio

    cfg = {
        "providers": [{"name": "p1", "type": "openai", "models": ["glm-5.2"]}],
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    result = asyncio.run(main.api_list_models())
    assert all(m["id"] != "auto" for m in result["models"])


# ── Provider-chain-aware health: ALL providers down → model unhealthy ──


class _FakeProvider:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def _breaker_test_state():
    """Give the global breaker a known config and clean state; restore after."""
    b = main.get_breaker()
    saved = (b.failure_threshold, b.cooldown_seconds, b.half_open_max)
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
    main._failed_provider_models.clear()
    yield b
    b.reset()
    main._failed_provider_models.clear()
    b.failure_threshold, b.cooldown_seconds, b.half_open_max = saved


def test_single_provider_chain_circuit_open_makes_model_unhealthy(
    monkeypatch, _breaker_test_state
):
    """glm-5.3 scenario: chain=[zhipu-coding], circuit OPEN → unhealthy."""
    monkeypatch.setattr(
        main,
        "_get_provider_candidates",
        lambda model: [(_FakeProvider("zhipu-coding"), None)],
    )
    b = _breaker_test_state
    assert main._is_healthy("glm-5.3")
    b.report_failure("zhipu-coding", "glm-5.3")  # 1 < threshold, CLOSED
    assert main._is_healthy("glm-5.3")
    b.report_failure("zhipu-coding", "glm-5.3")  # → OPEN
    assert not main._is_healthy("glm-5.3")


def test_one_open_provider_on_multi_chain_still_healthy(
    monkeypatch, _breaker_test_state
):
    """ALL-down semantics: one tripped provider must NOT flip the model
    (the pre-2026-08-13 is_model_open over-broadness bug)."""
    monkeypatch.setattr(
        main,
        "_get_provider_candidates",
        lambda model: [(_FakeProvider("a"), None), (_FakeProvider("b"), None)],
    )
    b = _breaker_test_state
    b.report_failure("a", "m")
    b.report_failure("a", "m")  # a OPEN
    assert main._is_healthy("m")  # b still live
    b.report_failure("b", "m")
    b.report_failure("b", "m")  # b OPEN too → chain all down
    assert not main._is_healthy("m")


def test_all_providers_fatal_marked_makes_model_unhealthy(
    monkeypatch, _breaker_test_state
):
    monkeypatch.setattr(
        main,
        "_get_provider_candidates",
        lambda model: [(_FakeProvider("zhipu-coding"), None)],
    )
    assert main._is_healthy("glm-5.3")
    main._mark_provider_model_failed("zhipu-coding", "glm-5.3", cooldown=60)
    assert not main._is_healthy("glm-5.3")


def _mutual_backup_cfg():
    return {
        "auto": {
            "simple_model": "glm-5.3",
            "complex_model": "glm-5.3",
            "simple_fallback": "kimi-k3",
            "complex_fallback": "kimi-k3",
        },
        "models": {"glm-5.3": ["zhipu-coding"], "kimi-k3": ["kimi code"]},
    }


def _mutual_backup_candidates(model):
    chain = {"glm-5.3": ["zhipu-coding"], "kimi-k3": ["kimi code"]}
    return [(_FakeProvider(p), None) for p in chain.get(model, [])]


def test_auto_falls_back_when_primary_chain_all_down(monkeypatch, _breaker_test_state):
    """5.3 不行 → kimi：primary 链全灭时 auto 切 fallback 模型。"""
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    b = _breaker_test_state
    b.report_failure("zhipu-coding", "glm-5.3")
    b.report_failure("zhipu-coding", "glm-5.3")  # zhipu OPEN → 5.3 unhealthy
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _mutual_backup_cfg()) == "kimi-k3"


def test_auto_restores_primary_after_recovery(monkeypatch, _breaker_test_state):
    """kimi  sticky 期间 primary 恢复健康 → 立即切回 5.3（不被粘性/
    cache-aware 钉在 fallback 上）。"""
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    main._session_set("sess-restore", "kimi-k3")
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "_session_id": "sess-restore",
    }
    assert _resolve_auto_model(body, _mutual_backup_cfg()) == "glm-5.3"


def test_auto_returns_primary_when_both_down(monkeypatch, _breaker_test_state):
    """kimi 也不行 → 回到 5.3（give-up 返回 target，让链层出 503/半开探测）。"""
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    b = _breaker_test_state
    for m, p in (("glm-5.3", "zhipu-coding"), ("kimi-k3", "kimi code")):
        b.report_failure(p, m)
        b.report_failure(p, m)  # both chains all down
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _mutual_backup_cfg()) == "glm-5.3"


# ── Quota-pressure deprioritization (live /usages readings) ──


@pytest.fixture
def _usages():
    main._usages_cache.clear()
    yield main._usages_cache
    main._usages_cache.clear()


def _pressure_cfg():
    return {
        "auto": {
            "simple_model": "glm-5.3",
            "complex_model": "glm-5.3",
            "simple_fallback": "kimi-k3",
            "complex_fallback": "kimi-k3",
            "quota_pressure_threshold": 90.0,
        },
        "models": {"glm-5.3": ["zhipu-coding"], "kimi-k3": ["kimi code"]},
    }


def _seed_pressure(provider: str, used_pct: float):
    main._usages_cache[provider] = {
        "fetched_at": __import__("time").time(),
        "weekly": {"used_pct": used_pct, "reset_time": None},
        "rate": None,
    }


def test_pressure_prefers_fallback(monkeypatch, _usages):
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("zhipu-coding", 95.0)  # glm-5.3's provider nearly exhausted
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _pressure_cfg()) == "kimi-k3"


def test_no_pressure_normal_pick(monkeypatch, _usages):
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("zhipu-coding", 34.0)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _pressure_cfg()) == "glm-5.3"


def test_pressure_both_pressured_stays_normal(monkeypatch, _usages):
    """Both models pressured → no better alternative → normal pick."""
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("zhipu-coding", 95.0)
    _seed_pressure("kimi code", 92.0)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _pressure_cfg()) == "glm-5.3"


def test_stale_reading_ignored(monkeypatch, _usages):
    """Stale readings (> staleness window) must not penalize the model."""
    import time as _t

    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("zhipu-coding", 99.0)
    main._usages_cache["zhipu-coding"]["fetched_at"] = (
        _t.time() - main._USAGES_STALE_SECONDS - 1
    )
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, _pressure_cfg()) == "glm-5.3"


def test_sticky_session_escapes_pressure(monkeypatch, _usages):
    """Session sticky on the pressured model → released, picks the
    un-pressured rule target immediately (not pinned by TTL/cost-stay)."""
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("kimi code", 95.0)
    main._session_set("sess-press", "kimi-k3")
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "_session_id": "sess-press",
    }
    assert _resolve_auto_model(body, _pressure_cfg()) == "glm-5.3"


def test_pressure_threshold_zero_disables(monkeypatch, _usages):
    monkeypatch.setattr(main, "_get_provider_candidates", _mutual_backup_candidates)
    _seed_pressure("zhipu-coding", 99.0)
    cfg = _pressure_cfg()
    cfg["auto"]["quota_pressure_threshold"] = 0
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert _resolve_auto_model(body, cfg) == "glm-5.3"


# ── In-request reroute (2026-08-30): exclude parameter ──
# A model that died fatally mid-request (quota 403 / rate-limit mark) is
# excluded from re-resolution so the retry lands on a live model.

_REROUTE_CFG = {
    "auto": {
        "simple_model": "glm-5.3-flash",
        "simple_fallback": "kimi-k3",
        "complex_model": "glm-5.3",
        "complex_fallback": "kimi-k3",
    },
    "providers": [{"name": "p1", "models": ["glm-5.3-flash", "glm-5.3", "kimi-k3"]}],
}


def test_exclude_dead_simple_target_uses_fallback():
    body = {"messages": [{"role": "user", "content": "hi"}]}  # simple tier
    assert (
        _resolve_auto_model(body, _REROUTE_CFG, exclude={"glm-5.3-flash"}) == "kimi-k3"
    )


def test_exclude_dead_complex_target_uses_fallback():
    body = {"messages": [{"role": "user", "content": "x" * 250}]}  # complex
    assert _resolve_auto_model(body, _REROUTE_CFG, exclude={"glm-5.3"}) == "kimi-k3"


def test_exclude_releases_sticky_and_repoints_session():
    import time as _t

    main._session_model["sess-x"] = ("kimi-k3", _t.time())
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "_session_id": "sess-x",
    }
    assert _resolve_auto_model(body, _REROUTE_CFG, exclude={"kimi-k3"}) == (
        "glm-5.3-flash"
    )
    # sticky map now points at the live model
    assert main._session_model["sess-x"][0] == "glm-5.3-flash"


def test_exclude_all_gives_up_on_rule_target():
    """Everything excluded → _pick_model give-up returns the rule target
    (retry dispatches to the dead chain; the reroute wrapper then terminates
    via its resolved-in-tried guard)."""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert (
        _resolve_auto_model(body, _REROUTE_CFG, exclude={"glm-5.3-flash", "kimi-k3"})
        == "glm-5.3-flash"
    )


# ── In-request reroute: the _run_auto_reroute wrapper loop ──


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeRequest:
    def __init__(self):
        import types

        self.state = types.SimpleNamespace()
        self.headers: dict = {}


def _auto_body():
    return {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


def test_reroute_wrapper_non_auto_single_call():
    import asyncio

    calls = []

    async def impl(request, body, _auto_exclude=None):
        calls.append(_auto_exclude)
        return _FakeResp(200)

    resp = asyncio.run(
        main._run_auto_reroute(
            _FakeRequest(), {"model": "glm-5.3", "messages": []}, impl
        )
    )
    assert resp.status_code == 200
    assert calls == [None]


def test_reroute_wrapper_success_first_try_no_retry():
    import asyncio

    calls = []

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = "kimi-k3"
        calls.append(_auto_exclude)
        return _FakeResp(200)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 200
    assert calls == [None]


def test_reroute_wrapper_fatal_then_ok():
    """First model dies fatally (marked dead → unhealthy) → re-resolve with
    exclusion → second model serves the request. 2026-08-30 incident shape."""
    import asyncio

    main._failed_models["kimi-k3"] = main.time.time()  # fatal mark set by 403
    seen_excludes = []

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = (
            "kimi-k3" if _auto_exclude is None else "glm-5.3"
        )
        seen_excludes.append(_auto_exclude)
        return _FakeResp(403) if _auto_exclude is None else _FakeResp(200)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 200
    assert seen_excludes == [None, {"kimi-k3"}]


def test_reroute_wrapper_non_fatal_error_no_retry(monkeypatch):
    """4xx with the model still healthy (plain passthrough, e.g. bad request
    params) must NOT reroute — retrying on another model just spreads damage."""
    import asyncio

    monkeypatch.setattr(main, "_is_healthy", lambda m: True)
    calls = []

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = "glm-5.3"
        calls.append(_auto_exclude)
        return _FakeResp(400)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 400
    assert calls == [None]


def test_reroute_wrapper_unpinned_request_no_retry():
    """impl cleared cy_auto_resolved (client pinned model/provider) → never
    reroute even on error."""
    import asyncio

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = ""
        return _FakeResp(403)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 403


def test_reroute_wrapper_caps_at_three_dead_models(monkeypatch):
    import asyncio

    monkeypatch.setattr(main, "_is_healthy", lambda m: False)
    n = {"i": 0}

    async def impl(request, body, _auto_exclude=None):
        n["i"] += 1
        request.state.cy_auto_resolved = f"m{n['i']}"
        return _FakeResp(403)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 403
    assert n["i"] == 4  # initial + 3 reroutes, then stop


def test_reroute_wrapper_terminates_on_repeat_model(monkeypatch):
    """Re-resolution gives up and returns the SAME dead model (give-up
    semantics) → wrapper must not loop forever."""
    import asyncio

    monkeypatch.setattr(main, "_is_healthy", lambda m: False)
    calls = []

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = "m1"
        calls.append(_auto_exclude)
        return _FakeResp(403)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 403
    assert len(calls) == 2  # second call re-resolved to m1 again → stop


# ── Empty-200 reroute (2026-09-12 yinor incident) ──
# Upstream answered HTTP 200 with genuinely empty content (ok_empty
# fall-through after in-provider retries). The client received nothing yet,
# so the wrapper re-resolves on another model — same safety argument as
# fatal errors, but WITHOUT marking the model dead globally.


def _jsonresp(payload, status_code=200):
    from fastapi.responses import JSONResponse

    return JSONResponse(payload, status_code=status_code)


def _empty_openai_200():
    return _jsonresp(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "created": 1,
            "model": "kimi-k3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def _content_openai_200():
    return _jsonresp(
        {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "created": 1,
            "model": "glm-5.3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def test_empty_200_reroutes_to_next_model():
    """yinor incident shape: model A answers 200-but-empty → reroute lands
    on model B which produces content. Model A stays un-marked (healthy)."""
    import asyncio

    assert main._is_healthy("kimi-k3")  # not marked dead by anyone
    seen_excludes = []

    async def impl(request, body, _auto_exclude=None):
        if _auto_exclude is None:
            request.state.cy_auto_resolved = "kimi-k3"
            seen_excludes.append(_auto_exclude)
            return _empty_openai_200()
        request.state.cy_auto_resolved = "glm-5.3"
        seen_excludes.append(_auto_exclude)
        return _content_openai_200()

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    import json as _j

    assert _j.loads(bytes(resp.body)) ["choices"][0]["message"]["content"] == "hi"
    assert seen_excludes == [None, {"kimi-k3"}]
    assert main._is_healthy("kimi-k3")  # empty-200 must NOT mark it dead globally


def test_toolcall_only_200_is_not_empty():
    """A legitimate tool-only turn (content empty, tool_calls present) must
    NOT trigger reroute — _is_empty_response counts tool_calls as content."""
    import asyncio

    calls = []
    resp_payload = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "glm-5.3",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    async def impl(request, body, _auto_exclude=None):
        request.state.cy_auto_resolved = "glm-5.3"
        calls.append(_auto_exclude)
        return _jsonresp(resp_payload)

    resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
    assert resp.status_code == 200
    assert calls == [None]  # single call — no reroute


def test_all_models_empty_caps_at_three_reroutes():
    """Every model answers 200-but-empty → cap at 3 dead models, then return
    the last empty 200 truthfully (no synthesised errors)."""
    import asyncio

    monkey = pytest.MonkeyPatch()
    monkey.setattr(main, "_is_healthy", lambda m: False)
    try:
        n = {"i": 0}

        async def impl(request, body, _auto_exclude=None):
            n["i"] += 1
            request.state.cy_auto_resolved = f"m{n['i']}"
            return _empty_openai_200()

        resp = asyncio.run(main._run_auto_reroute(_FakeRequest(), _auto_body(), impl))
        assert resp.status_code == 200
        assert n["i"] == 4  # initial + 3 reroutes, then stop (truthful 200)
    finally:
        monkey.undo()


def test_anthropic_shape_empty_200_detected():
    """The anthropic endpoint wrapper (anthropic_shape=True) must detect its
    own empty shape: content blocks list present but all blocks empty."""
    from fastapi.responses import JSONResponse

    empty_anthropic = JSONResponse(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "stop_reason": "end_turn",
        }
    )
    assert main._resp_payload_is_empty(empty_anthropic, anthropic_shape=True)
    # Same payload probed with the OpenAI shape must NOT be classified by the
    # OpenAI branch (choices missing → False, not a false positive).
    assert not main._resp_payload_is_empty(empty_anthropic, anthropic_shape=False)


def test_fake_resp_without_body_is_never_empty():
    """StreamingResponse (and legacy fakes without .body) must be skipped by
    construction — mid-stream drops can never be re-dispatched."""
    assert not main._resp_payload_is_empty(_FakeResp(200), anthropic_shape=False)


def test_cache_store_skips_empty_response(monkeypatch):
    """ok_empty 200 must not poison the cache (a cached empty 200 would
    bypass ALL retry layers — cache sits first in the pipeline)."""

    class _Cache:
        enabled = True
        stored = []

        def set(self, body, data):
            self.stored.append(data)

    c = _Cache()
    monkeypatch.setattr(main, "get_cache", lambda: c)
    body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}

    main._cache_store(body, _empty_openai_200())
    assert c.stored == []  # empty response dropped

    main._cache_store(body, _content_openai_200())
    assert len(c.stored) == 1  # real content cached as before


# ── Regression (2026-09-07): anthropic impl signature vs wrapper convention ──
# The /anthropic/v1/messages endpoint routes through _run_auto_reroute with
# impl_kwargs={"path": path}. _anthropic_messages_once used to declare
# `path` as the 2nd POSITIONAL parameter, so the wrapper's uniform
# impl(request, body, **kwargs) call raised "got multiple values for
# argument 'path'" → every Anthropic-protocol request 500'd since 08-30.
# The impl must accept (request, body, ..., path=..., _auto_exclude=...).


def test_anthropic_impl_signature_matches_wrapper_convention():
    import inspect

    sig = inspect.signature(main._anthropic_messages_once)
    params = list(sig.parameters.values())
    # First two positional params must be (request, body) — exactly what
    # _run_auto_reroute passes positionally.
    assert [p.name for p in params[:2]] == ["request", "body"]
    # `path` must be keyword-with-default so impl(request, body, path=p)
    # and impl(request, body, _auto_exclude=x, path=p) both work.
    assert "path" in sig.parameters
    assert sig.parameters["path"].default == ""


def test_anthropic_impl_invocable_like_wrapper_does():
    import asyncio
    from typing import Any, cast

    # Same typing note as _run_auto_reroute: impl only touches
    # request.state — the stub is a legal stand-in for Request.
    fake_req: Any = cast(Any, _FakeRequest())

    async def run_both_paths():
        # Mirror the two call shapes _run_auto_reroute produces.
        body = {
            "model": "no-such-model",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
        # Non-auto path: impl(request, body, **impl_kwargs) → must raise
        # HTTPException(404), NOT TypeError("multiple values for 'path'").
        try:
            await main._anthropic_messages_once(fake_req, dict(body), path="")
            raised = None
        except Exception as e:
            raised = e
        assert not isinstance(raised, TypeError), (
            f"wrapper-style call raised TypeError: {raised!r}"
        )
        # Auto path: unknown model resolution 404s cleanly too.
        try:
            await main._anthropic_messages_once(
                fake_req,
                dict(body, model="no-such-model"),
                _auto_exclude=None,
                path="",
            )
            raised2 = None
        except Exception as e:
            raised2 = e
        assert not isinstance(raised2, TypeError), (
            f"reroute-style call raised TypeError: {raised2!r}"
        )

    asyncio.run(run_both_paths())


# ── Regression (2026-09-07): anthropic dual-auth headers ──
# Volcengine API Gateway's /compatible (Anthropic protocol) consumer auth
# only reads `Authorization: Bearer` — the standard x-api-key alone 401s.
# The anthropic raw-proxy must send BOTH headers. Verified harmless on
# zhipu / deepseek / kimi coding anthropic endpoints.


def test_anthropic_upstream_sends_dual_auth_headers(monkeypatch):
    import asyncio

    captured = {}

    async def fake_nonstream(**kwargs):
        captured.update(kwargs)
        # Minimal successful Anthropic-shaped response, no fallback.
        return (
            type(
                "R",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "json": lambda self=None: {
                        "id": "msg_x",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
            )(),
            False,
        )

    class _P:
        name = "volcano-apig"

    monkeypatch.setattr(
        main, "_anthropic_raw_nonstream_once", fake_nonstream, raising=True
    )
    monkeypatch.setattr(
        main,
        "_get_upstreams_for_model",
        lambda model, force_provider=None: [
            (
                "https://gw.example/compatible",
                "sk-test",
                "qwen3.8-flash",
                "anthropic",
                _P(),
            )
        ],
        raising=True,
    )

    from typing import Any, cast

    fake_req: Any = cast(Any, _FakeRequest())
    body = {
        "model": "qwen3.8-flash",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    asyncio.run(main._anthropic_messages_once(fake_req, dict(body)))
    hdrs = captured.get("headers") or {}
    assert hdrs.get("x-api-key") == "sk-test"
    assert hdrs.get("authorization") == "Bearer sk-test"

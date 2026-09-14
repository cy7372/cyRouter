"""Builtin pricing catalog tests (2026-09-10).

背景：价格此前只存在于本机 config.json，全新安装（默认 config =
{port,host,providers:[],routes:[]}）pricing 生来为空 —— 发布版实例上
glm-5.3 等所有模型成本显示 0/—。BUILTIN_PRICING 随 exe 分发兜底，
解析链与 BUILTIN_MODEL_META 同构：config.pricing[m] 同名条目整体赢，
内置永不写回 config、不随导出走。

覆盖：
  - effective_pricing / effective_pricing_map 的覆盖语义
  - router._entry_metrics 在"全新安装"（无 pricing 配置）下用内置价记账
  - main._auto_turn_cost 同样吃到内置价（cache-aware 路由不再静默退回规则选档）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
import router  # noqa: E402
from builtin_pricing import (  # noqa: E402
    BUILTIN_PRICING,
    effective_pricing,
    effective_pricing_map,
)

# ── 解析语义 ────────────────────────────────────────────────────────


def test_user_entry_wins_wholesale():
    cfg = {
        "pricing": {
            "glm-5.3": {"input": 99, "output": 199, "cache_input": 9},
        }
    }
    p = effective_pricing(cfg, "glm-5.3")
    assert p == {"input": 99, "output": 199, "cache_input": 9}
    # plan_monthly 不做字段级合并 —— 用户条目整体替换内置条目
    assert p is not None and "plan_monthly" not in p


def test_builtin_fills_gap_on_fresh_install():
    # 全新安装：无 pricing 键 → 内置兜底
    assert effective_pricing({}, "glm-5.3") == BUILTIN_PRICING["glm-5.3"]
    assert effective_pricing({"pricing": {}}, "kimi-k3") == BUILTIN_PRICING["kimi-k3"]
    assert effective_pricing({}, "totally-unknown-model") is None


def test_empty_user_entry_does_not_shadow_builtin():
    # 空字典条目无信息量，不应遮蔽内置价（想显式禁用价格请写非空条目）
    cfg = {"pricing": {"glm-5.3": {}}}
    assert effective_pricing(cfg, "glm-5.3") == BUILTIN_PRICING["glm-5.3"]


def test_map_merges_config_over_builtin():
    cfg = {"pricing": {"kimi-k3": {"input": 30, "output": 150}}}
    m = effective_pricing_map(cfg)
    assert m["kimi-k3"] == {"input": 30, "output": 150}  # 用户赢
    assert m["glm-5.3"] == BUILTIN_PRICING["glm-5.3"]  # 内置补位
    # 空条目被滤掉，不遮蔽内置
    cfg2 = {"pricing": {"glm-5.2": {}}}
    assert effective_pricing_map(cfg2)["glm-5.2"] == BUILTIN_PRICING["glm-5.2"]


def test_catalog_shape():
    # 每个条目都有 input/output 数值（成本计算的硬依赖）
    for name, p in BUILTIN_PRICING.items():
        assert isinstance(p.get("input"), (int, float)), name
        assert isinstance(p.get("output"), (int, float)), name
    # 事故主角必须在册
    assert "glm-5.3" in BUILTIN_PRICING
    assert "kimi-k3" in BUILTIN_PRICING


# ── 记账与路由消费点 ────────────────────────────────────────────────


def _entry(model="glm-5.3", inp=1000, out=100, cr=0, cc=0):
    return {
        "model": model,
        "provider": "https://example.test/v4",
        "status": "ok",
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
        },
    }


def test_entry_metrics_uses_builtin_on_fresh_install(monkeypatch):
    """全新安装（config 无 pricing）下 _entry_metrics 也要算出真实成本。"""
    monkeypatch.setattr(router, "load_config", lambda: {"providers": [], "models": {}})
    m = router._entry_metrics(_entry(inp=1_000_000, out=1_000_000), {"providers": []})
    # 理论价 input=6 output=24（RMB/M）→ 1M in + 1M out = 30 元
    assert abs(m["cost"] - 30.0) < 0.01
    # glm-5.3 内置 plan_monthly=469 → plan_covered：累计层（_accumulate_day_stat）
    # 不计入实付、把 pay_cost 记为 plan_saved（此处保持原始值是既有契约）
    assert m["plan_covered"] is True


def test_entry_metrics_user_price_overrides_builtin(monkeypatch):
    cfg = {"providers": [], "pricing": {"glm-5.3": {"input": 1, "output": 1}}}
    monkeypatch.setattr(router, "load_config", lambda: cfg)
    m = router._entry_metrics(_entry(inp=1_000_000, out=1_000_000), cfg)
    # 用户价 1+1 → 2 元；用户条目无 plan_monthly → 非套餐覆盖
    assert abs(m["cost"] - 2.0) < 0.01
    assert m["plan_covered"] is False
    assert abs(m["pay_cost"] - 2.0) < 0.01


def test_auto_turn_cost_uses_builtin(monkeypatch):
    """cache-aware 路由：无配置价时按内置价比较，而不是静默退回规则选档。"""
    monkeypatch.setattr(main, "load_config", lambda: {"providers": [], "models": {}})
    # deepseek-v4-pro 内置 = V4.1-Flash 折算价 input=2.13（非套餐；
    # 2026-09-14 起官方将其路由到 Flash 计费，目录同步）
    cost = main._auto_turn_cost(
        "deepseek-v4-pro", ctx_tokens=1_000_000, out_tokens=0, cfg={}, cache_ratio=0.0
    )
    assert cost is not None
    assert abs(cost - 2.13) < 0.001
    # kimi-k3 内置 plan_monthly → 套餐内边际成本 0
    assert main._auto_turn_cost("kimi-k3", 1_000_000, 0, {}, 0.0) == 0.0
    # 未知模型仍返回 None（回退规则选档）
    assert main._auto_turn_cost("mystery-model", 100, 0, {}, 0.0) is None

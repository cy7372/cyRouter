"""每日自动检查的自动采纳护栏测试（2026-09-10，冲突审计后 v2）。

核心边界：自动采纳只碰 config.pricing 里已有条目的模型、CNY 单值、
无促销、跳变在 max_ratio_change 内。任何存疑口径跳过留人工。

v2 关键回归：采纳必须用 fetch 之后的最新 config（防并发写丢失），
不能用检查开始时的旧快照整份写回。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402


def _src(models):
  return {"aliyun": {"status": "ok", "models": models}}


def _cfg(pricing, **pc):
  return {"pricing": pricing, "pricing_check": {"auto_adopt": True, **pc}}


def _adopt(fresh_cfg, sources):
  """绑定 fresh_cfg 为 load_config() 返回值后执行采纳。"""
  main.load_config = lambda: fresh_cfg
  return main._auto_adopt_from_check(sources)


def test_auto_adopt_updates_existing_entry(monkeypatch):
  saved = []
  monkeypatch.setattr(main, "save_config", lambda cfg: saved.append(cfg))
  cfg = _cfg({"qwen3.8-max": {"input": 12, "output": 36}})
  src = _src(
    {
      "qwen3.8-max": {
        "input": 14.988,
        "output": 44.965,
        "currency": "CNY",
        "verdict": "differs",
      }
    }
  )
  adopted = _adopt(cfg, src)
  assert len(adopted) == 1
  a = adopted[0]
  assert a["model"] == "qwen3.8-max"
  assert a["input"] == [12, 14.988] and a["output"] == [36, 44.965]
  assert cfg["pricing"]["qwen3.8-max"]["input"] == 14.988
  assert saved  # 只在采纳发生时写盘


def test_auto_adopt_skips_promo_and_range_and_same(monkeypatch):
  monkeypatch.setattr(main, "save_config", lambda cfg: None)
  cfg = _cfg(
    {
      "a": {"input": 1, "output": 2},
      "b": {"input": 1, "output": 2},
      "c": {"input": 1, "output": 2},
    }
  )
  src = _src(
    {
      "a": {  # 限时折扣 — 跳过
        "input": 0.8,
        "output": 1.6,
        "currency": "CNY",
        "promo": "限时8折",
        "verdict": "differs",
      },
      "b": {  # 区间价 — verdict 本就是 info，不进护栏
        "input": [0.66, 1.32],
        "output": [1.98, 3.96],
        "currency": "CNY",
        "verdict": "info",
      },
      "c": {"input": 1, "output": 2, "verdict": "same"},
    }
  )
  adopted = _adopt(cfg, src)
  assert adopted == []
  assert cfg["pricing"]["a"]["input"] == 1  # 未被促销价覆盖


def test_auto_adopt_jump_breaker(monkeypatch):
  monkeypatch.setattr(main, "save_config", lambda cfg: None)
  # 12→36 = 3 倍跳变，超默认 max_ratio_change=1.0 — 拒绝（疑似提取错位）
  cfg = _cfg({"x": {"input": 12, "output": 36}})
  src = _src(
    {
      "x": {
        "input": 36,
        "output": 108,
        "currency": "CNY",
        "verdict": "differs",
      }
    }
  )
  assert _adopt(cfg, src) == []
  assert cfg["pricing"]["x"]["input"] == 12
  # 腰斩以内（12→7，ratio 0.58 ≥ 0.5）允许
  src["aliyun"]["models"]["x"]["input"] = 7
  src["aliyun"]["models"]["x"]["output"] = 21
  adopted = _adopt(cfg, src)
  assert len(adopted) == 1
  assert cfg["pricing"]["x"]["input"] == 7


def test_auto_adopt_never_creates_entry_for_builtin_only(monkeypatch):
  """builtin 兜底的模型（config 无条目）不新建——保护「跟随内置」语义。"""
  monkeypatch.setattr(main, "save_config", lambda cfg: None)
  cfg = _cfg({"known": {"input": 1, "output": 2}})
  src = _src(
    {
      "qwen3-max": {  # 只在 BUILTIN_PRICING，config 无条目
        "input": 2.5,
        "output": 10,
        "currency": "CNY",
        "verdict": "differs",
      }
    }
  )
  assert _adopt(cfg, src) == []
  assert "qwen3-max" not in cfg["pricing"]


def test_auto_adopt_keeps_other_fields(monkeypatch):
  """cache_input / plan_monthly 等非价字段保留，只换 input/output。"""
  monkeypatch.setattr(main, "save_config", lambda cfg: None)
  cfg = _cfg(
    {"m": {"input": 1, "output": 2, "cache_input": 0.1, "max_context": 131072}}
  )
  src = _src(
    {
      "m": {
        "input": 1.2,
        "output": 2.4,
        "currency": "CNY",
        "verdict": "differs",
      }
    }
  )
  _adopt(cfg, src)
  e = cfg["pricing"]["m"]
  assert e["input"] == 1.2 and e["output"] == 2.4
  assert e["cache_input"] == 0.1 and e["max_context"] == 131072


def test_auto_adopt_uses_latest_config_not_stale_snapshot(monkeypatch):
  """并发写丢失回归（2026-09-10 冲突审计）。

  场景：检查开始时 load_config 返回快照 A；fetch 的 ~20s await 期间
  用户在 UI 改配置（save_config 写入快照 B——加了模型链、改了别的
  provider 价格）；采纳必须基于 B 而不是 A，否则 B 的改动被整份
  回滚。实现取 fetch 后的 load_config()（缓存已被 save 替换为 B）。
  """
  saved = []
  monkeypatch.setattr(main, "save_config", lambda cfg: saved.append(cfg))
  stale = _cfg(
    {"x": {"input": 1, "output": 2}, "stale-only": {"input": 9, "output": 9}}
  )
  latest = _cfg({"x": {"input": 1, "output": 2}})
  latest["models"] = {"new-model": ["some-provider"]}  # fetch 期间用户新增
  latest["pricing"]["y"] = {"input": 5, "output": 5}  # fetch 期间用户改价
  src = _src(
    {
      "x": {
        "input": 1.2,
        "output": 2.4,
        "currency": "CNY",
        "verdict": "differs",
      }
    }
  )
  # load_config 返回 latest（save_config 已换掉缓存对象的等价模拟）
  main.load_config = lambda: latest
  main._auto_adopt_from_check(src)
  assert len(saved) == 1
  out = saved[0]
  assert out is latest  # 写回的是最新快照
  assert out["models"] == {"new-model": ["some-provider"]}  # 用户链保留
  assert out["pricing"]["y"] == {"input": 5, "output": 5}  # 用户价格保留
  assert "stale-only" not in out["pricing"]  # 旧快照独有内容不复活
  assert out["pricing"]["x"]["input"] == 1.2  # 采纳本身生效


def test_auto_adopt_skips_when_user_already_fixed(monkeypatch):
  """fetch 期间用户恰好手动改成官网价 — 已一致，不重复写。"""
  saved = []
  monkeypatch.setattr(main, "save_config", lambda cfg: saved.append(cfg))
  cfg = _cfg({"x": {"input": 1.2, "output": 2.4}})
  src = _src(
    {
      "x": {
        "input": 1.2,
        "output": 2.4,
        "currency": "CNY",
        "verdict": "differs",  # verdict 是 fetch 前打的旧快照
      }
    }
  )
  assert _adopt(cfg, src) == []
  assert saved == []  # 无写入

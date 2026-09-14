"""Built-in pricing catalog shipped with cyRouter.

pricing 与 model_meta 同类：模型固有属性（价格跟着模型走，不跟机器走）。
2026-09-10 之前价格只存在于本机 config.json，而全新安装的默认 config 是
{port, host, providers:[], routes:[]} —— pricing 生来为空，所有成本显示
0/—（"发布版 glm-5.3 没价格" 事故）。v1.2.0.5 的导出/导入/克隆白名单
解决"配置搬家"，本目录解决"开箱即有"，两者互补：
克隆过的机器用自己的 pricing，没克隆/新装的机器由内置兜底。

Resolution semantics（对齐 BUILTIN_MODEL_META，见 builtin_meta.py）:

    config.pricing[m]   用户/克隆条目 —— 同名模型整体赢（"yours wins
                        wholesale"），字段级微调直接改自己的条目
    > BUILTIN_PRICING   本目录（随 cyRouter 版本分发）

内置条目永不写回 config.json，导出也只导用户自己的 pricing（与
BUILTIN_MODEL_META 不随导出走同理）。想覆盖内置价格，在 UI/配置里写
同名条目即可。价格单位：RMB / 百万 tokens。

字段：
    input / output / cache_input   按量单价（缓存写/读同价记在 cache_input）
    plan_monthly                   套餐月费（套餐内边际成本≈0，成本统计
                                   按套餐覆盖处理；套餐档位升级后以用户
                                   config 里的同名条目为准）

价格来源：官方 2026-06 定价页 + 套餐档位实付（kimi Allegro ¥699、
zhipu coding ¥469）。新模型接入默认链或价格调整时更新本目录。
"""

from __future__ import annotations

BUILTIN_PRICING: dict[str, dict] = {
    # ── GLM（zhipu coding 套餐 ¥469/月）──
    "glm-5.1": {"input": 6, "output": 24, "cache_input": 1.1, "plan_monthly": 469},
    "glm-5.2": {"input": 6, "output": 24, "cache_input": 1.1, "plan_monthly": 469},
    "glm-5.3": {"input": 6, "output": 24, "cache_input": 1.1, "plan_monthly": 469},
    "glm-5.3-flash": {
        "input": 1.1,
        "output": 3.6,
        "cache_input": 0.22,
        "plan_monthly": 469,
    },
    # ── Kimi（coding 套餐 Allegro ¥699/月）──
    "kimi-k3": {"input": 21.6, "output": 108, "cache_input": 2.16, "plan_monthly": 699},
    # ── DeepSeek（USD 分时计费，此处为 peak 折算 CNY×7.1，off-peak 减半）──
    # 2026-09-14 12:00 (UTC+8) 起 deepseek-v4-pro 全部路由到 V4.1-Flash
    # 并按 Flash 价计费（官方定价页脚注）；v4-flash / v4-flash-vision-exp
    # 旧名仍接受，同样按 Flash 计费。三个名字统一 Flash 口径。
    "deepseek-flash": {"input": 2.13, "output": 8.52, "cache_input": 0.0426},
    "deepseek-v4-pro": {"input": 2.13, "output": 8.52, "cache_input": 0.0426},
    "deepseek-v4-flash": {"input": 2.13, "output": 8.52, "cache_input": 0.0426},
    # ── 通义千问（阿里云 dashscope / token plan）──
    "qwen3.6-plus": {"input": 2, "output": 12},
    "qwen3.6-flash": {"input": 1.2, "output": 7.2},
    "qwen3.7-plus": {"input": 2, "output": 8, "cache_input": 0.4},
    "qwen3.7-max": {"input": 12, "output": 36, "cache_input": 2.4},
    "qwen3.8-max": {"input": 12, "output": 36, "cache_input": 1.5},
    "qwen3.8-flash": {"input": 0.8, "output": 2.7, "cache_input": 0.1},
    # ── 遗留名（model_aliases 目标 / 老链）──
    "qwen-max": {"input": 2.4, "output": 9.6, "cache_input": 0.48},
    "qwen-turbo": {"input": 0.3, "output": 0.6, "cache_input": 0.06},
    "qwen-vl-max": {"input": 1.6, "output": 4, "cache_input": 0.32},
    "text-embedding-v3": {"input": 0.5, "output": 0},
    # ── OpenAI 系（经火山网关等，USD→CNY 记账）──
    "gpt-5.6-luna": {"input": 1.44, "output": 8.64, "cache_input": 0.14},
}


def effective_pricing(cfg: dict, model_id: str) -> dict | None:
    """Effective price entry for one model: user config wins wholesale,
    builtin catalog fills the gap. None = no pricing data anywhere."""
    user = None
    if isinstance(cfg, dict):
        user = (cfg.get("pricing") or {}).get(model_id)
    if isinstance(user, dict) and user:
        return user
    return BUILTIN_PRICING.get(model_id)


def effective_pricing_map(cfg: dict) -> dict:
    """Merged view for payload builders (/api/stats, /api/usage, /api/models).

    User entries override builtin per model; empty dict user entries are
    ignored (they carry no data and would only shadow the builtin entry —
    to nullify a builtin price, give the model a non-entry placeholder
    instead, e.g. {"input": 0})."""
    merged = dict(BUILTIN_PRICING)
    user = cfg.get("pricing") if isinstance(cfg, dict) else None
    if isinstance(user, dict):
        for k, v in user.items():
            if isinstance(v, dict) and v:
                merged[k] = v
    return merged

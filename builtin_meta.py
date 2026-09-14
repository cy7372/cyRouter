"""Built-in model metadata catalog shipped with cyRouter.

`model_meta` describes model-intrinsic facts (context window, output
ceiling, vision/reasoning support) — knowledge that is independent of the
machine and worth shipping with the binary instead of hand-backfilling on
every install. Resolution chain (first hit wins, see
`_resolve_model_limit` in main.py):

    model_overrides[m].limit   UI ⚙ editor (field-level)
    > model_meta[m] (config)   user-written entry (whole-model, wins here)
    > BUILTIN_MODEL_META       this catalog (versioned with cyRouter)
    > pricing[m].max_context   legacy knob
    > _infer_model_limit       family heuristic

Semantics: a user entry with the same model name replaces the builtin
entry WHOLESALE (predictable "yours wins"); field-level tweaks belong in
model_overrides. This also isolates name collisions (a custom model that
happens to be called "glm-5.3" but points elsewhere).

Values were accumulated from production incidents & probes — see AGENTS.md
(截断事故, 视觉实测, 断流保护). Update this catalog when a new model joins
the default chains or a limit is disproven in practice.
"""

BUILTIN_MODEL_META: dict[str, dict] = {
    "kimi-k3": {
        "context": 256000,
        "max_output": 32768,
        "vision": True,
        "reasoning": True,
        "notes": "kimi coding API k3-256k; 输出上限实测>=8192, 32K 安全值; "
        "视觉已实测(kimi code 端点收 base64 图, 2026-08-10)",
    },
    "glm-5.2": {
        "context": 1048576,
        "max_output": 131072,
        "vision": False,
        "reasoning": True,
        "notes": "zhipu coding GLM-5.2; 1M 实测(1029020 ok)",
    },
    "glm-5.3": {
        "context": 1048576,
        "max_output": 131072,
        "vision": False,
        "reasoning": True,
        "notes": "zhipu coding GLM-5.3; 规格沿用 5.2(1M/128K)，实测 200 ok 2026-08-13",
    },
    "glm-5.3-flash": {
        "context": 1048576,
        "max_output": 131072,
        "vision": True,
        "reasoning": True,
        "notes": "GLM-5.3-Flash 320B/18B激活 混合注意力 原生多模态; "
        "coding plan 3x额度; 2026-08-27接入",
    },
    "deepseek-flash": {
        "context": 1048576,
        "max_output": 393216,
        "vision": True,
        "reasoning": True,
        "notes": "V4.1-Flash 2026-09 正式版；原生多模态（vision-exp 已并入）",
    },
    "deepseek-v4-pro": {
        "context": 1048576,
        "max_output": 393216,
        "vision": False,
        "reasoning": True,
        "notes": "2026-09-14 起路由到 V4.1-Flash 计费",
    },
    "deepseek-v4-flash": {
        "context": 1048576,
        "max_output": 393216,
        "vision": False,
        "reasoning": True,
    },
    "qwen3.8-max": {
        "context": 1048576,
        "max_output": 65536,
        "vision": False,
        "reasoning": True,
    },
    "qwen3.7-plus": {
        "context": 1048576,
        "max_output": 65536,
        "vision": False,
        "reasoning": True,
    },
    "qwen3.8-flash": {
        "context": 1000000,
        "max_output": 131072,
        "vision": True,
        "reasoning": True,
        "notes": "阿里云百炼 Qwen3.8-Flash 多模态(图/文/视频输入); "
        "1M上下文/131K输出/262K思维链; 缓存命中0.1; 2026-09-04接入",
    },
    "qwen-vl-max": {
        "context": 32768,
        "max_output": 8192,
        "vision": True,
        "reasoning": False,
        "notes": "legacy VL 档, 8K 输出上限",
    },
    "qwen-max": {
        "context": 131072,
        "max_output": 8192,
        "vision": False,
        "reasoning": False,
        "notes": "legacy 档, dashscope 返回 max_tokens 上限 8192",
    },
    "qwen-turbo": {
        "context": 131072,
        "max_output": 8192,
        "vision": False,
        "reasoning": False,
    },
    "gpt-5.6-luna": {
        "context": 131072,
        "max_output": 8192,
        "vision": False,
        "reasoning": True,
        "notes": "opencode-go 代理模型",
    },
    "text-embedding-v3": {
        "context": 8192,
        "max_output": 0,
        "vision": False,
        "reasoning": False,
        "notes": "embedding, 无输出 token",
    },
}

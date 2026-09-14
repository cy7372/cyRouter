"""内置常用供应商模板（builtin provider templates，2026-09-10）。

动机：全新安装的实例没有任何 provider 配置，用户要自己翻文档找
base_url。模板把四家常用供应商的连接信息（endpoint / key 获取页 /
控制台 / 推荐模型链）随版本内置，UI「从模板添加」预填表单，用户
只需填 API key。

设计约束：
- **模板永不落 config**——只有用户显式添加后才成为 config 条目
  （与 BUILTIN_PRICING 运行时兜底不同：provider 没有用户的 key
  就是死的，兜底无意义，模板只在添加瞬间参与）；
- **不含任何 API key**——key 是每用户私有；
- base_urls 全部来自本机实测（D-Programs/cyRouter 2026-09-10 的
  config，OpenAI + Anthropic 双协议均 200），不是文档抄写；
- suggested_models 引用的模型都在 builtin_pricing.BUILTIN_PRICING
  里（价格/元数据随版本兜底，添加后零配置可记账）；
- volcano-apig 这类**私有 API 网关不内置**（endpoint 因人而异）。

endpoint 知识点（来自 AGENTS.md 实测记录）：
- kimi code 的 OpenAI 端点是 api.kimi.com/coding/v1（/v1 属于路径
  的一部分）；Anthropic 端点是 api.kimi.com/coding（无 /v1）；
- 智谱 coding 套餐走专属路径 open.bigmodel.cn/api/coding/paas/v4
  （与开放平台按量端点 /api/paas/v4 不同，用错端点会 401）；
- deepseek OpenAI 端点无 /v1 后缀（SDK 层拼接）；
- 阿里云百炼 anthropic 端点在 /apps/anthropic（非常见 /anthropic）。
"""

from __future__ import annotations

# brand 键与 web UI 表单 pf-type 的 option value 对齐（本机存量 provider
# 保存的 provider 字段值：aliyun / zhipu / deepseek / kimi / volcengine）。
BUILTIN_PROVIDER_TEMPLATES: dict[str, dict] = {
    "zhipu": {
        "brand": "zhipu",
        "label": "智谱 GLM（Coding 套餐）",
        "default_name": "zhipu-coding",
        "default_protocol": "anthropic",
        "base_urls": {
            "openai": "https://open.bigmodel.cn/api/coding/paas/v4",
            "anthropic": "https://open.bigmodel.cn/api/anthropic",
        },
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "console_url": "",
        "notes": (
            "Coding 套餐 ¥469/月（价格目录已内置 plan_monthly），"
            "注意走 /api/coding/paas/v4 专属端点，开放平台按量端点不可用"
        ),
        "suggested_models": [
            {"model": "glm-5.3", "note": "主力编码模型 1M 上下文"},
            {"model": "glm-5.3-flash", "note": "高速档"},
            {"model": "glm-5.2", "note": "上代主力"},
        ],
    },
    "deepseek": {
        "brand": "deepseek",
        "label": "DeepSeek",
        "default_name": "deepseek",
        "default_protocol": "anthropic",
        "base_urls": {
            "openai": "https://api.deepseek.com",
            "anthropic": "https://api.deepseek.com/anthropic",
        },
        "key_url": "https://platform.deepseek.com/api_keys",
        "console_url": "https://platform.deepseek.com/usage",
        "notes": "按量付费（USD 分时计费，off-peak 减半）；v4.1-Flash 已转正，v4-pro 9/14 起路由到 Flash 计费",
        "suggested_models": [
            {"model": "deepseek-flash", "note": "V4.1-Flash 旗舰 1M 上下文 · 支持图片"},
        ],
    },
    "kimi": {
        "brand": "kimi",
        "label": "Kimi Code（编程套餐）",
        "default_name": "kimi code",
        "default_protocol": "openai",
        "base_urls": {
            "openai": "https://api.kimi.com/coding/v1",
            "anthropic": "https://api.kimi.com/coding",
        },
        "key_url": "https://www.kimi.com/code/console",
        "console_url": "https://www.kimi.com/code/console",
        "notes": (
            "Coding 套餐 ¥699/月（Allegro 档，价格已内置）；"
            "支持官方 /usages 实时用量（local_quota.fetch_usages）"
        ),
        "suggested_models": [
            {"model": "kimi-k3", "note": "256K 上下文 · 支持图片"},
        ],
    },
    "aliyun": {
        "brand": "aliyun",
        "label": "阿里云百炼（通义千问）",
        "default_name": "aliyun-dashscope",
        "default_protocol": "openai",
        "base_urls": {
            "openai": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "anthropic": "https://dashscope.aliyuncs.com/apps/anthropic",
        },
        "key_url": ("https://bailian.console.aliyun.com/?apiKey=1#/api-key"),
        "console_url": "https://bailian.console.aliyun.com/",
        "notes": (
            "Token Plan 订阅与按量付费同端点（订阅在控制台开通）；"
            "Token Plan 无官方额度 API，额度条为本地估算"
        ),
        "suggested_models": [
            {"model": "qwen3.8-max", "note": "旗舰"},
            {"model": "qwen3.8-flash", "note": "高速档 · 支持图片"},
            {"model": "qwen3.7-plus", "note": "均衡档"},
            {"model": "qwen-vl-max", "note": "视觉模型"},
            {"model": "text-embedding-v3", "note": "向量化"},
        ],
    },
}


def provider_templates_payload() -> dict:
    """GET /api/providers/templates 的响应体（静态、无 key）。"""
    return {"templates": list(BUILTIN_PROVIDER_TEMPLATES.values())}

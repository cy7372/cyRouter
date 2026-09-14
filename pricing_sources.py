"""官网定价页抓取器（2026-09-10）——检查结果只进提示通道，永不自动生效。

设计评审结论（当日实测四家定价页）：
  - deepseek / aliyun 静态 HTML 可解析；
  - zhipu（open.bigmodel.cn/pricing）JS 渲染空壳、kimi（platform.moonshot.cn
    /docs/pricing/chat）价格分散在模型子页——两家标 unavailable，诚实呈现
    而不是假装能爬；
  - 语义陷阱真实存在：deepseek 官网是 USD 分时价（off-peak/peak 各半），
    阿里云有阶梯计费（0<Token≤128K / 128K<Token≤256K 不同价）和多地域
    （北京/新加坡/弗吉尼亚/法兰克福价不同）——提取器锚定华北2（北京）
    首区间，分时价保留区间。
    因此 verdict 分三档：same / differs 仅对「CNY 单一价」的可比情形给出，
    区间价或外币一律 info（展示原样，不判差异，禁止一键采纳）。

生效价永远是 config.pricing > BUILTIN_PRICING（wholesale），本模块的
输出只用于 UI 提示与人工核对。页面结构漂移时提取器返回空结果或抛
ExtractError，状态如实上报，不影响任何记账。
"""

from __future__ import annotations

import re
from collections.abc import Callable

# 每源提取结果（单模型）的形状：
#   {"currency": "CNY"|"USD", "input": float|[lo, hi], "output": float|[lo, hi],
#    "cache_input"? / "cache_hit"?: float|[lo, hi], "note"?: str}
# list 表示区间（分时/阶梯），对比层对区间只给 info。


class ExtractError(Exception):
    """页面结构漂移/无法解析——调用方把源标为 error，不猜。"""


def _f(x: str) -> float:
    return float(x)


# ── DeepSeek ────────────────────────────────────────────────────────
# Docusaurus 静态站，价格表是纯 HTML <table>（2026-09-10 确认）。列序 =
# 模型头顺序（deepseek-flash, deepseek-v4-pro）；行序固定：CACHE HIT
# off/peak → CACHE MISS off/peak → OUTPUT off/peak，每行两列价。


def extract_deepseek(html: str) -> dict[str, dict]:
    # 模型头：MODEL</td><td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro<sup>(2)</sup></td>
    names = re.findall(r"MODEL</td>((?:<td>[\w.-]+<sup>\(\d+\)</sup></td>)+)", html)
    if not names:
        raise ExtractError("model header row not found")
    models = re.findall(r"<td>([\w.-]+)<sup>", names[0])
    if not models:
        raise ExtractError("no model names in header")

    # 价格行对：(OFF-PEAK|PEAK)</td><td>$a</td><td>$b</td>（按文档顺序 6 行）
    pairs = re.findall(
        r"<td>(OFF-PEAK|PEAK)</td><td>\$([\d.]+)</td><td>\$([\d.]+)</td>",
        html,
    )
    if len(pairs) < 6:
        raise ExtractError(f"expected 6 price rows, got {len(pairs)}")
    # rows: [hit_off, hit_peak, miss_off, miss_peak, out_off, out_peak]
    rows = [(_f(a), _f(b)) for _, a, b in pairs[:6]]

    out: dict[str, dict] = {}
    for i, m in enumerate(models[:2]):
        out[m] = {
            "currency": "USD",
            "cache_hit": [rows[0][i], rows[1][i]],
            "input": [rows[2][i], rows[3][i]],
            "output": [rows[4][i], rows[5][i]],
            "note": "USD 分时价：off-peak / peak（区间首尾）",
        }
    return out


# ── 阿里云百炼 ──────────────────────────────────────────────────────
# help.aliyun.com 服务端渲染（2026-09-10 确认，页面 ~1.2MB）。每个模型
# 小节一个 tabbed-content-box，内含 id="华北2-北京" 的 section（我们要的
# 区段）；阶梯计费模型 rowspan 多行，取首区间（0<Token≤…）为代表价。


_ALI_TIER_RE = re.compile(r"Token≤\s*([0-9.]+)\s*([KM])")


def extract_aliyun(html: str) -> dict[str, dict]:
    # 所有北京段（每个模型小节一个 tabbed box，id 带递增编号：
    # 华北2-北京 / 华北2-北京-2 / …；穿插的 TTS 小节 id 直接用模型名）
    sections = re.findall(r'id="华北2-北京[^"]*"[^>]*>(.*?)</section>', html, re.S)
    if not sections:
        raise ExtractError("no cn-beijing section found")

    out: dict[str, dict] = {}
    for sec in sections:
        current: str | None = None
        for tr in re.findall(r"<tr>(.*?)</tr>", sec, re.S):
            # 模型 ID 与价格在同一 <tr>（阶梯计费时 rowspan 跨行：模型名
            # 只在首行，后续行只有区间+价格）。名字正则锚定 <p> 后紧跟
            # qwen/text-embedding 前缀，模式列/注释里的模型名不会误匹配。
            m = re.search(r"<p>((?:qwen|text-embedding)[\w.-]*)</p>", tr)
            if m:
                name = m.group(1)
                current = name
                out.setdefault(name, {"currency": "CNY", "tiers": []})
                # 不 continue：模型 ID 与首区间价格常在同一 <tr>
            if current is None:
                continue
            prices = re.findall(r"<td[^>]*><p>([\d.]+)<span[^>]*></span>元</p>", tr)
            # 限时折扣格式：原价<span>2</span>元<strong>（限时<span>8</span>折）</strong>——
            # 记原价并在 note 标注折扣（检查器要暴露的正是这种活动价）
            promo = re.findall(
                r"原价<span[^>]*></span>([\d.]+)<span[^>]*></span>元", tr
            )
            discount = re.search(
                r"限时<span[^>]*></span>([\d.]+)<span[^>]*></span>折", tr
            )
            tier = _ALI_TIER_RE.search(tr)
            if (
                len(prices) >= 2
                or len(promo) >= 2
                or (len(prices) == 1 and "embedding" in current)
            ):
                entry = out[current]
                if "input" not in entry:
                    if len(promo) >= 2:
                        entry["input"] = _f(promo[0])
                        entry["output"] = _f(promo[1])
                        if discount:
                            entry["promo"] = (
                                f"限时{discount.group(1)}折（现价按原价×{float(discount.group(1)) / 10}）"
                            )
                    elif len(prices) >= 2:
                        entry["input"] = _f(prices[0])
                        entry["output"] = _f(prices[1])
                    else:  # embedding 单价列（无输出价）
                        entry["input"] = _f(prices[0])
                        entry["output"] = 0
                        entry["note"] = "北京区原价（embedding 单价格）"
                    if "note" not in entry:
                        entry["tier"] = ("0<" + tier.group(0)) if tier else "无阶梯"
                        entry["note"] = "北京区原价（阶梯取首区间）"
                elif tier and tier.group(0) not in entry["tiers"]:
                    entry["tiers"].append(tier.group(0))
        # 每个 section 重置 current，避免跨段串行
        current = None
    for v in out.values():
        if not v.get("tiers"):
            v.pop("tiers", None)
    return out


# ── 源注册表 ────────────────────────────────────────────────────────
# unavailable 的源不上报提取器；extractor 抛 ExtractError → status=error。


PRICING_SOURCES: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api-docs.deepseek.com/quick_start/pricing",
        "currency": "USD",
        "extractor": extract_deepseek,
    },
    "aliyun": {
        "label": "阿里云百炼（北京）",
        "url": "https://help.aliyun.com/zh/model-studio/billing-for-model-studio",
        "currency": "CNY",
        "extractor": extract_aliyun,
    },
    "zhipu": {
        "label": "智谱开放平台",
        "url": "https://open.bigmodel.cn/pricing",
        "unavailable_reason": "定价页为 JS 渲染，暂不支持自动抓取；套餐价（¥469/月）也不在公网页上",
    },
    "kimi": {
        "label": "Kimi 开放平台",
        "url": "https://platform.moonshot.cn/docs/pricing/chat",
        "unavailable_reason": "价格分散在各模型子页，暂不支持自动抓取；coding 套餐档位以订阅为准",
    },
}


def extract_all(html_by_source: dict[str, str]) -> dict[str, dict]:
    """Parse fetched HTML per source → {source: {status, models|error}}."""
    results: dict[str, dict] = {}
    for key, cfg in PRICING_SOURCES.items():
        if "unavailable_reason" in cfg:
            results[key] = {
                "label": cfg["label"],
                "url": cfg["url"],
                "status": "unavailable",
                "reason": cfg["unavailable_reason"],
                "models": {},
            }
            continue
        html = html_by_source.get(key)
        if html is None:
            results[key] = {
                "label": cfg["label"],
                "url": cfg["url"],
                "status": "error",
                "reason": "fetch failed",
                "models": {},
            }
            continue
        try:
            extractor: Callable[[str], dict] = cfg["extractor"]
            models = extractor(html)
            results[key] = {
                "label": cfg["label"],
                "url": cfg["url"],
                "status": "ok" if models else "empty",
                "reason": "" if models else "页面解析到 0 个模型（结构漂移？）",
                "models": models,
            }
        except ExtractError as e:
            results[key] = {
                "label": cfg["label"],
                "url": cfg["url"],
                "status": "error",
                "reason": str(e),
                "models": {},
            }
    return results

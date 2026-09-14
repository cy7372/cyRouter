"""官网价格检查提取器测试（2026-09-10）。

fixture 是真实定价页的**最小结构切片**（deepseek Docusaurus 静态表 /
aliyun SSR 北京段），结构漂移时提取器应 ExtractError 而不是瞎猜。
整页（1.2MB）不入库，见 pricing_sources.py 头注释的实测记录。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from pricing_sources import (  # noqa: E402
    ExtractError,
    extract_aliyun,
    extract_all,
    extract_deepseek,
)

# ── deepseek：Docusaurus 静态表（2026-09-10 抓取的结构切片）──
_DEEPSEEK = """
<tr><td colspan="3" style="text-align:center">MODEL</td><td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro<sup>(2)</sup></td></tr>
<tr><td rowspan="6">PRICING<sup>(3)</sup></td><td rowspan="2">1M INPUT TOKENS<br>(CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>
<tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>
<tr><td rowspan="2">1M INPUT TOKENS<br>(CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.66</td></tr>
<tr><td>PEAK</td><td>$0.3</td><td>$1.32</td></tr>
<tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>
<tr><td>PEAK</td><td>$1.2</td><td>$3.96</td></tr>
"""

# ── aliyun：SSR 北京段切片（含阶梯 rowspan + 限时折扣 + embedding 单价）──
_ALIYUN = """
<section id="华北2-北京" class="section"><h4 id="华北2-北京-h4">华北2（北京）</h4><table><tbody>
<tr><td><p>qwen3.8-max</p><blockquote><p><a href="/x">Batch调用</a>半价</p></blockquote></td><td><p>非思考和思考模式</p></td><td><p>0&lt;Token≤1M</p></td><td><p>12<span class="help-letter-space"></span>元</p></td><td><p>36<span class="help-letter-space"></span>元</p></td><td><p>100<span class="help-letter-space"></span>万<span class="help-letter-space"></span>Token</p></td></tr>
<tr><td rowspan="2"><p>qwen3-max</p></td><td rowspan="2"><p>非思考和思考模式</p></td><td><p>0&lt;Token≤32K</p></td><td><p>2.5<span class="help-letter-space"></span>元</p></td><td><p>10<span class="help-letter-space"></span>元</p></td><td rowspan="2"><p>100万Token</p></td></tr>
<tr><td><p>32K&lt;Token≤128K</p></td><td><p>4<span class="help-letter-space"></span>元</p></td><td><p>16<span class="help-letter-space"></span>元</p></td></tr>
<tr><td><p>qwen3.7-plus</p></td><td><p>非思考和思考模式</p></td><td><p>0&lt;Token≤256K</p></td><td><p>原价<span class="help-letter-space"></span>2<span class="help-letter-space"></span>元<strong>（限时<span class="help-letter-space"></span>8<span class="help-letter-space"></span>折）</strong></p></td><td><p>原价<span class="help-letter-space"></span>8<span class="help-letter-space"></span>元<strong>（限时<span class="help-letter-space"></span>8<span class="help-letter-space"></span>折）</strong></p></td><td><p>100万Token</p></td></tr>
<tr><td><p>qwen-max</p></td><td><p>仅非思考模式</p></td><td><p>无阶梯计价</p></td><td><p>2.4<span class="help-letter-space"></span>元</p></td><td><p>9.6<span class="help-letter-space"></span>元</p></td><td><p>100万Token</p></td></tr>
</tbody></table></section>
<section id="新加坡" class="section"><table><tbody>
<tr><td><p>qwen3.8-max</p></td><td><p>全球</p></td><td><p>0&lt;Token≤1M</p></td><td><p>14.988<span class="help-letter-space"></span>元</p></td><td><p>44.965<span class="help-letter-space"></span>元</p></td></tr>
</tbody></table></section>
<section id="华北2-北京-2" class="section"><table><tbody>
<tr><td><p>text-embedding-v3</p></td><td><p>0.5<span class="help-letter-space"></span>元</p></td><td><p>50万Token</p></td></tr>
</tbody></table></section>
"""


def test_deepseek_extracts_usd_time_of_day_ranges():
    r = extract_deepseek(_DEEPSEEK)
    assert set(r) == {"deepseek-flash", "deepseek-v4-pro"}
    p = r["deepseek-v4-pro"]
    assert p["currency"] == "USD"
    assert p["input"] == [0.66, 1.32]
    assert p["output"] == [1.98, 3.96]
    assert p["cache_hit"] == [0.022, 0.044]
    # 区间价 → 不能一键采纳（verdict 层面由 main._pricing_verdict 判 info）


def test_deepseek_structure_drift_raises():
    with pytest.raises(ExtractError):
        extract_deepseek("<html>page redesigned</html>")


def test_aliyun_beijing_prices_and_tiers():
    r = extract_aliyun(_ALIYUN)
    assert r["qwen3.8-max"]["input"] == 12
    assert r["qwen3.8-max"]["output"] == 36
    # 阶梯：首区间代表价 + 后续区间进 tiers
    assert r["qwen3-max"]["input"] == 2.5
    assert r["qwen3-max"]["output"] == 10
    assert "Token≤128K" in r["qwen3-max"]["tiers"]
    # 无阶梯（更多模型表）：tier 标「无阶梯」
    assert r["qwen-max"]["input"] == 2.4
    assert r["qwen-max"]["tier"] == "无阶梯"
    # 新加坡段不进结果（只有华北2-北京* 段）
    assert r["qwen3.8-max"]["output"] == 36


def test_aliyun_promo_records_original_price():
    r = extract_aliyun(_ALIYUN)
    p = r["qwen3.7-plus"]
    assert p["input"] == 2  # 记原价，不按折扣折算
    assert p["output"] == 8
    assert "8折" in p["promo"]


def test_aliyun_embedding_single_price():
    r = extract_aliyun(_ALIYUN)
    assert r["text-embedding-v3"]["input"] == 0.5
    assert r["text-embedding-v3"]["output"] == 0


def test_aliyun_no_section_raises():
    with pytest.raises(ExtractError):
        extract_aliyun("<html>nothing here</html>")


def test_extract_all_marks_unavailable_and_errors():
    out = extract_all({})
    assert out["zhipu"]["status"] == "unavailable"
    assert "JS" in out["zhipu"]["reason"] or "渲染" in out["zhipu"]["reason"]
    assert out["kimi"]["status"] == "unavailable"
    # 可抓源没拉到页面 → error（fetch failed），不是瞎猜
    assert out["deepseek"]["status"] == "error"
    assert out["aliyun"]["status"] == "error"
    # 正常路径
    ok = extract_all({"deepseek": _DEEPSEEK, "aliyun": _ALIYUN})
    assert ok["deepseek"]["status"] == "ok"
    assert len(ok["aliyun"]["models"]) == 5

"""内置供应商模板测试（2026-09-10）。

关键不变量：模板静态、永不落 config、不含任何 key；base_urls 来自
实测（四家双协议均 200）；suggested_models 必须都在 BUILTIN_PRICING
和 BUILTIN_MODEL_META 里（添加后零配置可记账）。
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from builtin_meta import BUILTIN_MODEL_META  # noqa: E402
from builtin_pricing import BUILTIN_PRICING  # noqa: E402
from builtin_providers import (  # noqa: E402
    BUILTIN_PROVIDER_TEMPLATES,
    provider_templates_payload,
)

EXPECTED = {"zhipu", "deepseek", "kimi", "aliyun"}


def test_four_common_providers_present():
    assert set(BUILTIN_PROVIDER_TEMPLATES) == EXPECTED


def test_templates_never_contain_secrets():
    # 字段级检查：不存在任何密钥类字段；URL 查询串不带凭据值
    # （子串扫描会误伤 key_url 里的 /usercenter/apikeys 路径名）
    def _walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                assert not any(
                    s in k.lower() for s in ("api_key", "secret", "token", "password")
                ), f"template field leaks: {path}/{k}"
                _walk(v, f"{path}/{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                _walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and o.startswith("https://"):
            assert "sk-" not in o, o

    _walk(BUILTIN_PROVIDER_TEMPLATES)


def test_base_urls_are_https_and_parseable():
    for t in BUILTIN_PROVIDER_TEMPLATES.values():
        for proto, url in (t["base_urls"] or {}).items():
            u = urlparse(url)
            assert u.scheme == "https", (t["brand"], proto, url)
            assert u.netloc, (t["brand"], proto, url)


def test_suggested_models_have_pricing_and_meta():
    for t in BUILTIN_PROVIDER_TEMPLATES.values():
        for m in t["suggested_models"]:
            assert m["model"] in BUILTIN_PRICING, (t["brand"], m["model"])
            assert m["model"] in BUILTIN_MODEL_META, (t["brand"], m["model"])


def test_brand_matches_ui_dropdown_and_default_protocol_valid():
    # brand 必须是 UI pf-type option 值之一（aliyun/kimi/zhipu/deepseek
    # 均已在表单注册），default_protocol 必须是 base_urls 里的键
    for t in BUILTIN_PROVIDER_TEMPLATES.values():
        assert t["default_protocol"] in t["base_urls"]
        assert t["default_name"], t["brand"]


def test_payload_is_static_list():
    p = provider_templates_payload()
    assert set(p) == {"templates"}
    assert len(p["templates"]) == 4
    assert p is not BUILTIN_PROVIDER_TEMPLATES  # 消费方拿不到可变引用的目录本体


def test_endpoint_served(tmp_path, monkeypatch):
    """GET /api/providers/templates 返回模板 payload（FastAPI TestClient）。"""
    import fastapi.testclient

    import main

    client = fastapi.testclient.TestClient(main.app)
    r = client.get("/api/providers/templates")
    assert r.status_code == 200
    body = r.json()
    assert len(body["templates"]) == 4
    assert {t["brand"] for t in body["templates"]} == EXPECTED

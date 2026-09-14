"""Portable config export/import allowlist regression tests.

2026-09-07: model_meta joined the portable allowlist (providers, models).
Rationale: meta is model-intrinsic (context/max_output/vision) — a restore
without it degrades every limit to the name heuristic and strips vision
from kimi-k3 (name lacks -vl), which pi then blocks image input on.
Import merges model_meta PER MODEL so a backup older than the local config
cannot delete locally-known metas.

2026-09-08: export gained a __meta__ envelope (schema_version/exported_at/
source/machine/counts/warnings), decrypt-failure now exports the provider
key-less with a warning instead of leaking ciphertext labeled as plaintext
(which double-encrypted on import), import gained dry_run diff preview,
and /api/config/clone pulls the portable payload from a sibling instance.

2026-09-09: pricing joined the portable allowlist. Rationale: prices are
model-intrinsic (like model_meta); a fresh install's default config has NO
pricing at all, so excluding it left restored instances displaying 0/— for
every cost. plan_monthly travels with it — the cloned provider API keys
ride the same subscriptions. Import merges pricing PER MODEL (file wins).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import main  # noqa: E402


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _req(payload):
    from typing import Any, cast

    return cast(Any, _Req(payload))


# ── export ──────────────────────────────────────────────────────────


def test_export_includes_model_meta(monkeypatch):
    cfg = {
        "providers": [{"name": "p1", "api_key": "sk-x", "encrypted": True}],
        "models": {"m1": ["p1"]},
        "model_meta": {"m1": {"context": 256000, "vision": True}},
        "api_keys": [{"name": "cy", "key": "local-only"}],
        "port": 20100,
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "decrypt_value", lambda c: "sk-x")
    export = asyncio.run(main.api_export_config())
    assert export["model_meta"] == {"m1": {"context": 256000, "vision": True}}
    assert export["models"] == {"m1": ["p1"]}
    # local-only fields never leave the machine
    assert "api_keys" not in export and "port" not in export
    # encrypted provider keys are exported decrypted
    assert export["providers"][0]["api_key"] == "sk-x"
    assert export["providers"][0]["encrypted"] is False


def test_export_meta_envelope(monkeypatch):
    cfg = {
        "providers": [{"name": "p1", "api_key": None, "encrypted": False}],
        "models": {"m1": ["p1"]},
        "model_meta": {"m1": {}},
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "_machine_label", lambda: "TESTHOST")
    export = asyncio.run(main.api_export_config())
    meta = export["__meta__"]
    assert meta["format"] == "cyrouter-config"
    assert meta["schema_version"] == 2
    assert meta["machine"] == "TESTHOST"
    assert meta["source"].startswith("cyRouter ")
    assert "exported_at" in meta and "T" in meta["exported_at"]
    assert meta["counts"] == {
        "providers": 1,
        "models": 1,
        "model_meta": 1,
        "pricing": 0,
    }
    assert meta["warnings"] == []


def test_export_decrypt_failure_exports_keyless(monkeypatch):
    """2026-09-08 fix: undecryptable ciphertext must NOT ship as plaintext.

    The old suppress() path kept the ciphertext with encrypted=False, which
    the importer then re-encrypted — a double-encrypted, unrecoverable key.
    """
    cfg = {
        "providers": [
            {"name": "p1", "api_key": "gAAAA-ciphertext", "encrypted": True},
            {"name": "p2", "api_key": None, "encrypted": False},
        ],
        "models": {},
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)

    def _boom(c):
        raise ValueError("nope")

    monkeypatch.setattr(main, "decrypt_value", _boom)
    export = asyncio.run(main.api_export_config())
    p1 = export["providers"][0]
    assert p1["api_key"] is None
    assert p1["encrypted"] is False
    assert any("p1" in w for w in export["__meta__"]["warnings"])


# ── import ──────────────────────────────────────────────────────────


def test_import_merges_model_meta_per_model(monkeypatch):
    existing = {
        "providers": [],
        "models": {"m1": ["p1"]},
        "model_meta": {
            "m1": {"context": 111, "vision": True},  # file wins
            "m2": {"context": 222},  # local-only survivor
        },
        "port": 20100,
        "api_keys": [{"name": "cy", "key": "k"}],
    }
    incoming = {
        "providers": [{"name": "p1", "api_key": "sk-y"}],
        "models": {"m1": ["p1"], "m9": ["p1"]},
        "model_meta": {"m1": {"context": 999}},
    }
    saved = {}
    monkeypatch.setattr(main, "load_config", lambda: existing)
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    ok = asyncio.run(main.api_import_config(_req(incoming)))
    assert ok["ok"] is True
    # per-model merge: file's m1 wins, local m2 survives
    assert saved["model_meta"] == {
        "m1": {"context": 999},
        "m2": {"context": 222},
    }
    # portable keys come from the file wholesale
    assert saved["models"] == {"m1": ["p1"], "m9": ["p1"]}
    # local settings stay local
    assert saved["port"] == 20100
    assert saved["api_keys"] == [{"name": "cy", "key": "k"}]
    # plain provider key from the file got encrypted
    assert saved["providers"][0]["encrypted"] is True


def test_import_without_model_meta_keeps_existing(monkeypatch):
    """Old backups (pre-2026-09-07) have no model_meta — importing them must
    not wipe the local meta map."""
    existing = {
        "providers": [],
        "models": {},
        "model_meta": {"kimi-k3": {"context": 240000, "vision": True}},
        "port": 20100,
    }
    incoming = {"providers": [], "models": {"m1": ["p1"]}}
    saved = {}
    monkeypatch.setattr(main, "load_config", lambda: existing)
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    asyncio.run(main.api_import_config(_req(incoming)))
    assert saved["model_meta"] == existing["model_meta"]


def test_import_rejects_malformed(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.api_import_config(_req({"models": {}})))
    assert ei.value.status_code == 400


def test_import_ignores_meta_envelope(monkeypatch):
    """__meta__ is echoed for staleness display but never written to disk."""
    existing = {"providers": [], "models": {}, "port": 20100}
    incoming = {
        "__meta__": {
            "format": "cyrouter-config",
            "schema_version": 2,
            "exported_at": "2026-09-08T10:00:00+08:00",
        },
        "providers": [],
        "models": {"m1": ["p1"]},
    }
    saved = {}
    monkeypatch.setattr(main, "load_config", lambda: existing)
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    ok = asyncio.run(main.api_import_config(_req(incoming)))
    assert ok["ok"] is True
    assert "__meta__" not in saved


# ── dry-run diff ────────────────────────────────────────────────────


def test_import_dry_run_previews_and_skips_write(monkeypatch):
    existing = {
        "providers": [
            {
                "name": "keep",
                "base_url": "http://x",
                "api_key": "cipher",
                "encrypted": True,
            },
            {"name": "local-only", "base_url": "http://y"},
        ],
        "models": {"m-old": ["local-only"], "m-same": ["keep"]},
        "model_meta": {"m-old": {"context": 1}},
    }
    incoming = {
        "__meta__": {"schema_version": 2, "exported_at": "2026-09-08T10:00:00+08:00"},
        "providers": [
            {"name": "keep", "base_url": "http://x2"},  # url changed
            {"name": "fresh", "base_url": "http://z"},
        ],
        "models": {"m-new": ["fresh"], "m-same": ["keep"]},
        "model_meta": {"m-old": {"context": 9}, "m-brand-new": {"vision": True}},
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    saved = {}
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    out = asyncio.run(main.api_import_config(_req(incoming), dry_run=True))
    assert out["dry_run"] is True
    assert not saved, "dry run must not save"
    d = out["diff"]
    assert d["providers"]["added"] == ["fresh"]
    assert d["providers"]["changed"] == ["keep"]  # url differs; key excluded
    assert d["providers"]["removed"] == ["local-only"]
    assert d["models"]["added"] == ["m-new"]
    assert d["models"]["changed"] == []
    assert d["models"]["removed"] == ["m-old"]
    assert d["model_meta"]["added"] == ["m-brand-new"]
    assert d["model_meta"]["changed"] == ["m-old"]
    assert out["backup_meta"]["schema_version"] == 2


def test_import_diff_ignores_key_fields(monkeypatch):
    """Same provider, only the key differs (local cipher vs file plaintext):
    not a change — comparison excludes api_key/encrypted."""
    existing = {
        "providers": [
            {
                "name": "p",
                "base_url": "http://x",
                "api_key": "cipher",
                "encrypted": True,
            }
        ],
        "models": {},
    }
    incoming = {
        "providers": [
            {
                "name": "p",
                "base_url": "http://x",
                "api_key": "sk-plain",
                "encrypted": False,
            }
        ],
        "models": {},
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    out = asyncio.run(main.api_import_config(_req(incoming), dry_run=True))
    assert out["diff"]["providers"]["changed"] == []


def test_real_import_returns_diff(monkeypatch):
    existing = {"providers": [], "models": {}, "port": 20100}
    incoming = {"providers": [], "models": {"m1": ["p1"]}}
    monkeypatch.setattr(main, "load_config", lambda: existing)
    monkeypatch.setattr(main, "save_config", lambda cfg: None)
    ok = asyncio.run(main.api_import_config(_req(incoming)))
    assert ok["ok"] is True
    assert ok["diff"]["models"]["added"] == ["m1"]


# ── clone ───────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


def test_clone_dry_run(monkeypatch):
    payload = {
        "__meta__": {"schema_version": 2, "machine": "OTHERHOST"},
        "providers": [{"name": "p1", "api_key": "sk-y"}],
        "models": {"m1": ["p1"]},
    }
    client = _FakeClient(_FakeResp(200, payload))
    monkeypatch.setattr(main, "_get_http_client", lambda: client)
    existing = {"providers": [], "models": {}, "port": 20108}
    monkeypatch.setattr(main, "load_config", lambda: existing)
    saved = {}
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    out = asyncio.run(
        main.api_clone_config(
            _req(
                {
                    "source_url": "http://localhost:20100/",
                    "source_key": "sk-dev",
                    "dry_run": True,
                }
            )
        )
    )
    assert out["dry_run"] is True
    assert not saved
    assert out["diff"]["providers"]["added"] == ["p1"]
    assert out["backup_meta"]["machine"] == "OTHERHOST"
    # trailing slash stripped; key sent via both auth headers
    url, kw = client.calls[0]
    assert url == "http://localhost:20100/api/config/export"
    assert kw["headers"]["x-api-key"] == "sk-dev"


def test_clone_applies_import(monkeypatch):
    payload = {
        "providers": [{"name": "p1", "api_key": "sk-y"}],
        "models": {"m1": ["p1"]},
    }
    client = _FakeClient(_FakeResp(200, payload))
    monkeypatch.setattr(main, "_get_http_client", lambda: client)
    existing = {
        "providers": [],
        "models": {},
        "port": 20108,
        "api_keys": [{"name": "local", "key": "k"}],
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    saved = {}
    monkeypatch.setattr(main, "save_config", lambda cfg: saved.update(cfg))
    out = asyncio.run(
        main.api_clone_config(
            _req(
                {
                    "source_url": "http://localhost:20100",
                }
            )
        )
    )
    assert out["ok"] is True
    assert saved["port"] == 20108  # local port untouched by clone
    assert saved["api_keys"] == existing["api_keys"]
    assert saved["models"] == {"m1": ["p1"]}
    assert saved["providers"][0]["encrypted"] is True


def test_clone_auth_error(monkeypatch):
    client = _FakeClient(_FakeResp(401, {"error": "no"}))
    monkeypatch.setattr(main, "_get_http_client", lambda: client)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.api_clone_config(_req({"source_url": "http://x:1"})))
    assert ei.value.status_code == 401


def test_clone_bad_url_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.api_clone_config(_req({"source_url": "ftp://x"})))
    assert ei.value.status_code == 400


def test_clone_unreachable(monkeypatch):
    def _raise():
        raise httpx.ConnectError("refused")

    class _BoomClient:
        async def get(self, url, **kw):
            _raise()

    monkeypatch.setattr(main, "_get_http_client", lambda: _BoomClient())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(main.api_clone_config(_req({"source_url": "http://127.0.0.1:9"})))
    assert ei.value.status_code == 502


# ── pricing portability (2026-09-09) ────────────────────────────────


def test_export_includes_pricing(monkeypatch):
    """prices are model-intrinsic and must travel with providers/models."""
    cfg = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {"m1": {"input": 1.2, "output": 6, "plan_monthly": 99}},
        "token_plan_quota": {"limit_credits": 100},  # machine-local, stays
    }
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    export = asyncio.run(main.api_export_config())
    assert export["pricing"] == {"m1": {"input": 1.2, "output": 6, "plan_monthly": 99}}
    assert "token_plan_quota" not in export
    assert export["__meta__"]["counts"]["pricing"] == 1


def test_import_merges_pricing_per_model(monkeypatch):
    """File pricing wins per model; local-only entries survive a stale backup."""
    existing = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {
            "m1": {"input": 1.0, "output": 5.0},
            "m2-local": {"input": 0.5, "output": 1.0},
        },
        "port": 20100,
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    saved = {}
    monkeypatch.setattr(main, "save_config", lambda c: saved.update(c))
    incoming = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {"m1": {"input": 2.0, "output": 10.0}},  # m1 repriced
    }
    asyncio.run(main.api_import_config(_req(incoming)))
    assert saved["pricing"]["m1"] == {"input": 2.0, "output": 10.0}
    assert saved["pricing"]["m2-local"] == {"input": 0.5, "output": 1.0}


def test_import_without_pricing_preserves_local(monkeypatch):
    """Old backups (no pricing key) never wipe local pricing."""
    existing = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {"m1": {"input": 1.0}},
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    saved = {}
    monkeypatch.setattr(main, "save_config", lambda c: saved.update(c))
    asyncio.run(main.api_import_config(_req({"providers": [], "models": {}})))
    assert saved["pricing"] == {"m1": {"input": 1.0}}


def test_dry_run_diff_includes_pricing(monkeypatch):
    existing = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {"m1": {"input": 1.0}, "m2": {"input": 0.5}},
    }
    monkeypatch.setattr(main, "load_config", lambda: existing)
    monkeypatch.setattr(main, "save_config", lambda c: pytest.fail("dry_run wrote"))
    incoming = {
        "providers": [{"name": "p1"}],
        "models": {"m1": ["p1"]},
        "pricing": {"m1": {"input": 9.9}, "m3-new": {"input": 7.0}},
    }
    out = asyncio.run(main.api_import_config(_req(incoming), dry_run=True))
    d = out["diff"]["pricing"]
    assert d["added"] == ["m3-new"]
    assert d["changed"] == ["m1"]
    assert "removed" not in d  # merge semantics, never removes

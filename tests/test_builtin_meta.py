"""BUILTIN_MODEL_META resolution chain tests (2026-09-07).

Layer order: model_overrides.limit > user model_meta > BUILTIN_MODEL_META
> pricing.max_context > family heuristic. A user model_meta entry replaces
the builtin entry WHOLESALE; builtin serves cold-start installs (no user
meta at all) so capabilities like kimi-k3 vision never regress to the
name heuristic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
from builtin_meta import BUILTIN_MODEL_META  # noqa: E402


def _cfg(**kw):
    base = {"pricing": {}, "model_overrides": {}, "model_meta": {}}
    base.update(kw)
    return base


def test_builtin_catalog_shape():
    """Every catalog entry carries the essential numeric fields."""
    assert len(BUILTIN_MODEL_META) >= 10
    for m, meta in BUILTIN_MODEL_META.items():
        assert isinstance(meta.get("context"), int), m
        assert isinstance(meta.get("max_output"), int), m
        assert isinstance(meta.get("vision"), bool), m


def test_builtin_serves_fresh_install():
    """Empty user config → builtin values win over heuristics, tagged builtin."""
    r = main._resolve_model_limit("kimi-k3", _cfg())
    assert r["context"] == 256000 and r["context_source"] == "builtin"
    assert r["output"] == 32768 and r["output_source"] == "builtin"
    # vision chain also consults builtin — kimi-k3 has no -vl in its name
    assert main._model_supports_vision("kimi-k3", _cfg()) is True


def test_user_meta_wins_wholesale():
    """A user entry with the same model name replaces the builtin entry
    entirely — builtin values do NOT leak through as field-level defaults."""
    cfg = _cfg(
        model_meta={"kimi-k3": {"context": 999999}},  # no max_output field
    )
    r = main._resolve_model_limit("kimi-k3", cfg)
    assert r["context"] == 999999 and r["context_source"] == "model_meta"
    # builtin must NOT fill the missing output field — the source (not the
    # value) is the invariant: the kimi family heuristic coincidentally
    # also yields 32768.
    assert r["output_source"] == "heuristic"
    assert r["layers"]["builtin"] == {"context": None, "output": None}


def test_override_wins_over_all():
    cfg = _cfg(model_meta={"kimi-k3": {"context": 999999, "max_output": 111}})
    r = main._resolve_model_limit(
        "kimi-k3",
        _cfg(
            model_overrides={"kimi-k3": {"limit": {"output": 7}}},
            model_meta=cfg["model_meta"],
        ),
    )
    assert r["output"] == 7 and r["output_source"] == "override"
    assert r["context"] == 999999  # user meta still beats builtin


def test_unknown_model_falls_to_heuristic():
    r = main._resolve_model_limit("totally-unknown-model", _cfg())
    assert r["context_source"] == "heuristic"
    assert r["layers"]["builtin"] == {"context": None, "output": None}


def test_builtin_vision_respected_before_name_heuristic():
    # glm-5.3-flash: vision via builtin, name has no vl marker
    assert main._model_supports_vision("glm-5.3-flash", _cfg()) is True
    # user meta can turn it OFF (their model, their call)
    cfg = _cfg(model_meta={"glm-5.3-flash": {"vision": False}})
    assert main._model_supports_vision("glm-5.3-flash", cfg) is False


def test_layers_report_includes_builtin():
    r = main._resolve_model_limit("kimi-k3", _cfg())
    assert r["layers"]["builtin"] == {"context": 256000, "output": 32768}

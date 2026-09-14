"""Unit tests for secret redaction / reversible masking (redact.py + main.py)."""

import json
import sys
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from redact import (  # noqa: E402
    ContentRestorer,
    StreamingRestorer,
    build_patterns,
    redact_body,
    redact_enabled,
    restore_bytes,
    restore_obj,
    restore_text,
)

CFG = {}  # default aggressive mode, no custom patterns


# ── Known-prefix detection ────────────────────────────────────────────


def test_openai_classic_key_detected():
    tok = "sk-" + "a1B2c3" * 6  # sk- + 36 alnum
    body, m = redact_body(
        {"messages": [{"role": "user", "content": "key=" + tok}]}, CFG
    )
    assert tok in m
    assert tok not in body["messages"][0]["content"]


def test_anthropic_key_detected():
    tok = "sk-ant-" + "x" * 40
    body, m = redact_body({"t": tok}, CFG)
    assert tok in m and tok not in body["t"]


def test_google_ai_key_detected():
    tok = "AIza" + "a" * 35
    body, m = redact_body({"t": tok}, CFG)
    assert tok in m


def test_aws_key_detected():
    tok = "AKIA" + "ABCDEFGHJKLMNPQRSTUVWXYZ"[:16]
    body, m = redact_body({"t": tok}, CFG)
    assert tok in m


def test_aliyun_accesskey_id_detected():
    # Alibaba Cloud AccessKey ID: LTAI prefix + 16-24 alnum (~20-28 chars)
    tok = "LTAI" + "ab12CD" * 4  # LTAI + 24 alnum
    body, m = redact_body({"t": "key=" + tok}, CFG)
    assert tok in m


def test_aliyun_accesskey_secret_detected():
    # Alibaba Cloud AccessKey Secret: 30-char alnum, no prefix.
    # Exercises the 28-char entropy floor (lowered from 32 specifically for this).
    import secrets
    import string

    tok = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(30)
    )
    body, m = redact_body({"t": "secret " + tok}, CFG)
    assert tok in m


def test_github_pat_detected():
    tok = "ghp_" + "a" * 36
    body, m = redact_body({"t": tok}, CFG)
    assert tok in m


# ── High-entropy generic detection ────────────────────────────────────


def test_high_entropy_token_detected():
    import secrets
    import string

    alpha = string.ascii_letters + string.digits
    tok = "".join(secrets.choice(alpha) for _ in range(48))
    body, m = redact_body({"t": "tok " + tok}, CFG)
    assert tok in m


def test_md5_hash_not_flagged():
    # 32-char pure hex = MD5, almost never a secret
    body, m = redact_body({"t": "d41d8cd98f00b204e9800998ecf8427e"}, CFG)
    assert len(m) == 0


def test_sha1_hash_not_flagged():
    body, m = redact_body({"t": "da39a3ee5e6b4b0d3255bfef95601890afd80709"}, CFG)
    assert len(m) == 0


def test_pure_digits_not_flagged():
    body, m = redact_body({"t": "1" * 40}, CFG)
    assert len(m) == 0


def test_low_entropy_run_not_flagged():
    body, m = redact_body({"t": "abababababababababababababababab"}, CFG)
    assert len(m) == 0


# ── Structural-identifier corpus (2026-09-07 incident regression) ─────
# An agent debugging session had its tool-output UUIDs silently swapped for
# plausible-looking surrogates: the old _HIGH_ENTROPY_RE included '-' in its
# class, so a dashed UUID was one 36-char run that failed the (undashed-only)
# hex exemption and was masked unless its letter subsequence happened to look
# English — a per-token coin flip that made the corruption look random.
# These tests pin the fix from both sides: identifiers must pass through
# untouched, real credentials must still be masked.


def _assert_untouched(text: str):
    body, m = redact_body({"messages": [{"role": "user", "content": text}]}, CFG)
    assert len(m) == 0
    assert body["messages"][0]["content"] == text


def test_dashed_uuid4_not_flagged():
    for _ in range(20):
        _assert_untouched(f"episode {_uuid.uuid4()} committed")


def test_uppercase_dashed_uuid_not_flagged():
    # Built from short pieces: long hex literals don't survive the gateway's
    # OWN redaction layer in transit (dogfooding hazard — the very bug these
    # tests regress). 8/4/4/4/12 segments are each far below the 28-char run.
    u = "-".join(["BE951E48", "65C8", "4972", "A5CC", "2845EB1A7D88"])
    _assert_untouched(u.upper())


def test_undashed_hex36_uuid_not_flagged():
    # 36-hex undashed UUID — not covered by the old {32,40,64} hex exemption
    # either.  Assembled at runtime from short pieces (see note above).
    hex36 = "".join(["f47ac1", "0b58cc", "03728567", "0e02b0", "000000", "8fff"])
    assert len(hex36) == 36
    assert all(c in "0123456789abcdef" for c in hex36)
    _assert_untouched(hex36)


def test_sha256_in_prose_not_flagged():
    _assert_untouched(
        "digest sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ok"
    )


def test_hex16_short_id_not_flagged():
    _assert_untouched("git short sha a1b2c3d4e5f60718")


def test_iso8601_timestamp_not_flagged():
    _assert_untouched("created_at=2026-09-07T13:29:54.123456+08:00")


def test_ulid_not_flagged():
    _assert_untouched("01ARZ3NDEKTSV4RRFFQ69G5FAV")


def test_hyphenated_composite_id_not_flagged():
    # Composite ids with dashes (docker/bazel-style) — the whole reason '-'
    # left the generic class.
    _assert_untouched("io_bazel_rules_python-0.31.0-py3-none-any")


def test_dashed_uuid_round_trip_identity():
    u = str(_uuid.uuid4())
    body, m = redact_body({"t": f"ref {u}"}, CFG)
    assert len(m) == 0 and body["t"] == f"ref {u}"


def test_tool_call_id_not_flagged():
    # OpenAI tool-call ids (call_ + base62) were masked as entropy-class
    # secrets — the most common structured id in agent traffic.
    call_id = "call_" + "b2xYq9LmT4wZ7aD3eF6gH5j8k"  # 29 chars total
    assert len(call_id) >= 28
    _assert_untouched(f"tool call {call_id} done")


def test_chatcmpl_completion_id_not_flagged():
    cid = "chatcmpl-" + "Bq7xYm2Ld9Kf4Tz8aWc3Vn6Rj"  # 29 chars after prefix
    assert len(cid) >= 28
    _assert_untouched(cid)


def test_real_credentials_still_flagged_after_widening():
    # The benign widening must not bleed into real credential shapes.
    tok1 = "sk-ant-" + "Ab3xK9mQ" * 5
    tok2 = "ghp_" + "Ab3xK9mQzLp7" * 3
    body, m = redact_body({"t": f"{tok1} {tok2}"}, CFG)
    assert tok1 in m and tok2 in m
    assert tok1 not in body["t"] and tok2 not in body["t"]
    # Round-trip restore still works for what IS masked.
    assert restore_text(body["t"], m) == f"{tok1} {tok2}"


# ── Idempotency & placeholder shape ───────────────────────────────────


def test_placeholder_looks_like_key():
    orig = "sk-" + "z" * 30
    body, m = redact_body({"t": orig}, CFG)
    ph = next(iter(m.values()))
    # Middle-mask surrogate: same length, provider prefix + sample kept,
    # tail fingerprint kept, only the high-entropy middle is randomised.
    assert len(ph) == len(orig)
    assert ph.startswith("sk-")
    assert ph[:5] == orig[:5]  # head: structural prefix + 2-char sample
    assert ph[-4:] == orig[-4:]  # tail: 4-char fingerprint
    assert ph != orig  # middle masked
    assert all(c.isalnum() for c in ph[5:-4])  # pure [A-Za-z0-9] middle


def test_idempotent_second_pass():
    orig = "sk-" + "z" * 30
    body1, m1 = redact_body({"t": orig}, CFG)
    body2, m2 = redact_body(body1, CFG)
    s1, s2 = body1["t"], body2["t"]
    # Marker-free random surrogates look like real keys, so a second pass
    # re-masks to a fresh surrogate of identical shape. In production a body is
    # masked exactly once per request and surrogates are restored before they
    # ever return to a client, so this path is unreachable — we assert shape
    # preservation + that each pass round-trips its own mapping.
    assert len(s2) == len(s1) == len(orig)
    assert s2[:5] == s1[:5] == orig[:5]  # head preserved across passes
    assert s2[-4:] == s1[-4:] == orig[-4:]  # tail preserved across passes
    assert restore_text(s2, m2) == s1


def test_same_secret_one_placeholder():
    s = "sk-or-" + "q" * 30
    body, m = redact_body({"a": s, "b": [s, {"c": s}]}, CFG)
    assert len(m) == 1
    assert body["a"] != s and body["b"][0] != s


# ── Restore ───────────────────────────────────────────────────────────


def test_restore_text_exact():
    orig = "sk-" + "z" * 30
    body, m = redact_body({"t": "x " + orig + " y " + orig}, CFG)
    assert restore_text(body["t"], m) == "x " + orig + " y " + orig


def test_restore_bytes_exact():
    orig = "sk-" + "z" * 30
    body, m = redact_body({"t": "x " + orig}, CFG)
    assert restore_bytes(body["t"].encode(), m).decode() == "x " + orig


def test_restore_openai_response_shape():
    orig = "sk-" + "z" * 30
    body, m = redact_body({"t": orig}, CFG)
    ph = m[orig]
    # Simulated OpenAI non-streaming response echoing the placeholder
    result = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "you said " + ph},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    restored = restore_obj(result, m)
    assert restored["choices"][0]["message"]["content"] == "you said " + orig
    # usage (no strings) passes through untouched
    assert restored["usage"] == result["usage"]


def test_restore_anthropic_response_shape():
    orig = "hf_" + "a" * 30
    body, m = redact_body({"t": orig}, CFG)
    ph = m[orig]
    result = {
        "content": [{"type": "text", "text": "got " + ph}],
        "stop_reason": "end_turn",
    }
    restored = restore_obj(result, m)
    assert restored["content"][0]["text"] == "got " + orig


def test_restore_empty_mapping_is_noop():
    assert restore_text("abc", {}) == "abc"
    assert restore_obj({"a": [1, 2]}, {}) == {"a": [1, 2]}


# ── StreamingRestorer (cross-chunk placeholders) ─────────────────────


def test_streaming_restorer_split_across_chunks():
    import secrets
    import string

    orig = "sk-" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(30)
    )
    body, m = redact_body({"t": orig}, CFG)
    ph = m[orig]
    full = ("prefix " + ph + " suffix").encode()
    for split in range(len(full)):
        r = StreamingRestorer(m)
        out = r.feed(full[:split]) + r.feed(full[split:]) + r.flush()
        assert out == ("prefix " + orig + " suffix").encode(), (
            f"failed at split={split}"
        )


def test_streaming_restorer_no_mapping_passthrough():
    r = StreamingRestorer({})
    assert r.feed(b"anything") == b"anything"
    assert r.flush() == b""


# ── ContentRestorer (cross-delta content-level restoration) ──────────
# StreamingRestorer (byte-level) cannot reassemble a placeholder split across
# SSE/JSON deltas because the structural bytes between deltas interrupt byte
# continuity. ContentRestorer operates on the clean text extracted from those
# events, so it correctly restores surrogates split across any number of deltas.


def test_content_restorer_cross_delta():
    """A surrogate split across several content deltas is fully restored."""
    import secrets
    import string

    orig = "sk-" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(40)
    )
    _, m = redact_body({"t": orig}, CFG)
    sur = m[orig]
    # Split surrogate into 5 uneven chunks like a real tokenizer would.
    deltas = [sur[:8], sur[8:18], sur[18:28], sur[28:36], sur[36:]]
    r = ContentRestorer(m)
    out = "".join(r.feed(d) for d in deltas) + r.flush()
    assert orig in out
    assert sur not in out


def test_content_restorer_embedded_in_prose():
    """Surrogate surrounded by normal text is restored, prose passes through."""
    orig = "sk-" + "x" * 40
    _, m = redact_body({"t": orig}, CFG)
    sur = m[orig]
    prefix, suffix = "The token is ", " and that is it."
    # Stream: prefix whole, surrogate in 3 pieces, suffix whole.
    deltas = [prefix, sur[:10], sur[10:30], sur[30:], suffix]
    r = ContentRestorer(m)
    out = "".join(r.feed(d) for d in deltas) + r.flush()
    assert out == prefix + orig + suffix


def test_content_restorer_char_by_char():
    """Extreme case: surrogate streamed one character at a time."""
    orig = "sk-" + "q" * 40
    _, m = redact_body({"t": orig}, CFG)
    sur = m[orig]
    r = ContentRestorer(m)
    out = "".join(r.feed(c) for c in sur) + r.flush()
    assert orig in out
    assert sur not in out


def test_content_restorer_no_surrogate_passthrough():
    """Text with no surrogate fragment passes through unchanged."""
    r = ContentRestorer({"sk-real": "sk-fake"})
    out = r.feed("普通文本 hello 123, no secrets here") + r.flush()
    assert out == "普通文本 hello 123, no secrets here"


def test_content_restorer_empty_mapping():
    """Empty mapping is a no-op."""
    r = ContentRestorer({})
    assert r.feed("anything") == "anything"
    assert r.flush() == ""


def test_deterministic_surrogate_mapping():
    """Same secret always maps to a fixed surrogate within a day."""
    import redact as _r

    secret = "sk-" + "d" * 40
    m1 = redact_body({"t": secret}, CFG)[1]
    s1 = m1[secret]
    # Second call in a fresh per-request mapping must yield the SAME surrogate
    # (deterministic derivation from (daily seed, secret) — no randomness).
    m2 = redact_body({"t": secret}, CFG)[1]
    assert m2[secret] == s1

    # Determinism holds even with the seed cache reset (simulates a fresh
    # import / new process on the same day).
    _r._SEED_CACHE = None
    m3 = redact_body({"t": secret}, CFG)[1]
    assert m3[secret] == s1


# ── Config behaviour ──────────────────────────────────────────────────


def test_disabled_config():
    assert redact_enabled({"secret_redaction": {"enabled": False}}) is False


def test_default_on_when_missing():
    assert redact_enabled({}) is True
    assert redact_enabled({"secret_redaction": {}}) is True


def test_custom_patterns():
    cfg = {"custom_patterns": [r"MYFLAG_\w+"]}
    body, m = redact_body({"t": "MYFLAG_secret_x"}, cfg)
    assert "MYFLAG_secret_x" in m


def test_exclude_substrings_whitelist():
    cfg = {"exclude_substrings": ["sk-"]}
    body, m = redact_body({"t": "sk-" + "n" * 30}, cfg)
    assert len(m) == 0


def test_invalid_custom_pattern_skipped():
    pats = build_patterns(["(", "valid_[0-9]+"])
    assert len(pats) == 1  # the invalid one dropped, valid kept


# ── Caller body left pristine (cache-key safety) ─────────────────────


def test_caller_body_not_mutated():
    import copy

    orig_body = {"t": "sk-" + "p" * 30}
    snapshot = copy.deepcopy(orig_body)
    redact_body(orig_body, CFG)
    assert orig_body == snapshot


# ── main.py integration ──────────────────────────────────────────────


def test_main_redact_request_body_enabled(monkeypatch):
    import main

    monkeypatch.setattr(
        main, "load_config", lambda: {"secret_redaction": {"enabled": True}}
    )
    tok = "sk-" + "z" * 30
    body = {"messages": [{"role": "user", "content": "here " + tok}]}
    redacted, mapping = main._redact_request_body(body)
    assert tok in mapping
    assert tok not in redacted["messages"][0]["content"]
    assert body == {
        "messages": [{"role": "user", "content": "here " + tok}]
    }  # untouched


def test_main_redact_request_body_disabled(monkeypatch):
    import main

    monkeypatch.setattr(
        main, "load_config", lambda: {"secret_redaction": {"enabled": False}}
    )
    tok = "sk-" + "z" * 30
    body = {"messages": [{"content": tok}]}
    redacted, mapping = main._redact_request_body(body)
    assert mapping == {}
    assert redacted["messages"][0]["content"] == tok


# ── Streaming tool-call argument restoration ─────────────────────────
# A redacted secret echoed into a streamed tool_call argument must be
# restored just like content text — the arguments string is assembled
# incrementally across deltas, so a surrogate can be split across them.


def test_openai_tool_call_arg_surrogate_split_across_deltas():
    import main

    real = "sk-" + "a" * 30
    body = {"messages": [{"content": real}]}
    _, mapping = redact_body(body, CFG)
    sur = mapping[real]
    # Model echoes the surrogate inside a tool_call argument, split into 3
    # uneven fragments exactly as a tokenizer would chunk a JSON string.
    arg_full = json.dumps({"api_key": sur})
    frags = [arg_full[:9], arg_full[9:20], arg_full[20:]]
    restorers = {}
    get = lambda k: restorers.setdefault(k, ContentRestorer(mapping))
    reass = ""
    for f in frags:
        evt = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": f}}]
                    },
                }
            ]
        }
        line = ("data: " + json.dumps(evt) + "\n").encode()
        out = main._restore_sse_line(line, get)
        reass += json.loads(out[6:].rstrip())["choices"][0]["delta"]["tool_calls"][0][
            "function"
        ]["arguments"]
    assert json.loads(reass)["api_key"] == real  # restored, not surrogate


def test_anthropic_tool_use_input_surrogate_split_across_deltas():
    import main

    real = "sk-" + "b" * 30
    body = {"messages": [{"content": real}]}
    _, mapping = redact_body(body, CFG)
    sur = mapping[real]
    pj = json.dumps({"api_key": sur})
    mid = len(pj) // 2
    frags = [pj[:mid], pj[mid:]]
    restorers = {}
    get = lambda k: restorers.setdefault(k, ContentRestorer(mapping))
    reass = ""
    for frag in frags:
        evt = {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": frag},
        }
        line = ("data: " + json.dumps(evt) + "\n").encode()
        out = main._restore_sse_line(line, get)
        reass += json.loads(out[6:].rstrip())["delta"]["partial_json"]
    assert json.loads(reass)["api_key"] == real


def test_tail_content_chunk_emits_tool_call_shapes():
    import main

    # The flush path for a held-back tool-call fragment must re-emit it in the
    # same incremental argument shape, not as a content text delta.
    openai_chunk = main._tail_content_chunk("leftover", "openai:tc:0")
    assert b'"tool_calls"' in openai_chunk
    anthropic_chunk = main._tail_content_chunk("leftover", "anthropic:tc:2")
    assert b"input_json_delta" in anthropic_chunk
    assert b'"index": 2' in anthropic_chunk


# ── Benign long identifiers (generic-detector false positives) ───────
# Long PascalCase/camelCase code symbols must NOT be redacted — the model
# seeing watermark surrogates in place of identifiers once concluded that
# healthy files on disk were corrupted. Strings are built by concatenation
# so no literal >=28-char token appears in this file.


def test_pascal_case_identifier_not_flagged():
    ident = "USprin" + "gVector3" + "Animation" + "Component"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0
    assert body["t"] == ident


def test_signal_handler_identifier_not_flagged():
    ident = "On" + "Workspace" + "Pointer" + "Entered" + "Handler"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0
    assert body["t"] == ident


def test_concatenated_identifier_in_code_not_flagged():
    ident = (
        "Spring" + "Vector3" + "Animation" + "_On" + "Workspace" + "Pointer" + "Entered"
    )
    body, m = redact_body({"t": f"void Handle({ident}* ctx) {{ }}"}, CFG)
    assert len(m) == 0


def test_rule_id_identifier_not_flagged():
    ident = "unchec" + "ked" + "Python" + "CallArg" + "Count"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0


def test_vowel_free_secret_still_flagged():
    sec = "xY9kM2" + "uJLqt4" + "j89x8d" + "LCPDNV" + "gR1kB0" + "aL"
    body, m = redact_body({"t": sec}, CFG)
    assert len(m) == 1
    assert body["t"] != sec


def test_mixed_secret_still_flagged():
    sec = "mh7v2x" + "gJf40p" + "BSMmSk" + "XzVbcG" + "A3Rl8r" + "Eo1Q"
    body, m = redact_body({"t": sec}, CFG)
    assert len(m) == 1
    assert body["t"] != sec


# ── Markov english-likeness (bigram model, median scoring) ───────────
# The primary identifier/spacer: median char-bigram log-prob under the
# English+pinyin model. Covers consonant-heavy and pinyin identifiers that
# vowel-ratio heuristics miss; random secrets stay far below threshold.


def test_gtk_widget_path_identifier_not_flagged():
    ident = "Gtk" + "Widget" + "Path" + "Selection" + "Mode" + "Changed"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0
    assert body["t"] == ident


def test_qqml_engine_identifier_not_flagged():
    ident = "QQml" + "Application" + "Engine" + "Context" + "Property"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0


def test_pinyin_identifier_not_flagged():
    ident = "chushi" + "hua" + "jia" + "zai" + "shun" + "xu" + "biao" + "ge"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0


def test_pthread_style_identifier_not_flagged():
    ident = "pthread" + "mutex" + "attr" + "set" + "type" + "recursive"
    body, m = redact_body({"t": ident}, CFG)
    assert len(m) == 0


def test_mixed_secret_variant_still_flagged():
    sec = "Qw8Rt2" + "YzLp5" + "N7bV3c" + "X9mK4j" + "H6gF1d" + "S0aZ"
    body, m = redact_body({"t": sec}, CFG)
    assert len(m) == 1
    assert body["t"] != sec


def test_anthropic_image_bare_base64_not_redacted():
    """Anthropic wire format: image payload is a BARE base64 `data` field
    inside {type: base64, media_type, data} — no data: URL prefix, so the
    data-url exemption didn't cover it and the entropy scanner shattered
    the payload (2026-09-08 kimi "unsupported image format:
    application/octet-stream" incident). Must stay byte-identical."""
    import base64
    import struct
    import zlib

    def _png() -> str:
        def chunk(t: bytes, d: bytes) -> bytes:
            c = t + d
            return (
                struct.pack(">I", len(d))
                + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\xff\x00\x00")
        return base64.b64encode(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b"")
        ).decode()

    png_b64 = _png()  # len ~ 75, high entropy, starts with iVBORw
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png_b64,
                        },
                    },
                ],
            }
        ]
    }
    out, m = redact_body(body, CFG)
    assert len(m) == 0
    src = out["messages"][0]["content"][1]["source"]
    assert src["data"] == png_b64
    assert src["media_type"] == "image/png"


def test_anthropic_image_block_secret_elsewhere_still_redacted():
    """The structural skip must be surgical: a real key in the SAME message
    still gets redacted."""
    import base64

    png_b64 = base64.b64encode(b"\x89PNG" + b"\x00" * 40).decode()
    secret = "sk-ant-" + "Ab3xK9mQ" * 5  # runtime-built fake key, >28 chars
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"key: {secret}"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png_b64,
                        },
                    },
                ],
            }
        ]
    }
    out, m = redact_body(body, CFG)
    assert secret in m
    assert secret not in out["messages"][0]["content"][0]["text"]
    assert out["messages"][0]["content"][1]["source"]["data"] == png_b64

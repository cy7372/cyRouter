"""Spaceless-SSE regression (2026-09-08 incident).

kimi coding's Anthropic endpoint emits SSE data lines WITHOUT the optional
space after the colon (``data:{...}``). The WHATWG SSE spec allows exactly
that ("If value starts with a U+0020 SPACE character, remove it"), but
cyRouter's stream consumers all hard-coded ``b"data: "`` — so the
first-token gate buffered 11KB of thinking deltas as "no content", retried
3×, and surfaced empty_response 502 to every Anthropic-protocol streaming
request on that provider (zhipu emits the spaced form, which masked the
bug). These tests pin the tolerant parser from both directions.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
from redact import ContentRestorer, redact_body  # noqa: E402

# ── _sse_data_payload ────────────────────────────────────────────────


def test_payload_spaced():
    assert main._sse_data_payload(b'data: {"a":1}') == b'{"a":1}'


def test_payload_spaceless():
    assert main._sse_data_payload(b'data:{"a":1}') == b'{"a":1}'


def test_payload_non_data_lines():
    assert main._sse_data_payload(b"event: message_start") is None
    assert main._sse_data_payload(b": heartbeat") is None
    assert main._sse_data_payload(b"") is None
    assert main._sse_data_payload(b"id: 42") is None


# ── _sse_line_has_content — Anthropic shape (the incident) ───────────


def test_gate_spaceless_thinking_delta_is_content():
    line = b'data:{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"User"}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_gate_spaceless_text_delta_is_content():
    line = b'data:{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Blue"}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_gate_spaceless_message_start_is_not_content():
    # kimi's first two events — must NOT count as content (gate still waits
    # for a real delta), but must not crash either.
    line = b'data:{"type":"message_start","message":{"content":[]}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is False


def test_gate_spaced_still_works():
    line = b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


# ── _sse_line_has_content — OpenAI shape ─────────────────────────────


def test_gate_spaceless_openai_content():
    line = b'data:{"choices":[{"delta":{"content":"hi"}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is True


def test_gate_spaceless_openai_reasoning():
    line = b'data:{"choices":[{"delta":{"reasoning_content":"think"}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is True


# ── _restore_sse_line — spaceless lines get restored too ────────────


def test_restore_spaceless_anthropic_text_delta():
    real = "sk-" + "q" * 40
    _, mapping = redact_body({"t": real}, {})
    sur = mapping[real]

    def get(k):
        return ContentRestorer(mapping)

    evt = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "key was " + sur},
    }
    line = ("data:" + json.dumps(evt) + "\n").encode()  # NO space
    out = main._restore_sse_line(line, get)
    assert b"data: " in out  # normalised on rewrite
    restored = json.loads(out.split(b"data: ", 1)[1].rstrip())
    assert restored["delta"]["text"] == "key was " + real


def test_restore_spaceless_passthrough_when_no_surrogate():
    line = b'data:{"type":"ping"}\n'
    assert main._restore_sse_line(line, lambda k: ContentRestorer({})) == line

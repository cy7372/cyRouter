"""Unit tests for empty-output retry helpers.

Covers:
  - _is_empty_response: strict emptiness check (content + reasoning_content
    + tool_calls all empty) for OpenAI and Anthropic shapes. thinking blocks
    with a non-empty thinking field count as content (Anthropic protocol),
    matching the OpenAI path's reasoning_content handling.
  - _sse_line_has_content: SSE event-level content detection, including
    thinking_delta (which _observe_line misses — see main.py comment).
    thinking_delta is treated as content so thinking-mode streams don't
    trip the empty gate.

Does NOT exercise the streaming gate or retry loops — those need a mock
upstream and are out of scope for unit tests.
"""

from __future__ import annotations

import main


# ── _is_empty_response — OpenAI shape ─────────────────────────────────


def test_openai_content_present_is_not_empty():
    result = {"choices": [{"message": {"content": "hello"}}]}
    assert main._is_empty_response(result, anthropic_shape=False) is False


def test_openai_reasoning_only_is_not_empty():
    # GLM-5.x thinking mode: real reply sits in reasoning_content.
    # Must NOT be classified as empty (would cause infinite retry).
    result = {
        "choices": [{"message": {"content": "", "reasoning_content": "thinking..."}}]
    }
    assert main._is_empty_response(result, anthropic_shape=False) is False


def test_openai_tool_calls_only_is_not_empty():
    # Legitimate tool-only turn.
    result = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "t1", "function": {"name": "f"}}],
                }
            }
        ]
    }
    assert main._is_empty_response(result, anthropic_shape=False) is False


def test_openai_all_empty_is_empty():
    result = {
        "choices": [
            {"message": {"content": "", "reasoning_content": "", "tool_calls": []}}
        ]
    }
    assert main._is_empty_response(result, anthropic_shape=False) is True


def test_openai_no_choices_is_empty():
    result = {}
    assert main._is_empty_response(result, anthropic_shape=False) is True


def test_openai_null_content_is_empty():
    result = {"choices": [{"message": {"content": None}}]}
    assert main._is_empty_response(result, anthropic_shape=False) is True


# ── _is_empty_response — Anthropic shape ──────────────────────────────


def test_anthropic_text_block_is_not_empty():
    result = {"content": [{"type": "text", "text": "hi"}]}
    assert main._is_empty_response(result, anthropic_shape=True) is False


def test_anthropic_thinking_block_is_not_empty():
    # Pure thinking with non-empty thinking field is a legitimate content
    # block (Anthropic protocol) → NOT empty. Matches OpenAI reasoning_content.
    result = {"content": [{"type": "thinking", "thinking": "...reasoning..."}]}
    assert main._is_empty_response(result, anthropic_shape=True) is False


def test_anthropic_empty_thinking_block_is_empty():
    # thinking block whose thinking field is empty string → no substance.
    result = {"content": [{"type": "thinking", "thinking": ""}]}
    assert main._is_empty_response(result, anthropic_shape=True) is True


def test_anthropic_thinking_with_text_is_not_empty():
    # Thinking + text = legitimate response, not empty.
    result = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "result"}]}
    assert main._is_empty_response(result, anthropic_shape=True) is False


def test_anthropic_redacted_thinking_is_not_empty():
    # redacted_thinking payload is hidden, not absent — presence = content.
    result = {"content": [{"type": "redacted_thinking", "data": "abc"}]}
    assert main._is_empty_response(result, anthropic_shape=True) is False


def test_anthropic_tool_use_is_not_empty():
    result = {"content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]}
    assert main._is_empty_response(result, anthropic_shape=True) is False


def test_anthropic_empty_text_block_is_empty():
    result = {"content": [{"type": "text", "text": ""}]}
    assert main._is_empty_response(result, anthropic_shape=True) is True


def test_anthropic_no_blocks_is_empty():
    result = {"content": []}
    assert main._is_empty_response(result, anthropic_shape=True) is True


def test_anthropic_missing_content_is_empty():
    result = {}
    assert main._is_empty_response(result, anthropic_shape=True) is True


# ── _sse_line_has_content — OpenAI shape ──────────────────────────────


def test_openai_sse_content_delta_has_content():
    line = b'data: {"choices":[{"delta":{"content":"hi"}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is True


def test_openai_sse_reasoning_delta_has_content():
    line = b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is True


def test_openai_sse_tool_calls_delta_has_content():
    line = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"f"}}]}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is True


def test_openai_sse_empty_delta_no_content():
    line = b'data: {"choices":[{"delta":{}}]}'
    assert main._sse_line_has_content(line, anthropic_shape=False) is False


def test_openai_sse_done_marker_no_content():
    line = b"data: [DONE]"
    assert main._sse_line_has_content(line, anthropic_shape=False) is False


# ── _sse_line_has_content — Anthropic shape ───────────────────────────


def test_anthropic_sse_text_delta_has_content():
    line = b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_anthropic_sse_thinking_delta_has_content():
    # thinking_delta is a legitimate content event (Anthropic protocol).
    # Treat as content so the stream gate doesn't time out and trigger
    # pointless retries. Matches OpenAI path's reasoning_content handling.
    line = b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"..."}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_anthropic_sse_input_json_delta_has_content():
    line = b'data: {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\\"a\\""}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_anthropic_sse_tool_use_block_start_has_content():
    line = b'data: {"type":"content_block_start","content_block":{"type":"tool_use","name":"f"}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is True


def test_anthropic_sse_message_start_no_content():
    line = b'data: {"type":"message_start","message":{}}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is False


def test_anthropic_sse_message_stop_no_content():
    line = b'data: {"type":"message_stop"}'
    assert main._sse_line_has_content(line, anthropic_shape=True) is False


def test_anthropic_sse_empty_text_delta_no_content():
    # text_delta with empty string — edge case, treat as no content yet.
    line = (
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":""}}'
    )
    assert main._sse_line_has_content(line, anthropic_shape=True) is False


# ── _sse_line_has_content — invalid input ─────────────────────────────


def test_non_data_line_no_content():
    assert main._sse_line_has_content(b"event: ping", anthropic_shape=False) is False
    assert main._sse_line_has_content(b": heartbeat", anthropic_shape=True) is False


def test_malformed_json_no_content():
    assert (
        main._sse_line_has_content(b"data: {invalid json", anthropic_shape=False)
        is False
    )


def test_empty_line_no_content():
    assert main._sse_line_has_content(b"", anthropic_shape=False) is False


# ── _empty_response_jsonresponse ──────────────────────────────────────


def test_empty_response_openai_shape():
    resp = main._empty_response_jsonresponse(anthropic_shape=False, attempts=3)
    assert resp.status_code == 502
    body = bytes(resp.body).decode() if hasattr(resp, "body") else ""
    assert '"type":"empty_response"' in body
    assert "after 3 attempts" in body


def test_empty_response_anthropic_shape():
    resp = main._empty_response_jsonresponse(anthropic_shape=True, attempts=2)
    assert resp.status_code == 502
    body = bytes(resp.body).decode() if hasattr(resp, "body") else ""
    assert '"type":"error"' in body
    assert '"type":"empty_response"' in body
    assert "after 2 attempts" in body


def test_empty_response_zero_attempts_omits_suffix():
    resp = main._empty_response_jsonresponse(anthropic_shape=False, attempts=0)
    body = bytes(resp.body).decode() if hasattr(resp, "body") else ""
    assert "after 0 attempts" not in body

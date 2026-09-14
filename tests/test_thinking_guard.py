"""Unit tests for the thinking-mode guard helpers (main.py).

The old `_should_abort_thinking_only` pure helper was rearchitected into
two pure functions that the stream paths actually use:

- `_effective_gate_timeout`: the empty-stream retry gate must be extended
  when thinking may be active, so legitimate GLM-5.x thinking phases don't
  trigger empty-retry loops.
- `_is_empty_response`: strict "no user-visible content" check used to
  decide whether an upstream response warrants an empty retry. Matches
  LiteLLM's convention; thinking content (reasoning_content / thinking
  blocks) counts as content so thinking-mode responses aren't mis-retried.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402


# ── _effective_gate_timeout ───────────────────────────────────────────


def test_gate_uses_base_when_thinking_disabled():
    cfg = {"empty_retry_timeout": 90, "thinking_only_timeout": 120}
    body = {"thinking": {"type": "disabled"}}
    assert main._effective_gate_timeout(body, cfg) == 90


def test_gate_extended_when_thinking_enabled():
    cfg = {"empty_retry_timeout": 90, "thinking_only_timeout": 120}
    body = {"thinking": {"type": "enabled"}}
    assert main._effective_gate_timeout(body, cfg) == 120


def test_gate_extended_when_thinking_unspecified():
    # Not explicitly disabled → thinking may be active → extend the gate.
    cfg = {"empty_retry_timeout": 90, "thinking_only_timeout": 120}
    assert main._effective_gate_timeout({}, cfg) == 120


def test_gate_never_shorter_than_base():
    cfg = {"empty_retry_timeout": 90, "thinking_only_timeout": 30}
    assert main._effective_gate_timeout({}, cfg) == 90


def test_gate_zero_thinking_timeout_uses_base():
    cfg = {"empty_retry_timeout": 90, "thinking_only_timeout": 0}
    assert main._effective_gate_timeout({}, cfg) == 90


# ── _is_empty_response: OpenAI shape ──────────────────────────────────


def test_openai_empty_message_is_empty():
    r = {"choices": [{"message": {}}]}
    assert main._is_empty_response(r, anthropic_shape=False)


def test_openai_content_is_not_empty():
    r = {"choices": [{"message": {"content": "hello"}}]}
    assert not main._is_empty_response(r, anthropic_shape=False)


def test_openai_reasoning_only_is_not_empty():
    r = {"choices": [{"message": {"reasoning_content": "thinking..."}}]}
    assert not main._is_empty_response(r, anthropic_shape=False)


def test_openai_tool_calls_only_is_not_empty():
    r = {"choices": [{"message": {"tool_calls": [{"id": "t1"}]}}]}
    assert not main._is_empty_response(r, anthropic_shape=False)


def test_openai_empty_tool_calls_is_empty():
    r = {"choices": [{"message": {"tool_calls": []}}]}
    assert main._is_empty_response(r, anthropic_shape=False)


def test_openai_no_choices_is_empty():
    assert main._is_empty_response({}, anthropic_shape=False)
    # Runtime guard: non-dict results are treated as empty (triggers retry).
    assert main._is_empty_response("not a dict", anthropic_shape=False)  # type: ignore[arg-type]


# ── _is_empty_response: Anthropic shape ───────────────────────────────


def test_anthropic_text_block_is_not_empty():
    r = {"content": [{"type": "text", "text": "hello"}]}
    assert not main._is_empty_response(r, anthropic_shape=True)


def test_anthropic_thinking_block_is_not_empty():
    r = {"content": [{"type": "thinking", "thinking": "reasoning..."}]}
    assert not main._is_empty_response(r, anthropic_shape=True)


def test_anthropic_tool_use_block_is_not_empty():
    r = {"content": [{"type": "tool_use", "name": "bash", "input": {}}]}
    assert not main._is_empty_response(r, anthropic_shape=True)


def test_anthropic_redacted_thinking_is_not_empty():
    r = {"content": [{"type": "redacted_thinking"}]}
    assert not main._is_empty_response(r, anthropic_shape=True)


def test_anthropic_empty_content_is_empty():
    r = {"content": []}
    assert main._is_empty_response(r, anthropic_shape=True)
    r2 = {"content": [{"type": "text", "text": ""}]}
    assert main._is_empty_response(r2, anthropic_shape=True)


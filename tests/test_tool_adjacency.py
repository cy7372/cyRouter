"""Unit tests for the tool-adjacency repair layer (main.py).

Background (2026-08-28 DancherLink incident): pi-processes injects process
lifecycle events as user-role custom messages MID-TURN — between an
assistant's tool_call and its tool result. kimi code strictly validates
OpenAI adjacency (tool messages must immediately follow the assistant with
the matching tool_calls) and 400s the whole request:

    400 messages with role 'tool' must be a response to a preceeding
    message with 'tool_calls'

`_repair_tool_adjacency` reorders the turn to
[assistant(tool_calls), tool results..., intruding messages...] — nothing
dropped, relative orders preserved.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402


def _asst(tc_ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }
            for tc_id in tc_ids
        ],
    }


def _tool(tc_id, content="out"):
    return {"role": "tool", "tool_call_id": tc_id, "content": content}


INTRUDER = {"role": "user", "content": '<process_event type="lifecycle" kind="crash"/>'}


def test_noop_on_normal_history():
    msgs = [
        {"role": "user", "content": "hi"},
        _asst(["call_a"]),
        _tool("call_a"),
        {"role": "assistant", "content": "done"},
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert out == msgs
    assert (moved, intr) == (0, 0)


def test_real_incident_shape_user_between_call_and_result():
    msgs = [
        {"role": "user", "content": "kill it"},
        _asst(["call_taskkill"]),
        INTRUDER,
        _tool("call_taskkill", "SUCCESS: terminated"),
        _asst(["call_sleep"]),
        _tool("call_sleep"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert (moved, intr) == (1, 1)
    assert [m["role"] for m in out] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
    ]
    assert out[1]["tool_calls"][0]["id"] == "call_taskkill"
    assert out[2]["tool_call_id"] == "call_taskkill"
    assert out[3] is INTRUDER  # same object, not copied/dropped


def test_parallel_tools_hoisted_above_intruder():
    msgs = [
        _asst(["call_1", "call_2"]),
        INTRUDER,
        _tool("call_1", "r1"),
        _tool("call_2", "r2"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert (moved, intr) == (2, 1)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in out[1:3]] == ["call_1", "call_2"]


def test_intruder_between_split_tool_results():
    # tool1, intruder, tool2 → tools merged before intruder, order kept
    msgs = [
        _asst(["call_1", "call_2"]),
        _tool("call_1", "r1"),
        INTRUDER,
        _tool("call_2", "r2"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert (moved, intr) == (2, 1)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]
    assert [m["content"] for m in out[1:3]] == ["r1", "r2"]


def test_aborted_turn_without_results_untouched():
    # assistant tool_calls whose results never came: no tools to hoist
    msgs = [
        _asst(["call_gone"]),
        INTRUDER,
        {"role": "assistant", "content": "recovered"},
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert out == msgs
    assert (moved, intr) == (0, 0)


def test_intruders_after_all_tools_untouched():
    # user message AFTER the complete tool block is legal — don't move it
    msgs = [
        _asst(["call_a"]),
        _tool("call_a"),
        {"role": "user", "content": "next prompt"},
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert out == msgs
    assert (moved, intr) == (0, 0)


def test_foreign_tool_id_not_absorbed():
    # tool answering a DIFFERENT assistant stops the scan — never moved
    msgs = [
        _asst(["call_mine"]),
        INTRUDER,
        _tool("call_other"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert out == msgs
    assert (moved, intr) == (0, 0)


def test_next_assistant_stops_scan():
    # intruder then a NEW assistant with its own tools: the segment
    # belongs to the new turn; nothing to hoist for the outer assistant
    msgs = [
        _asst(["call_outer"]),
        INTRUDER,
        _asst(["call_inner"]),
        _tool("call_inner"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    assert out == msgs
    assert (moved, intr) == (0, 0)


def test_non_list_input_safe():
    out, moved, intr = main._repair_tool_adjacency(None)
    assert out is None
    assert (moved, intr) == (0, 0)


def test_multiple_broken_turns_both_repaired():
    msgs = [
        _asst(["call_a"]),
        INTRUDER,
        _tool("call_a"),
        {"role": "user", "content": "go"},
        _asst(["call_b", "call_c"]),
        {"role": "user", "content": "mid-turn inject"},
        _tool("call_b", "rb"),
        _tool("call_c", "rc"),
    ]
    out, moved, intr = main._repair_tool_adjacency(msgs)
    # turn 1: 1 tool hoisted above 2 intruders (mid-turn event + the next
    # "go" prompt, which is already after the tool and merely re-emitted in
    # place); turn 2: 2 tools above 1 intruder.
    assert (moved, intr) == (3, 3)
    roles = [m["role"] for m in out]
    assert roles == [
        "assistant",
        "tool",
        "user",
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert out[5]["tool_call_id"] == "call_b"
    assert out[6]["tool_call_id"] == "call_c"

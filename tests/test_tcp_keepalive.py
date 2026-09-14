"""TCP keepalive + FIN/RST close-style forensics (2026-09 experiment).

Two pieces wired into the upstream httpx clients:
  * _keepalive_socket_options — SO_KEEPALIVE with tightened probes
    (15s idle / 5s interval / 3 probes) so L4 middleboxes (NAT/LB flow
    tables) don't age out silent streams (openai-python #3269/#3503 fix).
  * _stream_close_exc_style — classifies stream-read exceptions into
    rst (ConnectionResetError → middlebox/crash, keepalive may help) /
    fin_abrupt (RemoteProtocolError "peer closed connection" → app-layer
    decision, keepalive can't help) / other.

Truncation blackbox triggers now carry close_style: "fin" on the clean-EOF
truncation path, and rst/fin_abrupt kills (which surface via the exception
branch) dump their own blackbox samples.
"""

import socket
import sys
from pathlib import Path

import httpcore
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402

# ── _keepalive_socket_options ─────────────────────────────────────────


def _optmap(cfg_over=None) -> dict:
    """_keepalive_socket_options() as {(level, optname): value} (3-tuples,
    not dict()-convertible directly)."""
    saved = main.load_config
    main.load_config = lambda: cfg_over or {}
    try:
        opts = main._keepalive_socket_options()
    finally:
        main.load_config = saved
    return {(lvl, name): val for lvl, name, val in opts}


def test_keepalive_always_enables_so_keepalive():
    opts = _optmap()
    assert opts[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1


def test_keepalive_disabled_leaves_only_so_keepalive():
    opts = _optmap({"tcp_keepalive": {"enabled": False}})
    assert opts == {(socket.SOL_SOCKET, socket.SO_KEEPALIVE): 1}


def test_keepalive_tightened_probes_under_kill_threshold():
    """First probe must fire well under the observed ~30s kill threshold."""
    opts = _optmap()
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None
    )
    if idle_opt is None:  # platform without tuning knobs — skip
        pytest.skip("no TCP keepidle socket option on this platform")
    assert opts[(socket.IPPROTO_TCP, idle_opt)] < 25
    if hasattr(socket, "TCP_KEEPINTVL"):
        assert opts[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] >= 1


def test_keepalive_config_overrides_defaults():
    opts = _optmap({"tcp_keepalive": {"idle_seconds": 20, "count": 7}})
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        pytest.skip("no TCP_KEEPIDLE on this platform")
    assert opts[(socket.IPPROTO_TCP, idle_opt)] == 20
    if hasattr(socket, "TCP_KEEPCNT"):
        assert opts[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] == 7


# ── _make_async_client ────────────────────────────────────────────────


def test_make_async_client_carries_keepalive_transport():
    """The shared factory wires socket_options into the transport."""
    saved = main.load_config
    main.load_config = dict
    try:
        client = main._make_async_client(trust_env=False)
    finally:
        main.load_config = saved
    # httpx exposes transport as _transport; verify it kept our socket options
    # by constructing one the same way and comparing the stored attribute.
    assert client._transport is not None
    # The real assertion: options list was accepted and is on the pool.
    pool = getattr(client._transport, "_pool", None)
    assert pool is not None
    assert getattr(pool, "_socket_options", None) is not None


# ── _stream_close_exc_style ───────────────────────────────────────────


def test_close_style_plain_reset():
    assert main._stream_close_exc_style(ConnectionResetError("reset by peer")) == "rst"


def test_close_style_wrapped_reset_via_cause():
    try:
        try:
            raise ConnectionResetError("[WinError 10054] reset")
        except ConnectionResetError as inner:
            raise httpx.ReadError("read failed") from inner
    except httpx.ReadError as e:
        assert main._stream_close_exc_style(e) == "rst"


def test_close_style_wrapped_reset_via_context():
    try:
        try:
            raise ConnectionResetError("ECONNRESET")
        except ConnectionResetError:
            raise httpcore.ReadError("boom")  # implicit __context__, no cause
    except httpcore.ReadError as e:
        assert main._stream_close_exc_style(e) == "rst"


def test_close_style_remote_protocol_peer_closed():
    err = httpcore.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )
    assert main._stream_close_exc_style(err) == "fin_abrupt"


def test_close_style_timeout_is_other():
    assert main._stream_close_exc_style(httpx.ReadTimeout("timed out")) == "other"


def test_close_style_text_fallback_rst():
    # Exception whose chain is empty but message carries the Windows code.
    assert main._stream_close_exc_style(RuntimeError("recv: WinError 10054 x")) == "rst"


def test_close_style_cycle_safe():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a  # pathological cycle must not hang
    assert main._stream_close_exc_style(a) == "other"


# ── notice text close-style tag ───────────────────────────────────────


def test_notice_default_fin_has_no_tag():
    txt = main._truncation_notice_text(
        provider_label="kimi code",
        elapsed=33.0,
        idle_gap=30.2,
        up_headers={},
    )
    assert "RST" not in txt and "（FIN）" not in txt
    assert "零字节" in txt


def test_notice_rst_tags_the_classification():
    txt = main._truncation_notice_text(
        provider_label="kimi code",
        elapsed=33.0,
        idle_gap=30.2,
        up_headers={},
        close_style="rst",
    )
    assert "（RST）" in txt

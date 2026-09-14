"""Integration tests for resource cleanup in the gate / chain helpers.

Verifies the three async helpers that broker upstream streams all close
their underlying iterator / response on EVERY exit path:

  - _gate_sdk_stream_for_content: closes `aiter` on success-with-content
    ONLY when caller abandons it later; on timeout / EOF / network error /
    cancellation the gate itself must close before re-raising.
  - _gate_probe_for_content: same contract for `probed.response`.
  - _chain_chunks: closes `live_aiter` from finally on normal completion,
    consumer cancellation, and consumer-side exception.

These cleanup paths matter because the underlying httpx connections live
in a shared pool — leaks don't surface immediately but eventually exhaust
the pool under load (PoolTimeout).

Uses pytest-asyncio in strict mode; each async test marked explicitly.
"""

from __future__ import annotations

import asyncio

import pytest

import main
from providers.base import StreamChunk


# ── Fake async iterators ─────────────────────────────────────────────


class FakeSDKStream:
    """Mimics provider.chat_stream(): async iterator of StreamChunk with
    an aclose() that the gate is expected to call on error paths.

    `behaviour` controls what __anext__ does:
      - ("chunks", [StreamChunk(...), ...]) -> yield chunks then StopAsyncIteration
      - ("sleep", float_seconds)            -> asyncio.sleep before next call
      - ("raise", ExceptionClass)           -> raise instance on first call
      - ("block",)                          -> await an Event forever (for cancel tests)
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.closed = False
        self._chunks_consumed = 0
        self._event = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        kind = self.behaviour[0]
        if kind == "chunks":
            chunks = self.behaviour[1]
            if self._chunks_consumed >= len(chunks):
                raise StopAsyncIteration
            c = chunks[self._chunks_consumed]
            self._chunks_consumed += 1
            return c
        if kind == "sleep":
            await asyncio.sleep(self.behaviour[1])
            raise StopAsyncIteration
        if kind == "raise":
            exc_cls = self.behaviour[1]
            raise exc_cls("simulated upstream error")
        if kind == "block":
            await self._event.wait()
            raise StopAsyncIteration
        raise AssertionError(f"unknown behaviour: {self.behaviour}")

    async def aclose(self):
        self.closed = True


class FakeByteStream:
    """Mimics httpx response.aiter_bytes() + response.aclose() for the
    raw-proxy gate path."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.closed = False
        self._chunks_consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        kind = self.behaviour[0]
        if kind == "bytes":
            chunks = self.behaviour[1]
            if self._chunks_consumed >= len(chunks):
                raise StopAsyncIteration
            c = chunks[self._chunks_consumed]
            self._chunks_consumed += 1
            return c
        if kind == "sleep":
            await asyncio.sleep(self.behaviour[1])
            raise StopAsyncIteration
        if kind == "raise":
            exc_cls = self.behaviour[1]
            raise exc_cls("simulated httpx read error")
        raise AssertionError(f"unknown behaviour: {self.behaviour}")

    async def aclose(self):
        self.closed = True


def _make_probed(first_chunk=b"", byte_iter=None):
    """Build a ProbedStream wired to a FakeByteStream response."""
    p = main.ProbedStream()
    p.first_chunk = first_chunk
    p.byte_iter = byte_iter or FakeByteStream(("bytes", []))
    p.response = p.byte_iter  # type: ignore[assignment]  # same object so closing either shows up as closed=True
    return p


# ── _gate_sdk_stream_for_content ─────────────────────────────────────


@pytest.mark.asyncio
async def test_sdk_gate_success_does_not_close():
    """First chunk has delta → gate returns (buffered, aiter); aiter NOT closed."""
    aiter = FakeSDKStream(
        ("chunks", [StreamChunk(delta="hi", model="m", provider="p")])
    )
    buffered, returned = await main._gate_sdk_stream_for_content(
        aiter, gate_timeout=1.0
    )
    assert returned is aiter
    assert len(buffered) == 1
    assert buffered[0].delta == "hi"
    assert aiter.closed is False


@pytest.mark.asyncio
async def test_sdk_gate_skips_empty_delta_chunks_then_succeeds():
    """Chunks with empty delta are buffered but don't trigger return; the
    first non-empty delta chunk does. Iterator stays open for caller."""
    aiter = FakeSDKStream(
        (
            "chunks",
            [
                StreamChunk(delta="", model="m", provider="p"),
                StreamChunk(delta="", model="m", provider="p"),
                StreamChunk(delta="real", model="m", provider="p"),
            ],
        )
    )
    buffered, _ = await main._gate_sdk_stream_for_content(aiter, gate_timeout=1.0)
    assert len(buffered) == 3
    assert buffered[-1].delta == "real"
    assert aiter.closed is False


@pytest.mark.asyncio
async def test_sdk_gate_timeout_closes_aiter():
    """deadline exceeded → EmptyStreamError; aiter closed."""
    aiter = FakeSDKStream(("sleep", 0.3))
    with pytest.raises(main.EmptyStreamError):
        await main._gate_sdk_stream_for_content(aiter, gate_timeout=0.05)
    assert aiter.closed is True


@pytest.mark.asyncio
async def test_sdk_gate_eof_closes_aiter():
    """StopAsyncIteration without any content → EmptyStreamError; aiter closed."""
    aiter = FakeSDKStream(("chunks", []))
    with pytest.raises(main.EmptyStreamError):
        await main._gate_sdk_stream_for_content(aiter, gate_timeout=1.0)
    assert aiter.closed is True


@pytest.mark.asyncio
async def test_sdk_gate_network_error_propagates_and_closes():
    """Underlying __anext__ raises a non-Empty/non-Timeout exception (e.g.
    httpx.ReadError模拟成 RuntimeError). The gate must close aiter before
    re-raising — this is the bug we just fixed."""

    class _SimulatedReadError(Exception):
        pass

    aiter = FakeSDKStream(("raise", _SimulatedReadError))
    with pytest.raises(_SimulatedReadError):
        await main._gate_sdk_stream_for_content(aiter, gate_timeout=1.0)
    assert aiter.closed is True


@pytest.mark.asyncio
async def test_sdk_gate_cancel_closes_aiter():
    """Caller cancels mid-gate → CancelledError propagates; aiter closed."""

    aiter = FakeSDKStream(("block",))

    async def _runner():
        await main._gate_sdk_stream_for_content(aiter, gate_timeout=10.0)

    task = asyncio.create_task(_runner())
    # Let the task settle into the gate's wait_for.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert aiter.closed is True


# ── _gate_probe_for_content ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_raw_gate_first_chunk_has_content_returns_without_close():
    """Probe already saw content in first_chunk → return immediately,
    response NOT closed (caller takes ownership for forwarding)."""
    # OpenAI-shape content delta in first_chunk
    first = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    probed = _make_probed(first_chunk=first)
    result = await main._gate_probe_for_content(
        probed,
        anthropic_shape=False,
        gate_timeout=1.0,
        log_entry={"session_id": "test", "trace_id": "t"},
        start_time=0.0,
    )
    assert result.error_response is None
    assert probed.response.closed is False  # type: ignore[union-attr, attr-defined]


@pytest.mark.asyncio
async def test_raw_gate_sees_content_in_byte_iter_returns_without_close():
    """first_chunk empty, byte_iter yields a content event → return; NOT closed."""
    probed = _make_probed(
        byte_iter=FakeByteStream(
            ("bytes", [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'])
        )
    )
    result = await main._gate_probe_for_content(
        probed,
        anthropic_shape=False,
        gate_timeout=1.0,
        log_entry={"session_id": "test", "trace_id": "t"},
        start_time=0.0,
    )
    assert result.error_response is None
    assert probed.response.closed is False  # type: ignore[union-attr, attr-defined]


@pytest.mark.asyncio
async def test_raw_gate_timeout_sets_error_and_closes():
    """deadline exceeded → probed.error_response set; response closed.

    Note: the gate nulls probed.response after closing it, so we capture
    the fake reference up-front and check its .closed flag directly."""
    fake_resp = FakeByteStream(("sleep", 0.3))
    probed = _make_probed(byte_iter=fake_resp)
    result = await main._gate_probe_for_content(
        probed,
        anthropic_shape=False,
        gate_timeout=0.05,
        log_entry={"session_id": "test", "trace_id": "t"},
        start_time=0.0,
    )
    assert result.error_response is not None
    assert result.error_response.status_code == 502
    assert fake_resp.closed is True


@pytest.mark.asyncio
async def test_raw_gate_eof_sets_error_and_closes():
    """byte_iter StopAsyncIteration without content → error_response + closed."""
    fake_resp = FakeByteStream(("bytes", []))
    probed = _make_probed(byte_iter=fake_resp)
    result = await main._gate_probe_for_content(
        probed,
        anthropic_shape=False,
        gate_timeout=1.0,
        log_entry={"session_id": "test", "trace_id": "t"},
        start_time=0.0,
    )
    assert result.error_response is not None
    assert fake_resp.closed is True


@pytest.mark.asyncio
async def test_raw_gate_network_error_propagates_and_closes():
    """byte_iter raises a non-Timeout/non-EOF exception → propagates AND
    closes response. This is the raw-proxy counterpart of the SDK bug."""

    class _SimulatedReadError(Exception):
        pass

    probed = _make_probed(byte_iter=FakeByteStream(("raise", _SimulatedReadError)))
    with pytest.raises(_SimulatedReadError):
        await main._gate_probe_for_content(
            probed,
            anthropic_shape=False,
            gate_timeout=1.0,
            log_entry={"session_id": "test", "trace_id": "t"},
            start_time=0.0,
        )
    assert probed.response.closed is True  # type: ignore[union-attr, attr-defined]


@pytest.mark.asyncio
async def test_raw_gate_cancel_propagates_and_closes():
    """Caller cancels mid-gate → CancelledError + response closed."""
    probed = _make_probed(byte_iter=FakeByteStream(("sleep", 5.0)))

    async def _runner():
        await main._gate_probe_for_content(
            probed,
            anthropic_shape=False,
            gate_timeout=10.0,
            log_entry={"session_id": "test", "trace_id": "t"},
            start_time=0.0,
        )

    task = asyncio.create_task(_runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert probed.response.closed is True  # type: ignore[union-attr, attr-defined]


# ── _chain_chunks ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chain_chunks_normal_completion_closes_live():
    """Consumer drains everything → live_aiter closed in finally."""
    live = FakeSDKStream(("chunks", [StreamChunk(delta="b", model="m", provider="p")]))
    buffered = [StreamChunk(delta="a", model="m", provider="p")]
    out = []
    async for c in main._chain_chunks(buffered, live):
        out.append(c.delta)
    assert out == ["a", "b"]
    assert live.closed is True


@pytest.mark.asyncio
async def test_chain_chunks_consumer_cancel_closes_live():
    """Consumer aclose() mid-iteration (GeneratorExit) → live_aiter closed.

    This mirrors how Starlette's StreamingResponse cleans up its body
    iterator when a client disconnects: it explicitly calls aclose(),
    which triggers our finally block."""

    live = FakeSDKStream(
        (
            "chunks",
            [StreamChunk(delta="b", model="m", provider="p")],
        )
    )

    gen = main._chain_chunks([], live)
    # Pull one value to start iteration, then explicitly close.
    first = await gen.__anext__()
    assert first.delta == "b"
    await gen.aclose()  # type: ignore[attr-defined]
    assert live.closed is True

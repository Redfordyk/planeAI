"""Unit tests for ai.streaming.claude_sse async generator.

We mock anthropic.AsyncAnthropic so no network is required and the
generator runs deterministically. The tests confirm:
  - sources frame comes first
  - delta frames in order
  - done frame last with usage block
  - error paths return an `error` frame (and no `done`)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from ai import streaming


@pytest.fixture
def stub_cfg():
    return SimpleNamespace(
        anthropic_key="sk-ant-test",
        chat_model="claude-haiku-4-5-20251001",
    )


def _async_stream(chunks):
    """Build an async-iterable that yields the given chunks."""

    class _AsyncIter:
        def __init__(self, items):
            self.items = list(items)
            self.i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.i >= len(self.items):
                raise StopAsyncIteration
            v = self.items[self.i]
            self.i += 1
            return v

    return _AsyncIter(chunks)


def _stream_ctx(chunks, final_usage):
    """A fake `async with client.messages.stream(...) as stream` ctx."""

    class _Stream:
        text_stream = _async_stream(chunks)

        async def get_final_message(self):
            return SimpleNamespace(usage=final_usage)

    class _Ctx:
        async def __aenter__(self):
            return _Stream()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return _Ctx()


def _parse_frames(body: str):
    """Split SSE body into a list of decoded JSON events."""
    out = []
    for line in body.split("\n\n"):
        line = line.strip()
        if not line:
            continue
        assert line.startswith("data: "), line
        out.append(json.loads(line[len("data: ") :]))
    return out


@pytest.mark.asyncio
async def test_happy_path_sources_then_deltas_then_done(stub_cfg, monkeypatch):
    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(
        return_value=_stream_ctx(
            ["hello ", "world"],
            final_usage={"input_tokens": 10, "output_tokens": 3},
        )
    )
    monkeypatch.setattr(
        streaming.anthropic, "AsyncAnthropic", lambda **kw: fake_client
    )

    # record_usage hits ORM; stub it out for this unit test.
    async_record = AsyncMock(return_value="0.000045")
    monkeypatch.setattr(streaming, "sync_to_async", lambda fn, **kw: async_record)

    sources = [{"source_type": "work_item", "source_id": "abc", "project_id": "p"}]
    body = ""
    async for frame in streaming.claude_sse(
        cfg=stub_cfg,
        system="sys",
        messages=[{"role": "user", "content": "q"}],
        sources=sources,
        workspace_id="ws",
        user_id="user",
    ):
        body += frame

    frames = _parse_frames(body)
    assert frames[0]["sources"] == sources
    deltas = [f for f in frames if "delta" in f]
    assert [d["delta"] for d in deltas] == ["hello ", "world"]
    assert frames[-1]["done"] is True
    assert frames[-1]["usage"]["model"] == stub_cfg.chat_model


@pytest.mark.asyncio
async def test_rate_limit_returns_error_frame(stub_cfg, monkeypatch):
    err = anthropic.RateLimitError(
        message="429", response=MagicMock(status_code=429), body=None
    )
    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(side_effect=err)
    monkeypatch.setattr(
        streaming.anthropic, "AsyncAnthropic", lambda **kw: fake_client
    )

    body = ""
    async for frame in streaming.claude_sse(
        cfg=stub_cfg,
        system="sys",
        messages=[],
        sources=[],
        workspace_id="ws",
        user_id="user",
    ):
        body += frame

    frames = _parse_frames(body)
    # sources frame first (empty list is fine), then error frame, NO done.
    assert frames[0] == {"sources": []}
    assert any("error" in f for f in frames)
    assert not any("done" in f for f in frames)


@pytest.mark.asyncio
async def test_auth_error_returns_specific_message(stub_cfg, monkeypatch):
    err = anthropic.AuthenticationError(
        message="bad key", response=MagicMock(status_code=401), body=None
    )
    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(side_effect=err)
    monkeypatch.setattr(
        streaming.anthropic, "AsyncAnthropic", lambda **kw: fake_client
    )

    body = ""
    async for frame in streaming.claude_sse(
        cfg=stub_cfg,
        system="s",
        messages=[],
        sources=[],
        workspace_id="ws",
        user_id="u",
    ):
        body += frame

    frames = _parse_frames(body)
    err_frames = [f for f in frames if "error" in f]
    assert err_frames
    assert "ключ" in err_frames[0]["error"].lower()


def test_sse_headers_have_no_buffering():
    h = streaming.sse_response_headers()
    assert h["Cache-Control"] == "no-cache"
    assert h["X-Accel-Buffering"] == "no"


def test_sse_frame_format():
    # data: <json>\n\n is the SSE protocol shape; trailing double-newline
    # is what makes the browser dispatch the event.
    frame = streaming._sse({"hello": "world"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = frame[len("data: ") : -2]
    assert json.loads(payload) == {"hello": "world"}

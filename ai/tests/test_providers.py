"""Unit tests for ai.providers.

No Django, no network, no DB. We monkeypatch the SDK clients with
fakes that exercise the batching + retry/backoff loops. This file is
the seed for the full pytest infrastructure that lands in TZ 1.9 —
keep it lightweight and runnable on its own:

    pip install pytest anthropic openai
    pytest ai/tests/test_providers.py

(Inside CI / staging, the real env already has these packages from
Dockerfile.ai.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import openai
import pytest

from ai import providers


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Replace the backoff sleep with a no-op so tests run instantly."""
    monkeypatch.setattr(providers, "_sleep_backoff", lambda *a, **kw: None)


# --- OpenAIEmbed -----------------------------------------------------------


def _embed_response(dim: int, count: int, total_tokens: int):
    """Build an object matching openai.types.CreateEmbeddingResponse shape."""
    data = [SimpleNamespace(embedding=[0.0] * dim) for _ in range(count)]
    return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=total_tokens))


def test_embed_batches_in_chunks_of_100(monkeypatch):
    """250 inputs → 3 batches (100 + 100 + 50). Vectors and token sums add up."""
    fake = MagicMock()
    fake.embeddings.create.side_effect = [
        _embed_response(providers.EMBED_DIM, 100, 1000),
        _embed_response(providers.EMBED_DIM, 100, 1000),
        _embed_response(providers.EMBED_DIM, 50, 500),
    ]
    monkeypatch.setattr(providers.openai, "OpenAI", lambda **kw: fake)

    embed = providers.OpenAIEmbed(api_key="sk-test")
    vectors, tokens = embed.embed(["t"] * 250)

    assert len(vectors) == 250
    assert all(len(v) == providers.EMBED_DIM for v in vectors)
    assert tokens == 2500
    assert fake.embeddings.create.call_count == 3
    # All three calls used the configured model name.
    for call in fake.embeddings.create.call_args_list:
        assert call.kwargs["model"] == providers.EMBED_MODEL


def test_embed_retries_on_rate_limit_then_succeeds(monkeypatch):
    """First call 429s, second succeeds. We get one batch of vectors."""
    fake = MagicMock()
    rate_err = openai.RateLimitError(
        message="rate", response=MagicMock(status_code=429), body=None
    )
    fake.embeddings.create.side_effect = [
        rate_err,
        _embed_response(providers.EMBED_DIM, 3, 30),
    ]
    monkeypatch.setattr(providers.openai, "OpenAI", lambda **kw: fake)

    embed = providers.OpenAIEmbed(api_key="sk-test")
    vectors, tokens = embed.embed(["a", "b", "c"])

    assert len(vectors) == 3
    assert tokens == 30
    assert fake.embeddings.create.call_count == 2


def test_embed_gives_up_after_max_retries(monkeypatch):
    fake = MagicMock()
    fake.embeddings.create.side_effect = openai.RateLimitError(
        message="rate", response=MagicMock(status_code=429), body=None
    )
    monkeypatch.setattr(providers.openai, "OpenAI", lambda **kw: fake)

    embed = providers.OpenAIEmbed(api_key="sk-test")
    with pytest.raises(openai.RateLimitError):
        embed.embed(["x"])
    assert fake.embeddings.create.call_count == providers.MAX_RETRIES


# --- ClaudeChat ------------------------------------------------------------


def _claude_response(text: str = "hi"):
    return SimpleNamespace(
        id="msg_01",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=2),
    )


def test_claude_complete_passes_args_through(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value = _claude_response("ok")
    monkeypatch.setattr(providers.anthropic, "Anthropic", lambda **kw: fake)

    chat = providers.ClaudeChat(api_key="sk-ant-test")
    out = chat.complete(
        system="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
        model=providers.CHEAP_MODEL,
        max_tokens=64,
    )

    assert out.content[0].text == "ok"
    call = fake.messages.create.call_args
    assert call.kwargs["model"] == providers.CHEAP_MODEL
    assert call.kwargs["max_tokens"] == 64
    assert call.kwargs["temperature"] == 0.1
    # tools defaults to [] (NOT None — that would be an SDK error).
    assert call.kwargs["tools"] == []


def test_claude_retries_on_rate_limit(monkeypatch):
    fake = MagicMock()
    rate_err = anthropic.RateLimitError(
        message="rate", response=MagicMock(status_code=429), body=None
    )
    fake.messages.create.side_effect = [rate_err, _claude_response("ok")]
    monkeypatch.setattr(providers.anthropic, "Anthropic", lambda **kw: fake)

    chat = providers.ClaudeChat(api_key="sk-ant-test")
    out = chat.complete(system="s", messages=[{"role": "user", "content": "x"}])

    assert out.content[0].text == "ok"
    assert fake.messages.create.call_count == 2


def test_claude_gives_up_after_max_retries(monkeypatch):
    fake = MagicMock()
    fake.messages.create.side_effect = anthropic.RateLimitError(
        message="rate", response=MagicMock(status_code=429), body=None
    )
    monkeypatch.setattr(providers.anthropic, "Anthropic", lambda **kw: fake)

    chat = providers.ClaudeChat(api_key="sk-ant-test")
    with pytest.raises(anthropic.RateLimitError):
        chat.complete(system="s", messages=[{"role": "user", "content": "x"}])
    assert fake.messages.create.call_count == providers.MAX_RETRIES


# --- Keys NEVER appear in logs --------------------------------------------


def test_no_key_in_logs(monkeypatch, caplog):
    """Even when retrying, the api_key must not appear in any log line."""
    fake = MagicMock()
    rate_err = openai.RateLimitError(
        message="rate", response=MagicMock(status_code=429), body=None
    )
    fake.embeddings.create.side_effect = [
        rate_err,
        _embed_response(providers.EMBED_DIM, 1, 1),
    ]
    monkeypatch.setattr(providers.openai, "OpenAI", lambda **kw: fake)

    SECRET = "sk-proj-DO-NOT-LEAK-ME-1234567890"
    with caplog.at_level("DEBUG", logger="plane.ai.providers"):
        providers.OpenAIEmbed(api_key=SECRET).embed(["x"])

    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in repr(rec.args or "")

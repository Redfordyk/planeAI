"""Pricing math tests — pure Python, no Django."""

from __future__ import annotations

from decimal import Decimal

from ai.pricing import (
    chat_cost,
    embed_cost,
)


def test_embed_cost_small_known_pricing():
    # text-embedding-3-small @ $0.02 / 1M => 1e6 tokens = $0.02
    assert embed_cost("text-embedding-3-small", 1_000_000) == Decimal("0.02")
    # 500 tokens => $0.00001
    assert embed_cost("text-embedding-3-small", 500) == Decimal("0.00001")


def test_embed_cost_unknown_model_falls_back_to_small():
    # Defensive: a typo in the model name should not crash accounting.
    assert embed_cost("text-embedding-3-bogus", 1) == Decimal("0.00000002")


def test_chat_cost_sonnet_input_only():
    # 1k input tokens at $3 / 1M = $0.003
    assert chat_cost("claude-sonnet-4-6", input_tokens=1000) == Decimal("0.003")


def test_chat_cost_sonnet_mixed():
    cost = chat_cost(
        "claude-sonnet-4-6",
        input_tokens=1000,        # $0.003
        output_tokens=500,        # $0.0075
        cache_read_tokens=2000,   # $0.0006 (10% of input rate)
        cache_creation_tokens=100,  # $0.000375 (125% of input rate)
    )
    expected = (
        Decimal("0.000003") * 1000
        + Decimal("0.000015") * 500
        + Decimal("0.0000003") * 2000
        + Decimal("0.00000375") * 100
    )
    assert cost == expected


def test_chat_cost_haiku_is_cheaper_than_sonnet():
    # Sanity: haiku must be cheaper for identical input
    s = chat_cost("claude-sonnet-4-6", input_tokens=1000, output_tokens=1000)
    h = chat_cost("claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=1000)
    assert h < s


def test_chat_cost_cache_read_is_one_tenth_of_input():
    # 10x cache_read tokens costs the same as 1x fresh input.
    fresh = chat_cost("claude-sonnet-4-6", input_tokens=100)
    cached = chat_cost("claude-sonnet-4-6", cache_read_tokens=1000)
    assert fresh == cached

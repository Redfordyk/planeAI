"""Per-model token pricing in USD per single token.

Single source of truth for cost calculation across the AI pipeline.
Used by:
  - ai.tasks.reindex_source — embedding-cost log entry
  - ai.usage (TZ 1.7) — generic `record_usage` helper
  - ai.budget (TZ 1.7) — projecting monthly spend from token counters

Values are converted from public per-million pricing into per-token
Decimal literals so cost arithmetic stays exact. Update intentionally
when provider prices change — bump the dict, commit, deploy.
"""

from __future__ import annotations

from decimal import Decimal


# OpenAI: https://openai.com/api/pricing/  (2026-05 snapshot)
#   text-embedding-3-small  $0.02 / 1M tokens
#   text-embedding-3-large  $0.13 / 1M tokens
EMBED_PRICING: dict[str, Decimal] = {
    "text-embedding-3-small": Decimal("0.00000002"),
    "text-embedding-3-large": Decimal("0.00000013"),
}


# Anthropic: https://www.anthropic.com/pricing#api  (2026-05 snapshot).
# `input`, `output`, `cache_read`, `cache_write` per token.
#   claude-sonnet-4-6      $3 / $15 / $0.30 / $3.75 per 1M
#   claude-haiku-4-5       $1 / $5  / $0.10 / $1.25 per 1M
CHAT_PRICING: dict[str, dict[str, Decimal]] = {
    "claude-sonnet-4-6": {
        "input": Decimal("0.000003"),
        "output": Decimal("0.000015"),
        "cache_read": Decimal("0.0000003"),
        "cache_write": Decimal("0.00000375"),
    },
    "claude-haiku-4-5-20251001": {
        "input": Decimal("0.000001"),
        "output": Decimal("0.000005"),
        "cache_read": Decimal("0.0000001"),
        "cache_write": Decimal("0.00000125"),
    },
}


def embed_cost(model: str, tokens: int) -> Decimal:
    """Return USD cost for an embedding call. Falls back to the small
    model price when an unknown model is supplied — a noisy log line
    in production, but never crashes the accounting path.
    """
    rate = EMBED_PRICING.get(model, EMBED_PRICING["text-embedding-3-small"])
    return rate * Decimal(tokens)


def chat_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """Return USD cost for a single Anthropic call. Token kinds map
    1:1 to AIUsageLog columns."""
    rates = CHAT_PRICING.get(model, CHAT_PRICING["claude-sonnet-4-6"])
    return (
        rates["input"] * Decimal(input_tokens)
        + rates["output"] * Decimal(output_tokens)
        + rates["cache_read"] * Decimal(cache_read_tokens)
        + rates["cache_write"] * Decimal(cache_creation_tokens)
    )

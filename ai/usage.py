"""Single chokepoint for writing ``AIUsageLog`` rows.

Every feature path that talks to Anthropic or OpenAI must funnel its
billing through ``record_usage`` so that:

  - costs are computed from one pricing table (``ai.pricing``);
  - cache-read / cache-creation tokens are accounted for separately
    (Anthropic charges them at lower / higher rates than fresh input);
  - the per-workspace monthly budget guard (``ai.guards``) has a
    consistent denominator.

Async callers (TZ 2.3 SSE streaming) MUST wrap the call in
``asgiref.sync.sync_to_async`` — this function does an ORM write,
which is the canonical sync-only path Django warns about in async
contexts. See STREAMING.md.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from ai.models import AIUsageLog, WorkspaceAIConfig
from ai.pricing import chat_cost, embed_cost


logger = logging.getLogger("plane.ai.usage")


def _usage_field(usage: Any, *names: str, default: int = 0) -> int:
    """Read an attribute or dict key from `usage`, tolerating either
    shape (Anthropic SDK returns objects, OpenAI returns objects with
    `.total_tokens`, and tests sometimes pass plain dicts)."""
    for name in names:
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        if v is not None:
            return int(v)
    return default


def record_usage(
    *,
    workspace_id,
    user_id,
    feature: str,
    model: str,
    usage: Any,
) -> Decimal:
    """Write one AIUsageLog row, return computed USD cost.

    `usage` can be any of:
      - anthropic.types.Usage  (attrs: input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens)
      - openai.types.CompletionUsage / EmbeddingUsage
        (attrs: total_tokens, input_tokens for embeddings)
      - dict with the same keys (for tests)
    """
    input_tokens = _usage_field(usage, "input_tokens", "total_tokens", "prompt_tokens")
    output_tokens = _usage_field(usage, "output_tokens", "completion_tokens")
    cache_read = _usage_field(usage, "cache_read_input_tokens", "cache_read_tokens")
    cache_creation = _usage_field(
        usage, "cache_creation_input_tokens", "cache_creation_tokens"
    )

    if feature == AIUsageLog.FEATURE_EMBED:
        cost = embed_cost(model, input_tokens)
    else:
        cost = chat_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
        )

    AIUsageLog.objects.create(
        workspace_id=workspace_id,
        user_id=user_id,
        feature=feature,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=cost,
    )
    return cost


def tokens_used_this_month(workspace_id) -> int:
    """Total ``input + output + cache_creation`` tokens for the
    current calendar month.

    Cache-read tokens deliberately don't count toward the budget: they
    are already a discount on fresh inputs, and double-counting them
    would punish the workspace for using prompt caching.
    """
    start = timezone.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    agg = AIUsageLog.objects.filter(
        workspace_id=workspace_id, created_at__gte=start
    ).aggregate(
        i=Sum("input_tokens"),
        o=Sum("output_tokens"),
        c=Sum("cache_creation_tokens"),
    )
    return (agg["i"] or 0) + (agg["o"] or 0) + (agg["c"] or 0)


def budget_status(workspace_id) -> tuple[int, int, bool]:
    """Return ``(used, budget, exceeded)`` for a workspace.

    ``exceeded`` is True iff ``used >= budget`` (inclusive — at the
    line, the next request gets 429). If the workspace has no
    AI config, returns ``(0, 0, True)`` — caller treats that as a
    soft block.
    """
    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id, enabled=True)
        .only("monthly_token_budget")
        .first()
    )
    if cfg is None:
        return 0, 0, True
    used = tokens_used_this_month(workspace_id)
    return used, cfg.monthly_token_budget, used >= cfg.monthly_token_budget

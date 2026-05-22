"""LLM provider abstraction.

Single import boundary the rest of `ai/` uses for anything that
touches Anthropic or OpenAI. The point of the boundary:

  - SDK details (model IDs, batching, retry windows) live here, not in
    feature code (search, summarise, bulk, agents — TZ 2.x onward).
  - All key reads go through `get_chat(workspace_id)` /
    `get_embed(workspace_id)`, which pull from `WorkspaceAIConfig`
    (encrypted at rest, TZ 0.11 / 1.1). Code that needs a key MUST go
    through those helpers — never read `os.environ["ANTHROPIC_API_KEY"]`
    directly in feature code, that bypasses per-workspace
    configuration and the `enabled` gate.
  - Retry/backoff is observable: we control it instead of leaving it
    to the SDK's hidden `max_retries`, so 1.4 / 1.7 can log per-attempt
    metrics and surface long-tail latencies.

Token accounting (`AIUsageLog`) is intentionally NOT inside this
module — it's the caller's responsibility (TZ 1.7). Keeping that
separation means feature code controls feature attribution
(intent_search vs summarize vs bulk vs agent).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import anthropic
import openai


logger = logging.getLogger("plane.ai.providers")


CHAT_MODEL = "claude-sonnet-4-6"
CHEAP_MODEL = "claude-haiku-4-5-20251001"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

EMBED_BATCH = 100
MAX_RETRIES = 5


def _sleep_backoff(attempt: int, *, jitter: bool = True) -> None:
    """Exponential backoff with optional jitter.

    Pulled into its own helper so the retry loops below stay readable
    and the unit test can monkeypatch a no-op (avoids real sleeps in
    test runs).
    """
    delay = 2**attempt
    if jitter:
        delay += random.uniform(0, 1)
    time.sleep(delay)


class ClaudeChat:
    """Thin sync wrapper over `anthropic.Anthropic.messages.create`.

    Async variant (`AsyncClaudeChat`) lands with the SSE-streaming view
    in TZ 2.3 — same shape, different client. Keep the sync path
    around for Celery tasks (summarise, bulk, agent) where blocking is
    fine and async isn't worth the complexity.
    """

    def __init__(self, api_key: str) -> None:
        # max_retries=0: the SDK has a built-in retry but we want to
        # see and log each attempt. We retry in `complete` below.
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = CHAT_MODEL,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ):
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools or [],
                    temperature=temperature,
                )
            except anthropic.RateLimitError as e:
                last_exc = e
                # 429 — backoff with jitter
                logger.warning(
                    "anthropic rate-limit on attempt %d/%d (model=%s)",
                    attempt + 1,
                    MAX_RETRIES,
                    model,
                )
                if attempt == MAX_RETRIES - 1:
                    raise
                _sleep_backoff(attempt, jitter=True)
            except anthropic.APIError as e:
                last_exc = e
                logger.warning(
                    "anthropic transient error on attempt %d/%d: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    type(e).__name__,
                )
                if attempt == MAX_RETRIES - 1:
                    raise
                _sleep_backoff(attempt, jitter=False)
        # Defensive: loop should always either return or raise.
        raise RuntimeError("ClaudeChat.complete exhausted retries") from last_exc


class OpenAIEmbed:
    """Batched embedding client. Returns `(vectors, total_tokens)`.

    Caller MUST destructure the tuple — sloppy use like
    `vecs = embed.embed(texts)` then iterating `vecs` would silently
    iterate over `(vectors, total_tokens)`. The signature is enforced
    by type hints; mypy/pyright will flag misuse.
    """

    def __init__(self, api_key: str, model: str = EMBED_MODEL) -> None:
        self.client = openai.OpenAI(api_key=api_key, max_retries=0)
        self.model = model

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        vectors: list[list[float]] = []
        total_tokens = 0

        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            for attempt in range(MAX_RETRIES):
                try:
                    response = self.client.embeddings.create(
                        model=self.model, input=batch
                    )
                except openai.RateLimitError:
                    logger.warning(
                        "openai rate-limit on embed attempt %d/%d (batch_size=%d)",
                        attempt + 1,
                        MAX_RETRIES,
                        len(batch),
                    )
                    if attempt == MAX_RETRIES - 1:
                        raise
                    _sleep_backoff(attempt, jitter=True)
                    continue
                except openai.APIError as e:
                    logger.warning(
                        "openai transient error on embed attempt %d/%d: %s",
                        attempt + 1,
                        MAX_RETRIES,
                        type(e).__name__,
                    )
                    if attempt == MAX_RETRIES - 1:
                        raise
                    _sleep_backoff(attempt, jitter=False)
                    continue

                vectors.extend(d.embedding for d in response.data)
                total_tokens += response.usage.total_tokens
                break

        return vectors, total_tokens


# --- Config-driven helpers --------------------------------------------------
#
# Callers (TZ 1.5 indexer, 2.1 retrieval, etc.) ask for a client by
# workspace_id, never by a raw key. If the workspace has no config or
# the config is disabled, these helpers raise — feature code should
# catch and return a 4xx to users / skip background work.


class AIDisabled(Exception):
    """Raised when a workspace has no enabled AI config."""


def _config(workspace_id) -> Any:
    # Local import: avoids loading Django models at module import time,
    # which lets the unit tests in ai/tests/test_providers.py run
    # without any DB / Django setup.
    from ai.models import WorkspaceAIConfig

    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id, enabled=True)
        .only(
            "anthropic_key", "openai_key", "chat_model", "embed_model"
        )
        .first()
    )
    if cfg is None:
        raise AIDisabled(f"workspace {workspace_id} has no enabled AI config")
    return cfg


def get_chat(workspace_id) -> ClaudeChat:
    cfg = _config(workspace_id)
    if not cfg.anthropic_key:
        raise AIDisabled(f"workspace {workspace_id} missing anthropic_key")
    return ClaudeChat(api_key=cfg.anthropic_key)


def get_embed(workspace_id) -> OpenAIEmbed:
    cfg = _config(workspace_id)
    if not cfg.openai_key:
        raise AIDisabled(f"workspace {workspace_id} missing openai_key")
    return OpenAIEmbed(api_key=cfg.openai_key, model=cfg.embed_model or EMBED_MODEL)

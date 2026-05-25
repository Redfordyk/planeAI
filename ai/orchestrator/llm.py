"""Thin LLM helper for orchestrator agents.

Wraps the DeepSeek-compatible OpenAI client (the same one
``ai.agent_loop`` uses) with a single ``ask_json`` call: send a
system + user prompt, expect a JSON response back, parse + return.

We use this for agents that don't need tool-use (PLANNER's plan
generation, ANALYST's insight write-up, COMMUNICATOR's status text).
Tool-using agents reuse ``ai.agent_loop.run_agent`` directly.

Token accounting goes through ``ai.usage.record_usage`` so the
month-budget gate works the same as everywhere else.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import openai

from ai.models import AIUsageLog, WorkspaceAIConfig
from ai.providers import CHAT_MODEL, CHEAP_MODEL


def _resolve_model(cfg, cheap: bool) -> str:
    """Pick chat model. When the workspace is configured for DeepSeek
    (chat_model starts with 'deepseek-'), ignore the `cheap` flag —
    Anthropic Haiku doesn't exist on the DeepSeek endpoint. Bring
    your own cheap-model name on DeepSeek by setting chat_model to
    deepseek-v4-flash directly."""
    base = cfg.chat_model or CHAT_MODEL
    if base.startswith("deepseek-"):
        return base
    return base if not cheap else CHEAP_MODEL
from ai.usage import record_usage


logger = logging.getLogger("plane.ai.orchestrator.llm")


def _make_client(cfg: WorkspaceAIConfig):
    """Same shim as ai.agent_loop — DeepSeek is OpenAI-compatible."""
    base_url = "https://api.deepseek.com/v1"
    http = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    return openai.OpenAI(
        api_key=cfg.anthropic_key,  # field reused for chat key
        base_url=base_url,
        max_retries=0,
        http_client=http,
    )


def ask_json(
    *,
    workspace_id,
    cfg: WorkspaceAIConfig,
    system: str,
    user: str,
    cheap: bool = False,
    user_id=None,
    feature: str = AIUsageLog.FEATURE_AGENT,
    max_tokens: int = 1500,
) -> dict[str, Any]:
    """Send system + user, parse JSON reply. Returns ``{}`` on bad JSON.

    The system prompt MUST end with an explicit "respond with JSON
    only" instruction — DeepSeek follows it well, but the parser
    here is forgiving (strips ```json fences, falls back to ``{}``).
    """
    client = _make_client(cfg)
    model = _resolve_model(cfg, cheap)
    # DeepSeek supports OpenAI's response_format JSON-only mode —
    # forces well-formed JSON output, eliminates "Here is the JSON:"
    # prefixes that we had to strip. Wrap in try/except for providers
    # that don't support the flag (we fall back to plain text + our
    # forgiving parser).
    create_kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
    )
    try:
        resp = client.chat.completions.create(
            response_format={"type": "json_object"}, **create_kwargs
        )
    except Exception as exc:
        logger.info("ask_json: response_format not supported, retrying plain: %s", exc)
        resp = client.chat.completions.create(**create_kwargs)
    msg = resp.choices[0].message
    usage = getattr(resp, "usage", None)
    if usage is not None:
        record_usage(
            workspace_id=workspace_id,
            user_id=user_id,
            feature=feature,
            model=model,
            usage={
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
            },
        )
    text = (msg.content or "").strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    # Find first { or [ — DeepSeek sometimes prefixes "Here is the JSON:"
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx >= 0:
            text = text[idx:]
            break
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("ask_json: bad JSON from LLM: %s (text head: %r)", e, text[:200])
        return {}


def ask_text(
    *,
    workspace_id,
    cfg: WorkspaceAIConfig,
    system: str,
    user: str,
    cheap: bool = True,
    user_id=None,
    feature: str = AIUsageLog.FEATURE_AGENT,
    max_tokens: int = 800,
) -> str:
    """Send + return plain text (no JSON parsing). Used by COMMUNICATOR
    for the weekly status markdown."""
    client = _make_client(cfg)
    model = _resolve_model(cfg, cheap)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    usage = getattr(resp, "usage", None)
    if usage is not None:
        record_usage(
            workspace_id=workspace_id,
            user_id=user_id,
            feature=feature,
            model=model,
            usage={
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
            },
        )
    return (resp.choices[0].message.content or "").strip()

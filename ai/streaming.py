"""SSE streaming of Claude responses (ASGI path).

Plane runs on gunicorn + uvicorn.workers.UvicornWorker (see
STREAMING.md), so we use the **async** SSE variant:

  - ``anthropic.AsyncAnthropic`` and ``async with .messages.stream()``.
  - Any ORM call from the generator goes through
    ``asgiref.sync.sync_to_async``. Calling ORM directly from an
    async coroutine raises ``SynchronousOnlyOperation``.
  - Retrieval (which is sync ORM) is awaited via ``sync_to_async``
    BEFORE the stream starts — never from inside the generator.

Frame contract (the frontend in TZ 2.4 depends on this verbatim):

  data: {"sources": [...]}      # always first
  data: {"delta": "text"}       # 0..N
  data: {"error": "..."}        # optional, instead of done
  data: {"done": true, "usage": {...}}   # final on success

The `sources` first-frame is non-negotiable — the UI hook waits for
it before mounting the response panel. Forgetting it desynchronises
the client.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import anthropic
from asgiref.sync import sync_to_async

from ai import providers
from ai.models import AIUsageLog
from ai.usage import record_usage


logger = logging.getLogger("plane.ai.streaming")


async def _deepseek_stream(
    *,
    cfg,
    system: str,
    messages: list[dict],
    workspace_id,
    user_id,
    feature: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> AsyncIterator[str]:
    """SSE frames sourced from DeepSeek's OpenAI-compatible streaming
    chat completion. Same frame contract as Anthropic path.

    Anthropic and OpenAI message shapes differ slightly: Claude expects
    content as a list of blocks, OpenAI as a string. The retrieval path
    builds messages in Claude format; here we flatten to OpenAI.
    """
    import httpx as _httpx_mod
    import openai as _openai

    def _flatten(msg):
        c = msg.get("content")
        if isinstance(c, str):
            return {"role": msg["role"], "content": c}
        if isinstance(c, list):
            parts = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
                elif isinstance(blk, str):
                    parts.append(blk)
            return {"role": msg["role"], "content": "".join(parts)}
        return {"role": msg["role"], "content": str(c or "")}

    chat_messages = [{"role": "system", "content": system}] + [_flatten(m) for m in messages]

    http = _httpx_mod.AsyncClient(timeout=_httpx_mod.Timeout(60.0, connect=10.0))
    client = _openai.AsyncOpenAI(
        api_key=cfg.anthropic_key,  # cfg slot reused for chat key
        base_url="https://api.deepseek.com/v1",
        max_retries=0,
        http_client=http,
    )

    in_tokens = 0
    out_tokens = 0
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=chat_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                in_tokens = chunk.usage.prompt_tokens or 0
                out_tokens = chunk.usage.completion_tokens or 0
            for choice in chunk.choices or []:
                delta = getattr(choice, "delta", None)
                if delta and getattr(delta, "content", None):
                    yield _sse({"delta": delta.content})
    except _openai.AuthenticationError:
        logger.warning("deepseek_stream: auth error (ws=%s)", workspace_id)
        yield _sse({"error": "Неверный ключ — проверьте настройки воркспейса"})
        return
    except _openai.RateLimitError:
        yield _sse({"error": "Лимит запросов исчерпан"})
        return
    except Exception as exc:  # noqa
        logger.exception("deepseek_stream failed ws=%s", workspace_id)
        yield _sse({"error": f"{type(exc).__name__}: {exc}"})
        return
    finally:
        try:
            await http.aclose()
        except Exception:
            pass

    if in_tokens or out_tokens:
        await sync_to_async(record_usage)(
            workspace_id=workspace_id,
            user_id=user_id,
            feature=feature,
            model=model,
            usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
        )
    yield _sse({"done": True, "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens}})


def _sse(event: dict) -> str:
    """Format one SSE frame. The double newline terminator is part of
    the protocol — without it, the browser EventSource never emits the
    event."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def claude_sse(
    *,
    cfg,
    system: str,
    messages: list[dict],
    sources: list[dict],
    workspace_id,
    user_id,
    feature: str = AIUsageLog.FEATURE_INTENT_SEARCH,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> AsyncIterator[str]:
    """Async generator that yields SSE frames.

    Caller wraps this with ``StreamingHttpResponse(claude_sse(...),
    content_type='text/event-stream')`` and sets
    ``X-Accel-Buffering: no``.
    """
    # 1. sources frame — must be first.
    yield _sse({"sources": sources})

    chosen_model = model or (cfg.chat_model if cfg else providers.CHAT_MODEL)

    # Route DeepSeek-named models via the OpenAI-compatible streaming
    # endpoint at api.deepseek.com — they speak the OpenAI protocol,
    # not Anthropic's. Falls back to Anthropic for everything else.
    if chosen_model.startswith("deepseek-"):
        async for frame in _deepseek_stream(
            cfg=cfg, system=system, messages=messages,
            workspace_id=workspace_id, user_id=user_id, feature=feature,
            model=chosen_model, max_tokens=max_tokens, temperature=temperature,
        ):
            yield frame
        return

    # anthropic SDK + httpx>=0.28 mismatch on the `proxies` kwarg —
    # construct our own async httpx client so the SDK doesn't try to
    # pass it.
    import httpx as _httpx_mod
    _http = _httpx_mod.AsyncClient(timeout=_httpx_mod.Timeout(60.0, connect=10.0))
    client = anthropic.AsyncAnthropic(
        api_key=cfg.anthropic_key, max_retries=0, http_client=_http
    )

    final_usage = None
    try:
        async with client.messages.stream(
            model=chosen_model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            temperature=temperature,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    yield _sse({"delta": text})
            final = await stream.get_final_message()
            final_usage = final.usage
    except anthropic.AuthenticationError:
        logger.warning("claude_sse: anthropic auth error (ws=%s)", workspace_id)
        yield _sse({"error": "Неверный ключ Claude — проверьте настройки воркспейса"})
        return
    except anthropic.RateLimitError:
        logger.warning("claude_sse: anthropic rate limit (ws=%s)", workspace_id)
        yield _sse({"error": "Превышен лимит запросов к Claude, попробуйте позже"})
        return
    except anthropic.BadRequestError as e:
        logger.warning("claude_sse: anthropic bad request: %s", e)
        yield _sse({"error": "Некорректный запрос к ИИ"})
        return
    except anthropic.APIError as e:
        logger.error("claude_sse: anthropic API error: %s", type(e).__name__)
        yield _sse({"error": "Ошибка ИИ-провайдера, попробуйте ещё раз"})
        return

    # 2. record usage via sync_to_async — ORM write from async ctx.
    cost = None
    if final_usage is not None:
        try:
            cost = await sync_to_async(record_usage, thread_sensitive=False)(
                workspace_id=workspace_id,
                user_id=user_id,
                feature=feature,
                model=chosen_model,
                usage=final_usage,
            )
        except Exception:
            # Accounting must never break the user-facing stream.
            logger.exception("claude_sse: record_usage failed (non-fatal)")

    # 3. done frame.
    yield _sse(
        {
            "done": True,
            "usage": {
                "model": chosen_model,
                "cost_usd": str(cost) if cost is not None else None,
            },
        }
    )


def sse_response_headers() -> dict[str, str]:
    """Headers that must accompany an SSE StreamingHttpResponse.

    The two non-obvious ones:
      - ``Cache-Control: no-cache`` — without it, some proxies cache
        the first chunk and replay it for subsequent clients.
      - ``X-Accel-Buffering: no`` — nginx buffers responses by
        default; this header asks nginx to forward bytes as they
        arrive. Caddy (Plane's default proxy) also honours it.
    """
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        # Connection: keep-alive is implicit for HTTP/1.1; spelling it
        # out helps debugging on the curl side.
        "Connection": "keep-alive",
    }

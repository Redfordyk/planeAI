"""Patch ai.providers.OpenAIEmbed so it survives openai==1.54 +
httpx>=0.28 (the SDK passes a  kwarg that the new httpx
removed). Same trick as DeepSeekChat — own httpx.Client handed in.

Also makes the patched class behave identically; nothing else changes
in providers.OpenAIEmbed surface.
"""
import logging
import httpx
import openai
from ai import providers


logger = logging.getLogger("plane.ai.openai_embed_fix")


class _PatchedOpenAIEmbed(providers.OpenAIEmbed):
    def __init__(self, api_key, model=providers.EMBED_MODEL):
        http_client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        # Skip the parent __init__ since it would re-create a client
        # via the buggy code path; build directly here.
        self.client = openai.OpenAI(
            api_key=api_key, max_retries=0, http_client=http_client
        )
        self.model = model


def install():
    if getattr(providers.OpenAIEmbed, "_planeai_patched", False):
        return
    _PatchedOpenAIEmbed._planeai_patched = True
    providers.OpenAIEmbed = _PatchedOpenAIEmbed
    logger.info("OpenAIEmbed patched (httpx-compat shim)")

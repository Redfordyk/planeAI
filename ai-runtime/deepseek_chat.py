"""DeepSeek chat router. Uses a hand-built httpx.Client to dodge the
openai==1.54 + httpx>=0.28 mismatch (the SDK passes a 
kwarg the new httpx removed)."""
import logging
import httpx
import openai
from ai import providers

logger = logging.getLogger("plane.ai.deepseek")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def _make_openai_client(api_key: str, base_url: str):
    # Build our own httpx.Client and hand it to openai.OpenAI so the
    # SDK does not call the new httpx.Client(proxies=...) ctor.
    http_client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    return openai.OpenAI(
        api_key=api_key, base_url=base_url, max_retries=0, http_client=http_client
    )


class DeepSeekChat:
    def __init__(self, api_key, *, base_url=DEEPSEEK_BASE_URL):
        self.client = _make_openai_client(api_key, base_url)

    def complete(self, *, system, messages, tools=None, model="deepseek-v4-flash",
                 max_tokens=2048, temperature=0.1):
        openai_messages = [{"role": "system", "content": system}, *messages]
        last_exc = None
        for attempt in range(providers.MAX_RETRIES):
            try:
                resp = self.client.chat.completions.create(
                    model=model, messages=openai_messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
                text = resp.choices[0].message.content if resp.choices else ""
                usage = type("Usage", (), {
                    "input_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                    "output_tokens": resp.usage.completion_tokens if resp.usage else 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                })()
                content = [type("Block", (), {"type": "text", "text": text})()]
                return type("Response", (), {"content": content, "usage": usage})()
            except openai.RateLimitError as e:
                last_exc = e
                logger.warning("deepseek 429 attempt %d", attempt + 1)
                if attempt == providers.MAX_RETRIES - 1: raise
                providers._sleep_backoff(attempt, jitter=True)
            except openai.APIError as e:
                last_exc = e
                logger.warning("deepseek %s attempt %d", type(e).__name__, attempt + 1)
                if attempt == providers.MAX_RETRIES - 1: raise
                providers._sleep_backoff(attempt, jitter=False)
        raise RuntimeError("DeepSeekChat exhausted retries") from last_exc


def _patched_get_chat(workspace_id):
    from ai.models import WorkspaceAIConfig
    cfg = (WorkspaceAIConfig.objects
        .filter(workspace_id=workspace_id, enabled=True)
        .only("anthropic_key", "chat_model").first())
    if cfg is None:
        raise providers.AIDisabled(f"workspace {workspace_id} has no enabled AI config")
    if not cfg.anthropic_key:
        raise providers.AIDisabled(f"workspace {workspace_id} missing chat key")
    if (cfg.chat_model or "").startswith("deepseek-"):
        return DeepSeekChat(api_key=cfg.anthropic_key)
    return providers.ClaudeChat(api_key=cfg.anthropic_key)


def install():
    if getattr(providers.get_chat, "_planeai_patched", False):
        return
    _patched_get_chat._planeai_patched = True
    providers.get_chat = _patched_get_chat
    logger.info("DeepSeek chat router installed")

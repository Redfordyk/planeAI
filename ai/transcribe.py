"""Voice transcription via OpenAI Whisper.

DeepSeek has no STT product (verified against /v1/models on 2026-05-23),
so we fall back to OpenAI's Whisper for the speech path. Cost is
$0.006 per audio minute — a 30-second voice note is ~$0.003.

Accepts any audio format that Whisper does (webm, mp3, mp4, mpeg,
mpga, m4a, wav). Browser MediaRecorder emits webm/opus by default,
which Whisper handles natively.
"""

from __future__ import annotations

import io
import logging

import httpx
import openai


logger = logging.getLogger("plane.ai.transcribe")


def transcribe_audio(api_key: str, *, audio_bytes: bytes, filename: str = "audio.webm",
                     language: str | None = "ru") -> str:
    """Call Whisper, return the transcript text. May raise
    ``openai.APIError`` / ``RateLimitError`` — caller decides how to
    surface those."""
    http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
    client = openai.OpenAI(api_key=api_key, max_retries=2, http_client=http)

    # openai-python's `.audio.transcriptions.create` accepts a tuple
    # (filename, file-like, content_type) so it can name the upload.
    buf = io.BytesIO(audio_bytes)
    buf.name = filename
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=buf,
        language=language,
        # `text` -> plain string, no JSON unwrap on our side
        response_format="text",
    )
    if isinstance(response, str):
        return response.strip()
    # SDK sometimes returns object with `.text`
    return (getattr(response, "text", "") or "").strip()

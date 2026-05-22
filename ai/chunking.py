"""Token-based chunker for the RAG indexer.

We slice source text on token boundaries (not characters or words) so
each chunk fits comfortably inside the embedding model's context
(8191 tokens for text-embedding-3-small). Overlapping windows give
retrieval continuity: a query that lands near a chunk boundary still
matches via the neighbouring chunk.

Pinned to OpenAI's `cl100k_base` tokenizer because that's what the
text-embedding-3-small model uses. If we ever switch embed models, we
must reconsider this — different tokenizers produce different token
counts and different boundaries.
"""

from __future__ import annotations

import tiktoken


# Safety upper bound. The embedding model's hard limit is 8191; we
# never want a chunk near that line.
MAX_CHUNK_TOKENS = 8000

# Cached at import time — `get_encoding` does I/O on first call.
_ENC = tiktoken.get_encoding("cl100k_base")


def chunk_text(
    text: str,
    *,
    target_tokens: int = 400,
    overlap: int = 50,
) -> list[tuple[str, int]]:
    """Return `(chunk_text, token_count)` pairs.

    Short inputs return a single chunk; longer ones produce
    overlapping windows of size `target_tokens` with `overlap` tokens
    shared between consecutive chunks.

    Defaults: 400 tokens per chunk (~300 words; small enough that
    several chunks fit in a Claude context for top-k retrieval),
    50-token overlap (~12% — enough to bridge mid-sentence cuts).
    """
    assert target_tokens < MAX_CHUNK_TOKENS, "target_tokens must stay under MAX_CHUNK_TOKENS"
    assert 0 <= overlap < target_tokens, "overlap must be < target_tokens"

    if not text:
        return []

    toks = _ENC.encode(text)
    if len(toks) == 0:
        return []
    if len(toks) <= target_tokens:
        return [(text, len(toks))]

    chunks: list[tuple[str, int]] = []
    start = 0
    stride = target_tokens - overlap
    while start < len(toks):
        window = toks[start : start + target_tokens]
        chunks.append((_ENC.decode(window), len(window)))
        if start + target_tokens >= len(toks):
            break
        start += stride
    return chunks


def count_tokens(text: str) -> int:
    """Length in `cl100k_base` tokens. Used by accounting paths that
    need a token count without actually chunking."""
    return len(_ENC.encode(text or ""))

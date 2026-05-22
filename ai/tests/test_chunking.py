"""Pure-Python tests for ai.chunking. No Django, no DB."""

from __future__ import annotations

import pytest

from ai.chunking import MAX_CHUNK_TOKENS, chunk_text, count_tokens


def test_empty_text_returns_empty_list():
    assert chunk_text("") == []


def test_short_text_returns_single_chunk():
    chunks = chunk_text("hello world")
    assert len(chunks) == 1
    text, n_tokens = chunks[0]
    assert text == "hello world"
    assert n_tokens == count_tokens("hello world")


def test_long_text_splits_into_overlapping_windows():
    # ~1500 tokens of repeated tokens. With target=400 / overlap=50,
    # stride = 350, so windows start at 0, 350, 700, 1050, 1400 ->
    # 4 full windows + a tail window.
    text = ("word " * 1500).strip()  # ~1500 BPE tokens-ish
    chunks = chunk_text(text, target_tokens=400, overlap=50)
    # 1500/350 ≈ 4.3 -> at least 4 full chunks, plus one tail.
    assert len(chunks) >= 4
    # All chunks (except possibly the last) are exactly target_tokens.
    for i, (_, n) in enumerate(chunks[:-1]):
        assert n == 400, f"chunk {i} has {n} tokens (expected 400)"
    # Last chunk is <= target_tokens.
    assert chunks[-1][1] <= 400


def test_overlap_actually_overlaps():
    text = ("alpha beta gamma " * 500).strip()
    chunks = chunk_text(text, target_tokens=100, overlap=20)
    assert len(chunks) >= 2
    # The decoded text of chunk 1 ends with the decoded text of chunk 2's
    # start — exact byte-level overlap depends on tokenizer boundaries,
    # so the cleanest invariant is: total token coverage across chunks
    # exceeds the unique token count, by overlap * (n_chunks - 1).
    total_tokens = sum(n for _, n in chunks)
    unique_tokens = count_tokens(text)
    assert total_tokens >= unique_tokens
    # Overlap budget upper bound: (n_chunks - 1) * overlap. Lower bound:
    # at least one overlap interval exists.
    assert total_tokens - unique_tokens >= 20


def test_target_tokens_must_be_under_safety_limit():
    with pytest.raises(AssertionError):
        chunk_text("x", target_tokens=MAX_CHUNK_TOKENS)


def test_overlap_must_be_smaller_than_target():
    with pytest.raises(AssertionError):
        chunk_text("x", target_tokens=100, overlap=100)


def test_count_tokens_handles_none_safely():
    # `count_tokens(None)` should not raise — we coerce to "".
    assert count_tokens(None) == 0
    assert count_tokens("") == 0
    assert count_tokens("hi") > 0

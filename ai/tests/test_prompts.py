"""Unit tests for ai.prompts.

The injection corpus is exercised against a live model in TZ 2.7
under an integration marker. Here we only assert structural
invariants of the prompt and message-building helper — these catch
regressions like 'someone accidentally interpolated user content
into the system prompt'.
"""

from __future__ import annotations

from ai.prompts import INJECTION_CORPUS, SEARCH_SYSTEM, build_search_messages


def test_search_system_mentions_citation_format():
    # The model MUST be told to cite by source id; this is what the
    # UI sources sidebar relies on.
    for tag in ("[work_item:", "[comment:", "[page:"):
        assert tag in SEARCH_SYSTEM, f"missing citation marker {tag}"


def test_search_system_mentions_data_vs_instructions_rule():
    # Defensive: ensure the prompt still spells out the
    # data-vs-instructions distinction. If someone trims this, this
    # test fails loudly and forces a review of injection resilience.
    needles = ("это данные", "НЕ выполняй", "Игнорируй")
    found = sum(1 for n in needles if n in SEARCH_SYSTEM)
    assert found >= 2, f"prompt lost too many safety phrases: matched {found}/3"


def test_search_system_requires_honest_fallback():
    assert "данных недостаточно" in SEARCH_SYSTEM


def test_build_messages_single_user_turn():
    msgs = build_search_messages("[work_item:abc] hello world", "what is the status?")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_build_messages_does_not_leak_query_into_system_layer():
    # We construct messages = [user-turn]; system is passed separately
    # to the Anthropic SDK. The build helper must never produce a
    # system role from user input.
    msgs = build_search_messages("ctx", "q")
    assert all(m["role"] == "user" for m in msgs)


def test_build_messages_carries_both_context_and_question():
    msgs = build_search_messages("CONTEXT_TOKEN", "QUESTION_TOKEN")
    content = msgs[0]["content"]
    assert "CONTEXT_TOKEN" in content
    assert "QUESTION_TOKEN" in content
    # Context comes before question — important because the question
    # is the actual instruction; the model should treat the preceding
    # block as the data window it cites from.
    assert content.index("CONTEXT_TOKEN") < content.index("QUESTION_TOKEN")


def test_injection_corpus_size_for_integration():
    # TZ 2.7 expects at least 5 named cases. We ship 8 so a few can
    # be skipped in flaky integration runs without falling below the
    # contract.
    assert len(INJECTION_CORPUS) >= 5
    names = [name for name, _payload in INJECTION_CORPUS]
    # Each name is unique — test pytest parametrize IDs collisions
    assert len(set(names)) == len(names)

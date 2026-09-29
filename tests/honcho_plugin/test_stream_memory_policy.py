"""Tests for Hermes-side Honcho stream memory policy."""

from typing import Any, cast

from plugins.memory.honcho import HonchoMemoryProvider, STREAM_MEMORY_POLICY


def test_system_prompt_block_includes_stream_taxonomy_policy() -> None:
    provider = HonchoMemoryProvider()
    provider._manager = cast(Any, True)
    provider._session_key = "test-session"
    provider._recall_mode = "hybrid"

    block = provider.system_prompt_block()

    assert "stream taxonomy" in block
    assert "insights, decisions, failures, references, notable context" in block
    assert "Do not treat assistant narration as durable fact" in block


def test_dialectic_prompt_applies_stream_policy_to_active_work() -> None:
    provider = HonchoMemoryProvider()

    prompt = provider._build_dialectic_prompt(pass_idx=0, prior_results=[], is_cold=False)

    assert STREAM_MEMORY_POLICY in prompt
    assert "stream-worthy active context" in prompt
    assert "Only include active work" in prompt


def test_stream_memory_context_filter_drops_self_history_and_keeps_decisions() -> None:
    text = "\n".join(
        [
            "## User Representation",
            "Hermes said it would check the queue status.",
            "Decision: Source of truth is Kanban plus vault notes.",
            "The assistant recommended a cute playful memory style.",
            "Failure: GLM 5.1 produced malformed JSON in the deriver path.",
        ]
    )

    filtered = HonchoMemoryProvider._filter_stream_memory_context(text)

    assert "Hermes said" not in filtered
    assert "cute playful" not in filtered
    assert "Decision: Source of truth is Kanban plus vault notes." in filtered
    assert "Failure: GLM 5.1 produced malformed JSON" in filtered

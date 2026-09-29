"""Tests for Honcho message-level reasoning policy."""

from types import SimpleNamespace
from typing import Any, cast

from plugins.memory.honcho.session import HonchoSession, HonchoSessionManager


class _FakePeer:
    def __init__(self, peer_id: str):
        self.peer_id = peer_id
        self.messages = []

    def message(self, content, **kwargs):
        msg = {"peer_id": self.peer_id, "content": content, **kwargs}
        self.messages.append(msg)
        return msg


class _FakeHonchoSession:
    def __init__(self):
        self.batches = []

    def add_messages(self, messages):
        self.batches.append(messages)


def _manager_with_unsynced_messages():
    cfg = SimpleNamespace(
        write_frequency="turn",
        dialectic_reasoning_level="low",
        dialectic_dynamic=True,
        dialectic_max_chars=600,
        observation_mode="unified",
        user_observe_me=True,
        user_observe_others=False,
        ai_observe_me=False,
        ai_observe_others=True,
        message_max_chars=25000,
        dialectic_max_input_chars=10000,
    )
    mgr = HonchoSessionManager(honcho=cast(Any, SimpleNamespace()), config=cfg)
    user_peer = _FakePeer("szab")
    assistant_peer = _FakePeer("hermes")
    fake_session = _FakeHonchoSession()
    session = HonchoSession(
        key="test-session",
        user_peer_id="szab",
        assistant_peer_id="hermes",
        honcho_session_id="test-session",
        messages=[
            {"role": "user", "content": "Decision: use the stream taxonomy."},
            {"role": "assistant", "content": "Hermes verified the queue was empty."},
        ],
    )
    mgr._cache[session.key] = session
    mgr._peers_cache["szab"] = user_peer
    mgr._peers_cache["hermes"] = assistant_peer
    mgr._sessions_cache[session.honcho_session_id] = fake_session
    return mgr, fake_session


def test_assistant_messages_are_saved_but_excluded_from_reasoning():
    mgr, fake_session = _manager_with_unsynced_messages()

    assert mgr._flush_session(mgr._cache["test-session"])

    user_msg, assistant_msg = fake_session.batches[0]
    assert user_msg["configuration"] == {"reasoning": {"enabled": True}}
    assert assistant_msg["configuration"] == {"reasoning": {"enabled": False}}
    assert assistant_msg["metadata"]["stream_memory_policy"] == "assistant_self_history_suppressed"


def test_context_compaction_user_messages_are_excluded_from_reasoning():
    mgr, fake_session = _manager_with_unsynced_messages()
    session = mgr._cache["test-session"]
    session.messages = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Previous turns...",
        }
    ]

    assert mgr._flush_session(session)

    compacted_msg = fake_session.batches[0][0]
    assert compacted_msg["configuration"] == {"reasoning": {"enabled": False}}
    assert compacted_msg["metadata"]["stream_memory_policy"] == "transient_context_suppressed"

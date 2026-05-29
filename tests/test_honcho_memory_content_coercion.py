from typing import Any, cast

from plugins.memory.honcho import HonchoMemoryProvider, _coerce_memory_content
from plugins.memory.honcho.client import HonchoClientConfig


class _FakeSession:
    def __init__(self):
        self.messages = []

    def add_message(self, role, content):
        self.messages.append((role, content))


class _FakeManager:
    def __init__(self):
        self.session = _FakeSession()
        self.flushed = False

    def get_or_create(self, key):
        assert key == "test-session"
        return self.session

    def _flush_session(self, session):
        assert session is self.session
        self.flushed = True
        return True


def _provider_with_fake_manager():
    provider = HonchoMemoryProvider()
    provider._cron_skipped = False
    manager = _FakeManager()
    provider._manager = cast(Any, manager)
    provider._session_key = "test-session"
    provider._config = HonchoClientConfig(message_max_chars=25000)
    provider._sync_thread = None
    return provider, manager


def test_coerce_memory_content_flattens_responses_content_parts():
    payload = [
        {"type": "text", "text": "first"},
        {"type": "output_text", "content": [{"text": "second"}]},
        "third",
    ]

    assert _coerce_memory_content(payload) == "first\nsecond\nthird"


def test_sync_turn_accepts_structured_content_parts():
    provider, manager = _provider_with_fake_manager()

    provider.sync_turn(
        cast(Any, [{"type": "input_text", "text": "student question"}]),
        cast(Any, [{"type": "output_text", "text": "tutor answer"}]),
    )
    assert provider._sync_thread is not None
    provider._sync_thread.join(timeout=2)

    assert manager.flushed is True
    assert manager.session.messages == [
        ("user", "student question"),
        ("assistant", "tutor answer"),
    ]

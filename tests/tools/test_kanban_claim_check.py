"""The claim decision is a worker-only native tool, never a terminal identity grant."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "session-memory")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "195")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)


def test_native_claim_calls_gate_in_worker_context_only(monkeypatch, worker):
    from tools import kanban_tools as kt
    calls = []
    gate = SimpleNamespace(check_claim=lambda *args: calls.append(args) or {"action": "fresh"},
                           ClaimBlocked=type("ClaimBlocked", (Exception,), {}))
    monkeypatch.setattr(kt, "_load_claim_gate", lambda: gate)
    args = {"run_folder": "/runs/SMEM-0062", "repo_path": "/repo",
            "canonical_issue": "/vault/SMEM-0062/issue.md"}
    response = json.loads(kt._handle_claim_check(args))
    assert response["action"] == "fresh"
    assert calls == [("session-memory", "t_owned", "/runs/SMEM-0062", "/repo", "/vault/SMEM-0062/issue.md")]
    from tools.registry import registry
    assert registry.get_schema("kanban_claim_check")["name"] == "kanban_claim_check"
    assert kt._check_kanban_mode()


@pytest.mark.parametrize("mode", ["child", "cron", "fenced", "missing", "unnumbered"])
def test_native_claim_rejects_non_owner_before_loading_gate(monkeypatch, worker, mode):
    from tools import kanban_tools as kt
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    from contextlib import nullcontext
    monkeypatch.setattr(kt, "_load_claim_gate", lambda: pytest.fail("gate loaded without ownership"))
    context = delegated_child_context() if mode == "child" else (
        non_dispatcher_owned_context() if mode == "cron" else nullcontext())
    if mode == "fenced":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    if mode == "missing":
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    if mode == "unnumbered":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "bad")
    with context:
        result = json.loads(kt._handle_claim_check({
            "run_folder": "/runs/SMEM-0062", "repo_path": "/repo",
            "canonical_issue": "/vault/SMEM-0062/issue.md"}))
    assert result.get("error") or result.get("action") == "blocked"

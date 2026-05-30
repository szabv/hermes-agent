"""Tests for the bundled observability/openinference plugin.

Pure-mock and hermetic: no network, no real OpenTelemetry SDK import (so the
suite runs under any venv, including .venv which lacks the OTel packages). We
assert against **literal OpenInference key strings** — this also pins the wire
contract independent of the openinference-semantic-conventions package.
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "observability" / "openinference"
MOD_NAME = "plugins.observability.openinference"


# ---------------------------------------------------------------------------
# Fakes (stand in for the OTel tracer/span/exporter without importing OTel).
# ---------------------------------------------------------------------------
class FakeStatus:
    def __init__(self, code, description=None):
        self.code = code
        self.description = description


class FakeStatusCode:
    OK = "OK"
    ERROR = "ERROR"


class FakeSpan:
    def __init__(self, name, attributes=None, start_context=None):
        self.name = name
        self.attributes = dict(attributes or {})
        self.start_context = start_context
        self.status = None
        self.ended = False
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def record_exception(self, exc):
        self.exceptions.append(exc)

    def end(self):
        self.ended = True


class FakeTracer:
    def __init__(self):
        self.spans = []
        self.start_calls = []

    def start_span(self, name, context=None, attributes=None):
        span = FakeSpan(name, attributes=attributes, start_context=context)
        self.spans.append(span)
        self.start_calls.append((name, context, attributes))
        return span


class FakeOtelTrace:
    """Records the parent handed to set_span_in_context for parenting asserts."""

    @staticmethod
    def set_span_in_context(span):
        return {"__parent__": span}


def _install_fake_tracer(mod):
    """Wire a FakeTracer + fake OTel symbols into the module under test."""
    tracer = FakeTracer()
    mod._TRACER = tracer
    mod._get_tracer = lambda: tracer  # type: ignore[assignment]
    mod._otel_trace = FakeOtelTrace
    mod.Status = FakeStatus
    mod.StatusCode = FakeStatusCode
    mod._flush = lambda: None  # type: ignore[assignment]
    return tracer


def _fresh_plugin():
    sys.modules.pop(MOD_NAME, None)
    mod = importlib.import_module(MOD_NAME)
    mod._TRACE_STATE.clear()
    mod._TRACER = None
    mod._TRACER_PROVIDER = None
    return mod


def _clear_otel_env(monkeypatch):
    for k in (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SERVICE_NAME",
        "HERMES_OPENINFERENCE_MAX_ATTR_CHARS",
        "HERMES_OPENINFERENCE_DEBUG",
        "HERMES_PROFILE",
    ):
        monkeypatch.delenv(k, raising=False)


def _parent_of(span):
    ctx = span.start_context
    return ctx.get("__parent__") if isinstance(ctx, dict) else None


# ---------------------------------------------------------------------------
# 1. Manifest + layout
# ---------------------------------------------------------------------------
class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()
        assert (PLUGIN_DIR / "README.md").exists()

    def test_manifest_fields(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert data["name"] == "openinference"
        assert data["version"]
        assert set(data["hooks"]) == {
            "pre_api_request", "post_api_request",
            "pre_llm_call", "post_llm_call",
            "pre_tool_call", "post_tool_call",
            "on_session_finalize", "on_session_reset",
        }


# ---------------------------------------------------------------------------
# 2. Discovery: opt-in, not loaded by default.
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_plugin_is_discovered_as_standalone_opt_in(self, tmp_path, monkeypatch):
        from hermes_cli import plugins as plugins_mod

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        manager = plugins_mod.PluginManager()
        manager.discover_and_load()

        plugins = {plugin["key"]: plugin for plugin in manager.list_plugins()}
        loaded = plugins.get("observability/openinference")
        assert loaded is not None, "plugin not discovered"
        assert loaded["enabled"] is False
        assert "not enabled" in (loaded["error"] or "").lower()


# ---------------------------------------------------------------------------
# 3. Runtime gate (no endpoint / missing SDK / bad build).
# ---------------------------------------------------------------------------
class TestRuntimeGate:
    def test_no_endpoint_is_inert(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        # Pretend the SDK is present so only the endpoint gate decides.
        monkeypatch.setattr(mod, "_otel_trace", FakeOtelTrace, raising=False)
        monkeypatch.setattr(mod, "TracerProvider", object, raising=False)
        monkeypatch.setattr(mod, "Resource", object, raising=False)
        assert mod._get_tracer() is None
        assert mod._TRACER is mod._INIT_FAILED

    def test_missing_sdk_is_inert(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        mod = _fresh_plugin()
        monkeypatch.setattr(mod, "_otel_trace", None, raising=False)
        assert mod._get_tracer() is None
        assert mod._TRACER is mod._INIT_FAILED

    def test_bad_build_warns_once_and_caches(self, monkeypatch, caplog):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        mod = _fresh_plugin()
        monkeypatch.setattr(mod, "_otel_trace", FakeOtelTrace, raising=False)
        monkeypatch.setattr(mod, "Resource", type("R", (), {"create": staticmethod(lambda d: d)}), raising=False)
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: object(), raising=False)

        def boom(*a, **k):
            raise RuntimeError("provider build failed")

        monkeypatch.setattr(mod, "TracerProvider", boom, raising=False)
        with caplog.at_level(logging.WARNING, logger=MOD_NAME):
            for _ in range(10):
                assert mod._get_tracer() is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == MOD_NAME]
        assert len(warnings) == 1
        assert mod._TRACER is mod._INIT_FAILED


# ---------------------------------------------------------------------------
# 4. Hooks are inert when the tracer is unavailable.
# ---------------------------------------------------------------------------
class TestHooksInert:
    def test_hooks_noop_without_tracer(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        monkeypatch.setattr(mod, "_get_tracer", lambda: None)
        mod.on_pre_llm_call(task_id="t", session_id="s", messages=[{"role": "user", "content": "hi"}])
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1, request_messages=[])
        mod.on_post_api_request(task_id="t", session_id="s", api_call_count=1)
        mod.on_pre_tool_call(tool_name="read_file", args={}, task_id="t", session_id="s")
        mod.on_post_tool_call(tool_name="read_file", args={}, result="ok", task_id="t", session_id="s")
        mod.on_session_finalize(session_id="s")
        mod.on_session_reset(session_id="s")
        assert mod._TRACE_STATE == {}


# ---------------------------------------------------------------------------
# 5. Provider/exporter setup (protocol selection, resource, flush/shutdown).
# ---------------------------------------------------------------------------
class TestProviderSetup:
    def _wire_real_get_tracer(self, mod, monkeypatch):
        created = {}

        class FakeProvider:
            def __init__(self, resource=None):
                self.resource = resource
                self.processors = []
                created["provider"] = self

            def add_span_processor(self, p):
                self.processors.append(p)

            def get_tracer(self, *a, **k):
                return FakeTracer()

            def force_flush(self):
                created["flushed"] = True

            def shutdown(self):
                created["shutdown"] = True

        class FakeResource:
            @staticmethod
            def create(attrs):
                return {"resource_attrs": attrs}

        monkeypatch.setattr(mod, "_otel_trace", FakeOtelTrace, raising=False)
        monkeypatch.setattr(mod, "TracerProvider", FakeProvider, raising=False)
        monkeypatch.setattr(mod, "Resource", FakeResource, raising=False)
        monkeypatch.setattr(mod, "BatchSpanProcessor", lambda exp: ("bsp", exp), raising=False)
        return created

    def test_http_default_and_resource_service_name(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        mod = _fresh_plugin()
        created = self._wire_real_get_tracer(mod, monkeypatch)
        http = {"used": False}
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: http.__setitem__("used", True) or "http-exp", raising=False)
        monkeypatch.setattr(mod, "_OTLPGRPCSpanExporter", None, raising=False)

        tracer = mod._get_tracer()
        assert tracer is not None
        assert http["used"] is True
        assert created["provider"].resource == {"resource_attrs": {"service.name": "hermes-agent"}}
        # flush + shutdown are safe.
        mod._flush()
        mod._shutdown()
        assert created.get("flushed") and created.get("shutdown")

    def test_service_name_env_override(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "my-svc")
        mod = _fresh_plugin()
        created = self._wire_real_get_tracer(mod, monkeypatch)
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: "http-exp", raising=False)
        mod._get_tracer()
        assert created["provider"].resource == {"resource_attrs": {"service.name": "my-svc"}}

    def test_grpc_protocol_uses_grpc_when_available(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        mod = _fresh_plugin()
        self._wire_real_get_tracer(mod, monkeypatch)
        picked = {}
        monkeypatch.setattr(mod, "_OTLPGRPCSpanExporter", lambda: picked.setdefault("grpc", True) or "grpc-exp", raising=False)
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: picked.setdefault("http", True) or "http-exp", raising=False)
        mod._get_tracer()
        assert picked.get("grpc") and "http" not in picked

    def test_grpc_falls_back_to_http_when_missing(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
        mod = _fresh_plugin()
        self._wire_real_get_tracer(mod, monkeypatch)
        picked = {}
        monkeypatch.setattr(mod, "_OTLPGRPCSpanExporter", None, raising=False)
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: picked.setdefault("http", True) or "http-exp", raising=False)
        mod._get_tracer()
        assert picked.get("http")

    def test_concurrent_first_call_builds_one_provider(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
        mod = _fresh_plugin()
        created = {"providers": 0, "tracers": []}

        class FakeProvider:
            def __init__(self, resource=None):
                self.resource = resource
                self.processors = []
                created["providers"] += 1
                time.sleep(0.01)

            def add_span_processor(self, p):
                self.processors.append(p)

            def get_tracer(self, *a, **k):
                tracer = FakeTracer()
                created["tracers"].append(tracer)
                return tracer

        class FakeResource:
            @staticmethod
            def create(attrs):
                return {"resource_attrs": attrs}

        monkeypatch.setattr(mod, "_otel_trace", FakeOtelTrace, raising=False)
        monkeypatch.setattr(mod, "TracerProvider", FakeProvider, raising=False)
        monkeypatch.setattr(mod, "Resource", FakeResource, raising=False)
        monkeypatch.setattr(mod, "BatchSpanProcessor", lambda exp: ("bsp", exp), raising=False)
        monkeypatch.setattr(mod, "_OTLPHTTPSpanExporter", lambda: "http-exp", raising=False)

        results = []
        result_lock = threading.Lock()

        def worker():
            tracer = mod._get_tracer()
            with result_lock:
                results.append(tracer)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert created["providers"] == 1
        assert len({id(result) for result in results}) == 1


# ---------------------------------------------------------------------------
# 6. Span shaping (literal OpenInference keys).
# ---------------------------------------------------------------------------
class TestSpanShaping:
    def test_agent_llm_tool_span_kinds_and_keys(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)

        mod.on_pre_api_request(
            task_id="task-1", session_id="sess-1", platform="cli",
            model="claude-3", provider="anthropic", api_mode="anthropic_messages",
            api_call_count=1, max_tokens=4096,
            request_messages=[{"role": "user", "content": "hello"}],
        )
        root = tracer.spans[0]
        llm = tracer.spans[1]
        assert root.attributes["openinference.span.kind"] == "AGENT"
        assert root.name == "hermes.agent.turn"
        assert root.attributes["session.id"] == "sess-1"
        assert llm.attributes["openinference.span.kind"] == "LLM"
        assert llm.attributes["llm.model_name"] == "claude-3"
        assert llm.attributes["llm.provider"] == "anthropic"
        assert llm.attributes["llm.input_messages.0.message.role"] == "user"
        assert llm.attributes["llm.input_messages.0.message.content"] == "hello"
        assert llm.attributes["input.value"] == '[{"role": "user", "content": "hello"}]'

        class _Msg:
            content = "hi there"
            tool_calls = None

        mod.on_post_api_request(
            task_id="task-1", session_id="sess-1", model="claude-3", provider="anthropic",
            api_call_count=1, finish_reason="stop",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                   "cache_read_tokens": 2, "reasoning_tokens": 1},
            assistant_message=_Msg(), assistant_content_chars=8, assistant_tool_call_count=0,
        )
        assert llm.attributes["llm.token_count.prompt"] == 10
        assert llm.attributes["llm.token_count.completion"] == 5
        assert llm.attributes["llm.token_count.total"] == 15
        assert llm.attributes["llm.token_count.prompt_details.cache_read"] == 2
        assert llm.attributes["llm.token_count.completion_details.reasoning"] == 1
        assert llm.attributes["llm.finish_reason"] == "stop"
        assert llm.attributes["llm.output_messages.0.message.role"] == "assistant"
        assert llm.attributes["llm.output_messages.0.message.content"] == "hi there"
        assert llm.ended is True
        # No tool calls + content ⇒ turn complete, root closed + state gone.
        assert mod._TRACE_STATE == {}
        assert root.ended is True

    def test_tool_span_keys(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        mod.on_pre_tool_call(tool_name="read_file", args={"path": "x.py"}, task_id="t", session_id="s")
        tool = tracer.spans[-1]
        assert tool.attributes["openinference.span.kind"] == "TOOL"
        assert tool.attributes["tool.name"] == "read_file"
        assert tool.name == "hermes.tool.read_file"
        mod.on_post_tool_call(tool_name="read_file", args={"path": "x.py"}, result={"content": "ok"},
                              task_id="t", session_id="s", tool_call_id="call-9", duration_ms=42)
        assert tool.attributes["tool.id"] == "call-9"
        assert tool.attributes["output.value"] == '{"content": "ok"}'
        assert tool.attributes["duration_ms"] == 42
        assert tool.ended is True

    def test_output_message_tool_calls_flattened(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        llm = tracer.spans[1]

        class _Fn:
            name = "search"
            arguments = '{"q": "x"}'

        class _TC:
            function = _Fn()

        class _Msg:
            content = None
            tool_calls = [_TC()]

        mod.on_post_api_request(task_id="t", session_id="s", api_call_count=1,
                                assistant_message=_Msg(), assistant_tool_call_count=1)
        assert llm.attributes["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == "search"
        assert llm.attributes["llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"] == '{"q": "x"}'
        # tool call present ⇒ root stays open.
        assert "t" in mod._TRACE_STATE

    def test_malformed_tool_calls_do_not_raise_or_leak_spans(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(
            task_id="t", session_id="s", api_call_count=1,
            request_messages=[{"role": "assistant", "tool_calls": 5}],
        )
        root, llm = tracer.spans[0], tracer.spans[1]

        class _Msg:
            content = None
            tool_calls = 5

        mod.on_post_api_request(
            task_id="t", session_id="s", api_call_count=1,
            assistant_message=_Msg(), assistant_content_chars=0,
            assistant_tool_call_count=0,
        )
        assert llm.ended is True
        assert root.ended is True
        assert mod._TRACE_STATE == {}

    def test_input_message_capture_is_bounded(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        messages = [{"role": "user", "content": str(i)} for i in range(55)]
        mod.on_pre_api_request(
            task_id="t", session_id="s", api_call_count=1,
            request_messages=messages,
        )
        llm = tracer.spans[1]
        assert llm.attributes["llm.input_messages.0.message.content"] == "5"
        assert "llm.input_messages.50.message.role" not in llm.attributes
        assert json.loads(llm.attributes["input.value"])[0]["content"] == "5"


# ---------------------------------------------------------------------------
# 7. Parenting contract (we hand OTel the right parent handle).
# ---------------------------------------------------------------------------
class TestParenting:
    def test_llm_and_tool_parent_on_root(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        root, llm = tracer.spans[0], tracer.spans[1]
        assert _parent_of(root) is None
        assert _parent_of(llm) is root
        mod.on_pre_tool_call(tool_name="grep", args={}, task_id="t", session_id="s")
        tool = tracer.spans[-1]
        assert _parent_of(tool) is root


# ---------------------------------------------------------------------------
# 8. Tool pairing (explicit id, FIFO-by-name, threaded FIFO under lock).
# ---------------------------------------------------------------------------
class TestToolPairing:
    def _seed_state(self, mod):
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        return tracer

    def test_explicit_tool_call_id(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        self._seed_state(mod)
        mod.on_pre_tool_call(tool_name="t1", args={}, task_id="t", session_id="s", tool_call_id="id-1")
        state = mod._TRACE_STATE["t"]
        assert "id-1" in state.tools
        mod.on_post_tool_call(tool_name="t1", args={}, result="r", task_id="t", session_id="s", tool_call_id="id-1")
        assert state.tools == {}

    def test_empty_id_fifo_within_name(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = self._seed_state(mod)
        mod.on_pre_tool_call(tool_name="dup", args={"n": 1}, task_id="t", session_id="s")
        mod.on_pre_tool_call(tool_name="dup", args={"n": 2}, task_id="t", session_id="s")
        first, second = tracer.spans[-2], tracer.spans[-1]
        mod.on_post_tool_call(tool_name="dup", args={}, result="a", task_id="t", session_id="s")
        mod.on_post_tool_call(tool_name="dup", args={}, result="b", task_id="t", session_id="s")
        assert first.attributes["output.value"] == "a"
        assert second.attributes["output.value"] == "b"
        assert mod._TRACE_STATE["t"].pending_tools_by_name.get("dup") is None

    def test_threaded_post_calls_preserve_fifo_under_lock(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = self._seed_state(mod)
        n = 8
        for _ in range(n):
            mod.on_pre_tool_call(tool_name="par", args={}, task_id="t", session_id="s")
        ended = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def worker():
            barrier.wait()
            mod.on_post_tool_call(tool_name="par", args={}, result="x",
                                  task_id="t", session_id="s")

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        spans = [s for s in tracer.spans if s.name == "hermes.tool.par"]
        assert len(spans) == n
        assert all(s.ended for s in spans)
        assert mod._TRACE_STATE["t"].pending_tools_by_name.get("par") is None


# ---------------------------------------------------------------------------
# 9. Content + truncation.
# ---------------------------------------------------------------------------
class TestContentTruncation:
    def test_content_captured_and_truncated(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        monkeypatch.setenv("HERMES_OPENINFERENCE_MAX_ATTR_CHARS", "20")
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        big = "x" * 500
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": big}])
        llm = tracer.spans[1]
        content = llm.attributes["llm.input_messages.0.message.content"]
        assert content.startswith("x" * 20)
        assert "truncated" in content

    def test_default_max_attr_chars(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        assert mod._max_attr_chars() == 12000


# ---------------------------------------------------------------------------
# 10. Retry: same api_call_count keeps one span + bumps llm.retry_count.
# ---------------------------------------------------------------------------
class TestRetry:
    def test_repeated_pre_keeps_one_span(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        for _ in range(3):
            mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                                   model="m", request_messages=[{"role": "user", "content": "hi"}])
        llm_spans = [s for s in tracer.spans if s.name == "hermes.llm.call.1"]
        assert len(llm_spans) == 1
        assert llm_spans[0].attributes["llm.retry_count"] == 2


# ---------------------------------------------------------------------------
# 11. Sweep-close on session boundary + agent-level tool (pre, no post).
# ---------------------------------------------------------------------------
class TestSweepClose:
    def test_empty_response_closes_turn(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        root, llm = tracer.spans[0], tracer.spans[1]

        class _Msg:
            content = None
            tool_calls = []

        mod.on_post_api_request(task_id="t", session_id="s", api_call_count=1,
                                assistant_message=_Msg(), assistant_content_chars=0,
                                assistant_tool_call_count=0)
        assert llm.ended
        assert root.ended
        assert mod._TRACE_STATE == {}

    def test_finalize_closes_open_spans(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        # An agent-level tool that fires pre but never post.
        mod.on_pre_tool_call(tool_name="agent_tool", args={}, task_id="t", session_id="s")
        root, llm, tool = tracer.spans[0], tracer.spans[1], tracer.spans[2]
        assert not (root.ended or llm.ended or tool.ended)
        mod.on_session_finalize(session_id="s")
        assert root.ended and llm.ended and tool.ended
        assert mod._TRACE_STATE == {}

    def test_reset_sweeps_all_when_no_session(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="t", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        mod.on_session_reset(session_id="")
        assert tracer.spans[0].ended
        assert mod._TRACE_STATE == {}

    def test_stale_state_reaped_on_next_pre_request(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_api_request(task_id="old", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "go"}])
        old_root, old_llm = tracer.spans[0], tracer.spans[1]
        mod._TRACE_STATE["old"].last_updated_at = (
            time.time() - mod._STATE_MAX_AGE_SECONDS - 1
        )
        mod._LAST_REAP_AT = 0.0

        mod.on_pre_api_request(task_id="new", session_id="s", api_call_count=1,
                               request_messages=[{"role": "user", "content": "next"}])

        assert old_root.ended
        assert old_llm.ended
        assert "old" not in mod._TRACE_STATE
        assert "new" in mod._TRACE_STATE


# ---------------------------------------------------------------------------
# 12. Backend-agnostic guard: telemetry shape stays vendor-neutral at runtime.
# ---------------------------------------------------------------------------
class TestBackendAgnostic:
    BACKEND_NEEDLES = (
        "phoenix", "arize", "langfuse", ":6006", "4317", "4318",
        "localhost", "127.0.0.1",
    )

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": (
                    "http://phoenix.localhost:6006/v1/traces"
                ),
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_RESOURCE_ATTRIBUTES": "openinference.project.name=langfuse",
            },
        ],
    )
    def test_runtime_outputs_are_vendor_neutral(self, monkeypatch, env):
        _clear_otel_env(monkeypatch)
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)

        class _Ctx:
            def __init__(self):
                self.hooks = []

            def register_hook(self, name, callback):
                self.hooks.append((name, callback.__name__))

        ctx = _Ctx()
        mod.register(ctx)

        mod.on_pre_api_request(
            task_id="t", session_id="s", platform="cli",
            model="model", provider="provider", api_mode="chat_completions",
            api_call_count=1,
            request_messages=[{"role": "user", "content": "hello"}],
        )

        class _Msg:
            content = "done"
            tool_calls = []

        mod.on_post_api_request(
            task_id="t", session_id="s", api_call_count=1,
            finish_reason="stop", assistant_message=_Msg(),
        )

        payload = {
            "hooks": [name for name, _ in ctx.hooks],
            "spans": [
                {"name": span.name, "attributes": span.attributes}
                for span in tracer.spans
            ],
        }
        text = json.dumps(payload, sort_keys=True, default=str).lower()
        for needle in self.BACKEND_NEEDLES:
            assert needle not in text, (
                f"backend-specific value leaked into telemetry: {needle!r}"
            )


# ---------------------------------------------------------------------------
# Legacy pre_llm_call disambiguation: only request-shaped (messages list) traces.
# ---------------------------------------------------------------------------
class TestLegacyLlmCall:
    def test_pre_llm_call_without_messages_is_ignored(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        _install_fake_tracer(mod)
        mod.on_pre_llm_call(task_id="t", session_id="s", conversation_history=[], user_message="hi")
        assert mod._TRACE_STATE == {}

    def test_pre_llm_call_with_messages_starts_root(self, monkeypatch):
        _clear_otel_env(monkeypatch)
        mod = _fresh_plugin()
        tracer = _install_fake_tracer(mod)
        mod.on_pre_llm_call(task_id="t", session_id="s",
                            messages=[{"role": "user", "content": "hi"}])
        assert "t" in mod._TRACE_STATE
        assert tracer.spans[0].attributes["openinference.span.kind"] == "AGENT"

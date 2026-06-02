"""openinference — backend-agnostic OpenInference/OTLP tracing for Hermes.

Exports OpenInference-compliant spans (Agent -> LLM -> Tool) over plain OTLP to
**any** OpenTelemetry-compatible backend. No backend name or endpoint is hardcoded
-- the destination is chosen entirely through standard ``OTEL_*`` environment
variables (see README.md for a list of compatible backends).

Activation is handled by the Hermes plugin system — standalone plugins only load
when listed in ``plugins.enabled`` (via ``hermes plugins enable
observability/openinference``, or by checking the box in the interactive
``hermes plugins`` UI). At runtime the plugin additionally requires:

  * the OpenTelemetry SDK + an OTLP exporter to be importable, and
  * an OTLP endpoint to be configured.

If either is missing the hooks are inert (no spans, no warnings per hook).

Backend selection (standard OpenTelemetry env vars, honored as-is):
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT
      Collector URL. **Activation gate** — if neither is set the plugin is inert.
  OTEL_EXPORTER_OTLP_PROTOCOL    - "http/protobuf" (default) or "grpc".
  OTEL_EXPORTER_OTLP_HEADERS / OTEL_EXPORTER_OTLP_TRACES_HEADERS  - auth headers
      (read natively by the exporter; never logged by this plugin).
  OTEL_SERVICE_NAME              - service name (default "hermes-agent").
  OTEL_RESOURCE_ATTRIBUTES       - extra resource attributes, incl. the recipe
      for OI project routing: OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=my-project

Minimal Hermes-namespaced knobs (only what OTEL_* doesn't cover):
  HERMES_OPENINFERENCE_MAX_ATTR_CHARS  - truncation bound per captured string (default 12000).
  HERMES_OPENINFERENCE_DEBUG           - "true" for verbose plugin logging (never logs headers).

Privacy: when active, the plugin captures prompt/response/tool I/O content (truncated
to MAX_ATTR_CHARS). There is no content flag — to not export content, disable the plugin.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# --- Optional OpenTelemetry imports (fail-open: missing dep ⇒ inert) --------
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode
except Exception:  # pragma: no cover - fail-open when the OTel SDK is absent
    _otel_trace = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None
    Status = None
    StatusCode = None

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as _OTLPHTTPSpanExporter,
    )
except Exception:  # pragma: no cover - fail-open
    _OTLPHTTPSpanExporter = None

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as _OTLPGRPCSpanExporter,
    )
except Exception:  # pragma: no cover - optional; only needed for protocol=grpc
    _OTLPGRPCSpanExporter = None

# openinference-semantic-conventions is the canonical source of the OI key
# strings. We pin the wire contract with the literal constants below (so the
# plugin is importable and testable without the package), but record whether
# the package is present for the README/setup story.
try:  # pragma: no cover - informational only, never gates behaviour
    import openinference.semconv.trace as _oi_semconv  # noqa: F401

    _OI_SEMCONV_AVAILABLE = True
except Exception:  # pragma: no cover
    _OI_SEMCONV_AVAILABLE = False


# --- OpenInference attribute keys (literal strings = the pinned wire contract) ---
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
INPUT_MIME_TYPE = "input.mime_type"
OUTPUT_VALUE = "output.value"
OUTPUT_MIME_TYPE = "output.mime_type"

LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_SYSTEM = "llm.system"
LLM_INVOCATION_PARAMETERS = "llm.invocation_parameters"
LLM_FINISH_REASON = "llm.finish_reason"
LLM_RETRY_COUNT = "llm.retry_count"

TOKEN_PROMPT = "llm.token_count.prompt"
TOKEN_COMPLETION = "llm.token_count.completion"
TOKEN_TOTAL = "llm.token_count.total"
TOKEN_CACHE_READ = "llm.token_count.prompt_details.cache_read"
TOKEN_CACHE_WRITE = "llm.token_count.prompt_details.cache_write"
TOKEN_REASONING = "llm.token_count.completion_details.reasoning"

LLM_INPUT_MESSAGES = "llm.input_messages"
LLM_OUTPUT_MESSAGES = "llm.output_messages"
LLM_TOOLS = "llm.tools"
MESSAGE_ROLE = "message.role"
MESSAGE_CONTENT = "message.content"
MESSAGE_TOOL_CALLS = "message.tool_calls"
TOOL_CALL_FUNCTION_NAME = "tool_call.function.name"
TOOL_CALL_FUNCTION_ARGUMENTS = "tool_call.function.arguments"
TOOL_JSON_SCHEMA = "tool.json_schema"

TOOL_NAME = "tool.name"
TOOL_DESCRIPTION = "tool.description"
TOOL_PARAMETERS = "tool.parameters"
TOOL_ID = "tool.id"

MIME_TYPE_JSON = "application/json"
MIME_TYPE_TEXT = "text/plain"

SESSION_ID = "session.id"
HERMES_MESSAGE_COUNT = "hermes.message_count"
HERMES_TOOL_COUNT = "hermes.tool_count"
HERMES_REQUEST_CHAR_COUNT = "hermes.request_char_count"
HERMES_APPROX_INPUT_TOKENS = "hermes.approx_input_tokens"
HERMES_API_DURATION_MS = "hermes.api_duration_ms"
HERMES_OUTPUT_CHARS = "hermes.output_chars"
HERMES_OUTPUT_TOOL_CALL_COUNT = "hermes.output_tool_call_count"
HERMES_RESPONSE_MODEL = "hermes.response_model"

# OpenInference span-kind values.
KIND_AGENT = "AGENT"
KIND_LLM = "LLM"
KIND_TOOL = "TOOL"

DEFAULT_SERVICE_NAME = "hermes-agent"
DEFAULT_MAX_ATTR_CHARS = 12000
_MAX_CAPTURED_MESSAGES = 50
# Reap TraceState entries idle longer than this so a Turn that never closes
# cannot leak spans/memory in a long-running gateway.
_STATE_MAX_AGE_SECONDS = 3600.0
_STATE_REAP_INTERVAL_SECONDS = 60.0
_STATE_MAX_ENTRIES = 4096


# --- State ------------------------------------------------------------------
@dataclass
class TraceState:
    trace_key: str
    root_span: Any
    session_id: str = ""
    llm_spans: dict[str, Any] = field(default_factory=dict)
    llm_retry_counts: dict[str, int] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    pending_tools_by_name: dict[str, list[Any]] = field(default_factory=dict)
    last_updated_at: float = field(default_factory=time.time)


_STATE_LOCK = threading.Lock()
_TRACE_STATE: dict[str, TraceState] = {}
_LAST_REAP_AT = 0.0

_TRACER = None
_TRACER_PROVIDER = None
_TRACER_LOCK = threading.Lock()
# Sentinel: "_get_tracer() tried and failed". Lets every later hook fast-return
# without re-checking env or re-attempting provider build (bundled-plugin precedent).
_INIT_FAILED = object()
_ATEXIT_REGISTERED = False


class _PluginContext(Protocol):
    def register_hook(self, name: str, callback: Any) -> None: ...


# --- Small helpers ----------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(*names: str) -> bool:
    for name in names:
        value = _env(name).lower()
        if value:
            return value in {"1", "true", "yes", "on"}
    return False


def _debug_enabled() -> bool:
    return _env_bool("HERMES_OPENINFERENCE_DEBUG")


def _debug(message: str) -> None:
    if _debug_enabled():
        logger.info("OpenInference tracing: %s", message)


def _max_attr_chars() -> int:
    raw = _env("HERMES_OPENINFERENCE_MAX_ATTR_CHARS")
    if not raw:
        return DEFAULT_MAX_ATTR_CHARS
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_MAX_ATTR_CHARS
    except ValueError:
        return DEFAULT_MAX_ATTR_CHARS


def _safe_attr(
    value: Any, *, max_chars: int | None = None
) -> str | int | float | bool:
    """Stringify + truncate a value for use as a span attribute (no redaction)."""
    max_chars = max_chars if max_chars is not None else _max_attr_chars()
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    elif isinstance(value, (int, float, bool)):
        return value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
    if len(text) > max_chars:
        return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"
    return text


def _set(span: Any, key: str, value: Any, *, max_chars: int | None = None) -> None:
    """Set a span attribute, skipping None/empty and truncating strings."""
    if span is None or value is None:
        return
    if isinstance(value, str):
        if not value:
            return
        value = _safe_attr(value, max_chars=max_chars)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        pass
    else:
        value = _safe_attr(value, max_chars=max_chars)
    try:
        span.set_attribute(key, value)
    except Exception:  # pragma: no cover - fail-open
        pass


def _endpoint_configured() -> bool:
    return bool(
        _env("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or _env("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


def _build_exporter() -> Any | None:
    """Pick the OTLP exporter from OTEL_EXPORTER_OTLP_PROTOCOL.

    Defaults to http/protobuf. gRPC is used only when explicitly requested
    *and* the gRPC exporter is installed; otherwise we fall back to HTTP.
    """
    protocol = _env("OTEL_EXPORTER_OTLP_PROTOCOL").lower()
    if protocol == "grpc":
        if _OTLPGRPCSpanExporter is not None:
            return _OTLPGRPCSpanExporter()
        logger.warning(
            "OpenInference plugin: OTEL_EXPORTER_OTLP_PROTOCOL=grpc but the gRPC "
            "exporter is not installed; falling back to http/protobuf. Install "
            "opentelemetry-exporter-otlp-proto-grpc to use gRPC."
        )
    if _OTLPHTTPSpanExporter is None:
        return None
    return _OTLPHTTPSpanExporter()


def _build_resource() -> Any:
    service_name = _env("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME
    # Resource.create() merges OTEL_RESOURCE_ATTRIBUTES + OTEL_SERVICE_NAME from
    # the environment; the explicit service.name preserves our default when the
    # operator hasn't set OTEL_SERVICE_NAME.
    return Resource.create({"service.name": service_name})


def _get_tracer() -> Any | None:
    """Return a cached private tracer, or None when tracing is unavailable.

    Cached: first call builds a plugin-private TracerProvider + bounded
    BatchSpanProcessor + OTLP exporter; later calls return it (or fast-return
    None via _INIT_FAILED). Never hijacks the global OTel provider.
    """
    global _TRACER, _TRACER_PROVIDER, _ATEXIT_REGISTERED
    cached = _TRACER
    if cached is _INIT_FAILED:
        return None
    if cached is not None:
        return cached

    with _TRACER_LOCK:
        if _TRACER is _INIT_FAILED:
            return None
        if _TRACER is not None:
            return _TRACER

        if _otel_trace is None or TracerProvider is None or Resource is None:
            _TRACER = _INIT_FAILED
            return None

        if not _endpoint_configured():
            # No surprise traffic to a phantom local collector — stay inert.
            _TRACER = _INIT_FAILED
            return None

        try:
            exporter = _build_exporter()
            if exporter is None:
                logger.warning(
                    "OpenInference plugin: no OTLP span exporter available. Install "
                    "opentelemetry-exporter-otlp-proto-http (and the SDK)."
                )
                _TRACER = _INIT_FAILED
                return None
            provider = TracerProvider(resource=_build_resource())
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _TRACER_PROVIDER = provider
            _TRACER = provider.get_tracer("hermes.openinference", "1.0.0")
        except Exception as exc:  # pragma: no cover - fail-open
            logger.warning("OpenInference plugin: could not initialize tracer: %s", exc)
            _TRACER = _INIT_FAILED
            _TRACER_PROVIDER = None
            return None

        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown)
            _ATEXIT_REGISTERED = True

        _debug("tracer initialized")
        return _TRACER


def _shutdown() -> None:
    """Idempotent hard-exit backstop: sweep-close open traces, then shut down.

    Spans are only queued for export on ``.end()``, so any in-flight root/LLM/
    tool spans must be sweep-closed *before* ``provider.shutdown()`` performs its
    final flush — otherwise they are dropped on hard exit. State is removed
    atomically under ``_STATE_LOCK``, but spans are ended outside the lock so we
    never hold it across an export. Idempotent: the provider reference is dropped
    before shutdown, so a second call finds no state and no provider to re-shut.
    """
    global _TRACER_PROVIDER
    with _STATE_LOCK:
        states = list(_TRACE_STATE.values())
        _TRACE_STATE.clear()
    for state in states:
        try:
            _sweep_close_state(state, error="span closed at process exit")
        except Exception:  # pragma: no cover - fail-open
            pass
    provider = _TRACER_PROVIDER
    if provider is None:
        return
    # Drop the reference first so a repeat call (or a post-shutdown _flush) is a
    # no-op rather than re-invoking provider.shutdown().
    _TRACER_PROVIDER = None
    try:
        provider.shutdown()
    except Exception:  # pragma: no cover - fail-open
        pass


def _flush() -> None:
    provider = _TRACER_PROVIDER
    if provider is None:
        return
    try:
        provider.force_flush()
    except Exception:  # pragma: no cover - fail-open
        pass


def _trace_key(task_id: str, session_id: str) -> str:
    # task_id-primary: pre_tool_call carries only task_id, so keying on
    # session_id would orphan every tool span.
    if task_id:
        return task_id
    if session_id:
        return f"session:{session_id}"
    return f"thread:{threading.get_ident()}"


def _request_key(api_call_count: Any) -> str:
    return str(api_call_count or 0)


def _start_span(
    name: str, *, parent: Any, kind: str, attributes: dict[str, Any] | None = None
) -> Any | None:
    tracer = _get_tracer()
    if tracer is None:
        return None
    attrs = {SPAN_KIND: kind}
    if attributes:
        attrs.update(attributes)
    ctx = _otel_trace.set_span_in_context(parent) if parent is not None else None
    try:
        return tracer.start_span(name, context=ctx, attributes=attrs)
    except Exception:  # pragma: no cover - fail-open
        return None


def _end_span(span: Any, *, error: str | None = None) -> None:
    if span is None:
        return
    try:
        if error is not None and Status is not None and StatusCode is not None:
            span.set_status(Status(StatusCode.ERROR, error))
        elif Status is not None and StatusCode is not None:
            span.set_status(Status(StatusCode.OK))
        span.end()
    except Exception:  # pragma: no cover - fail-open
        pass


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:
        return str(content)


def _captured_messages(messages: Any) -> list[Any]:
    if not isinstance(messages, list):
        return []
    return messages[-_MAX_CAPTURED_MESSAGES:]


def _safe_tool_calls(tool_calls: Any) -> list[Any] | tuple[Any, ...]:
    if isinstance(tool_calls, (list, tuple)):
        return tool_calls
    return []


def _set_message_attrs(span: Any, prefix: str, messages: Any) -> None:
    """Flatten a list of chat messages into indexed OI message attributes.

    Never sets a raw dict/list — only flattened scalar keys, per the OI spec.
    """
    if span is None or not isinstance(messages, list):
        return
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        base = f"{prefix}.{i}.{MESSAGE_ROLE}"
        _set(span, base, message.get("role"))
        content = message.get("content")
        if content is not None:
            _set(span, f"{prefix}.{i}.{MESSAGE_CONTENT}", _stringify_content(content))
        tool_calls = _safe_tool_calls(message.get("tool_calls"))
        for j, tc in enumerate(tool_calls):
            fn = tc.get("function") if isinstance(tc, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            tc_prefix = f"{prefix}.{i}.{MESSAGE_TOOL_CALLS}.{j}"
            _set(span, f"{tc_prefix}.{TOOL_CALL_FUNCTION_NAME}", name)
            if args is not None:
                _set(span, f"{tc_prefix}.{TOOL_CALL_FUNCTION_ARGUMENTS}", _stringify_content(args))


def _set_output_message_attrs(span: Any, prefix: str, assistant_message: Any) -> None:
    """Flatten an assistant message object (post-hook) into output message attrs."""
    if span is None or assistant_message is None:
        return
    _set(span, f"{prefix}.0.{MESSAGE_ROLE}", "assistant")
    content = getattr(assistant_message, "content", None)
    if content is not None:
        _set(span, f"{prefix}.0.{MESSAGE_CONTENT}", _stringify_content(content))
    tool_calls = _safe_tool_calls(getattr(assistant_message, "tool_calls", None))
    for j, tc in enumerate(tool_calls):
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        args = getattr(fn, "arguments", None) if fn is not None else None
        tc_prefix = f"{prefix}.0.{MESSAGE_TOOL_CALLS}.{j}"
        _set(span, f"{tc_prefix}.{TOOL_CALL_FUNCTION_NAME}", name)
        if args is not None:
            _set(span, f"{tc_prefix}.{TOOL_CALL_FUNCTION_ARGUMENTS}", _stringify_content(args))


def _set_usage_attrs(span: Any, usage: Any) -> None:
    if span is None or not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    if completion is None:
        completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    _set(span, TOKEN_PROMPT, prompt)
    _set(span, TOKEN_COMPLETION, completion)
    _set(span, TOKEN_TOTAL, total)
    if usage.get("cache_read_tokens"):
        _set(span, TOKEN_CACHE_READ, usage.get("cache_read_tokens"))
    if usage.get("cache_write_tokens"):
        _set(span, TOKEN_CACHE_WRITE, usage.get("cache_write_tokens"))
    if usage.get("reasoning_tokens"):
        _set(span, TOKEN_REASONING, usage.get("reasoning_tokens"))


def _reap_stale_locked(now: float) -> list[TraceState]:
    """Remove idle/over-capacity TraceState entries (caller holds _STATE_LOCK)."""
    global _LAST_REAP_AT
    removed: list[TraceState] = []
    should_scan_age = (
        now < _LAST_REAP_AT
        or now - _LAST_REAP_AT >= _STATE_REAP_INTERVAL_SECONDS
    )
    if should_scan_age:
        _LAST_REAP_AT = now
        stale = [
            k
            for k, s in _TRACE_STATE.items()
            if now - s.last_updated_at > _STATE_MAX_AGE_SECONDS
        ]
        for key in stale:
            state = _TRACE_STATE.pop(key, None)
            if state is not None:
                removed.append(state)

    overflow = len(_TRACE_STATE) - _STATE_MAX_ENTRIES
    if overflow > 0:
        # Evict oldest first. This path is only hot when the global cap is hit.
        for key, _ in sorted(_TRACE_STATE.items(), key=lambda kv: kv[1].last_updated_at)[
            :overflow
        ]:
            state = _TRACE_STATE.pop(key, None)
            if state is not None:
                removed.append(state)
    return removed


def _sweep_close_state(state: TraceState, *, error: str | None = None) -> None:
    """Force-close all open spans for a TraceState (sweep-close backbone)."""
    for span in list(state.llm_spans.values()):
        _end_span(span, error=error or "span closed by sweep")
    state.llm_spans.clear()
    state.llm_retry_counts.clear()
    for span in list(state.tools.values()):
        _end_span(span, error=error or "span closed by sweep")
    state.tools.clear()
    for queue in list(state.pending_tools_by_name.values()):
        for span in queue:
            _end_span(span, error=error or "tool span closed by sweep")
    state.pending_tools_by_name.clear()
    _end_span(state.root_span)


def _start_root(task_key: str, *, task_id: str, session_id: str, platform: str) -> TraceState:
    attrs: dict[str, Any] = {}
    if session_id:
        attrs[SESSION_ID] = session_id
    if platform:
        attrs["hermes.platform"] = platform
    if task_id:
        attrs["hermes.task_id"] = task_id
    profile = _env("HERMES_PROFILE")
    if profile:
        attrs["hermes.profile"] = profile
    root = _start_span("hermes.agent.turn", parent=None, kind=KIND_AGENT, attributes=attrs)
    return TraceState(trace_key=task_key, root_span=root, session_id=session_id)


# --- Hook callbacks ---------------------------------------------------------
def on_pre_llm_call(*, task_id: str = "", session_id: str = "", platform: str = "",
                    messages: Any = None, **_: Any) -> None:
    # Disambiguation (bundled-plugin precedent): the current turn-scoped pre_llm_call
    # is used for context injection and carries no `messages` list — tracing it
    # would create an orphan root. Only the legacy request-shaped call (with a
    # `messages` list) is treated as request-scoped here.
    if not isinstance(messages, list):
        return
    if _get_tracer() is None:
        return
    task_key = _trace_key(task_id, session_id)
    with _STATE_LOCK:
        if task_key not in _TRACE_STATE:
            _TRACE_STATE[task_key] = _start_root(
                task_key, task_id=task_id, session_id=session_id, platform=platform
            )
        _TRACE_STATE[task_key].last_updated_at = time.time()


def on_pre_api_request(*, task_id: str = "", session_id: str = "", platform: str = "",
                       model: str = "", provider: str = "", base_url: str = "",
                       api_mode: str = "", api_call_count: int = 0,
                       request_messages: Any = None, max_tokens: Any = None,
                       tool_count: int = 0, message_count: int = 0,
                       approx_input_tokens: int = 0,
                       request_char_count: int = 0,
                       user_message: Any = None, **_: Any) -> None:
    if _get_tracer() is None:
        return
    task_key = _trace_key(task_id, session_id)
    req_key = _request_key(api_call_count)

    span = None
    retry_span = None
    root_span = None
    retry_count = 0
    stale_states: list[TraceState] = []
    with _STATE_LOCK:
        now = time.time()
        stale_states = _reap_stale_locked(now)
        state = _TRACE_STATE.get(task_key)
        if state is None:
            state = _start_root(task_key, task_id=task_id, session_id=session_id, platform=platform)
            _TRACE_STATE[task_key] = state
        state.last_updated_at = now
        root_span = state.root_span

        existing = state.llm_spans.get(req_key)
        if existing is not None:
            # Retry of the same logical call: keep the span, bump retry_count.
            retry_count = state.llm_retry_counts.get(req_key, 0) + 1
            state.llm_retry_counts[req_key] = retry_count
            retry_span = existing
        else:
            span = _start_span(
                f"hermes.llm.call.{api_call_count}",
                parent=state.root_span,
                kind=KIND_LLM,
            )
            state.llm_spans[req_key] = span
            state.llm_retry_counts[req_key] = 0

    for stale_state in stale_states:
        _sweep_close_state(stale_state)

    if user_message is not None:
        _set(root_span, INPUT_VALUE, _stringify_content(user_message))
        _set(root_span, INPUT_MIME_TYPE, MIME_TYPE_TEXT)

    if retry_span is not None:
        _set(retry_span, LLM_RETRY_COUNT, retry_count)
        return

    if span is None:
        return

    _set(span, LLM_MODEL_NAME, model)
    _set(span, LLM_PROVIDER, provider)
    _set(span, LLM_SYSTEM, provider)
    _set(span, SESSION_ID, session_id)
    _set(span, HERMES_MESSAGE_COUNT, message_count)
    _set(span, HERMES_TOOL_COUNT, tool_count)
    _set(span, HERMES_APPROX_INPUT_TOKENS, approx_input_tokens)
    _set(span, HERMES_REQUEST_CHAR_COUNT, request_char_count)
    invocation: dict[str, Any] = {"api_mode": api_mode}
    if max_tokens is not None:
        invocation["max_tokens"] = max_tokens
    _set(span, LLM_INVOCATION_PARAMETERS, json.dumps(invocation, default=str))
    if isinstance(request_messages, list):
        messages = _captured_messages(request_messages)
        _set(span, INPUT_VALUE, _stringify_content(messages))
        _set(span, INPUT_MIME_TYPE, MIME_TYPE_JSON)
        _set_message_attrs(span, LLM_INPUT_MESSAGES, messages)


def on_post_api_request(*, task_id: str = "", session_id: str = "", model: str = "",
                        provider: str = "", api_mode: str = "", api_call_count: int = 0,
                        api_duration: float = 0.0, finish_reason: str = "",
                        response_model: Any = None,
                        usage: Any = None, assistant_message: Any = None,
                        assistant_content_chars: int = 0,
                        assistant_tool_call_count: int = 0, **_: Any) -> None:
    if _get_tracer() is None:
        return
    task_key = _trace_key(task_id, session_id)
    req_key = _request_key(api_call_count)

    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        span = state.llm_spans.pop(req_key, None) if state else None
        if state is not None:
            state.llm_retry_counts.pop(req_key, None)
            state.last_updated_at = time.time()
    if state is None or span is None:
        return

    _set(span, LLM_FINISH_REASON, finish_reason)
    _set_usage_attrs(span, usage)
    if assistant_message is not None:
        _set(span, OUTPUT_VALUE, _stringify_content(getattr(assistant_message, "content", None)))
        _set(span, OUTPUT_MIME_TYPE, MIME_TYPE_TEXT)
        _set_output_message_attrs(span, LLM_OUTPUT_MESSAGES, assistant_message)
    if api_duration:
        _set(span, HERMES_API_DURATION_MS, float(api_duration) * 1000.0)
    _set(span, HERMES_OUTPUT_CHARS, assistant_content_chars)
    _set(span, HERMES_OUTPUT_TOOL_CALL_COUNT, assistant_tool_call_count)
    if response_model:
        _set(span, HERMES_RESPONSE_MODEL, response_model)
        _set(span, LLM_MODEL_NAME, response_model)
    _end_span(span)

    message_tool_calls = (
        _safe_tool_calls(getattr(assistant_message, "tool_calls", None))
        if assistant_message is not None
        else []
    )
    has_tools = bool(assistant_tool_call_count) or bool(message_tool_calls)
    if not has_tools:
        # Turn complete (no tool calls to wait for) — close the root.
        with _STATE_LOCK:
            popped = _TRACE_STATE.pop(task_key, None)
        if popped is not None:
            _sweep_close_state(popped)
        _flush()


def on_pre_tool_call(*, tool_name: str = "", args: Any = None, task_id: str = "",
                     session_id: str = "", tool_call_id: str = "", **_: Any) -> None:
    if _get_tracer() is None:
        return
    task_key = _trace_key(task_id, session_id)
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        state.last_updated_at = time.time()
        attrs: dict[str, Any] = {TOOL_NAME: tool_name}
        span = _start_span(
            f"hermes.tool.{tool_name}",
            parent=state.root_span,
            kind=KIND_TOOL,
            attributes=attrs,
        )
        if span is not None and args is not None:
            _set(span, TOOL_PARAMETERS, _stringify_content(args))
            _set(span, INPUT_VALUE, _stringify_content(args))
            _set(span, INPUT_MIME_TYPE, MIME_TYPE_JSON)
        # tool_call_id is empty at pre time today, so we pair FIFO-by-name; the
        # id-keyed dict is dormant forward-compat (never triggers at pre now).
        if tool_call_id:
            state.tools[tool_call_id] = span
        else:
            state.pending_tools_by_name.setdefault(tool_name, []).append(span)


def on_post_tool_call(*, tool_name: str = "", args: Any = None, result: Any = None,
                      task_id: str = "", session_id: str = "", tool_call_id: str = "",
                      duration_ms: Any = None, **_: Any) -> None:
    if _get_tracer() is None:
        return
    task_key = _trace_key(task_id, session_id)
    span = None
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        state.last_updated_at = time.time()
        if tool_call_id:
            span = state.tools.pop(tool_call_id, None)
        if span is None:
            queue = state.pending_tools_by_name.get(tool_name)
            if queue:
                span = queue.pop(0)
                if not queue:
                    state.pending_tools_by_name.pop(tool_name, None)
    if span is None:
        return

    # tool.id is only available on the post hook (pre lacks it).
    _set(span, TOOL_ID, tool_call_id)
    if result is not None:
        _set(span, OUTPUT_VALUE, _stringify_content(result))
        _set(span, OUTPUT_MIME_TYPE, MIME_TYPE_JSON)
    if duration_ms is not None:
        _set(span, "duration_ms", duration_ms)
    _end_span(span)


def _on_session_boundary(*, session_id: str = "", **_: Any) -> None:
    """Flush + sweep-close on session finalize/reset (OTel-specific addition).

    These boundary hooks carry session_id (not task_id), so we sweep every
    open trace for the session — or all open traces when session_id is absent.
    """
    if _get_tracer() is None:
        return
    with _STATE_LOCK:
        if session_id:
            keys = [
                k for k, s in _TRACE_STATE.items()
                if s.session_id == session_id or k == f"session:{session_id}"
            ]
        else:
            keys = list(_TRACE_STATE.keys())
        states = [_TRACE_STATE.pop(k) for k in keys if k in _TRACE_STATE]
    for state in states:
        _sweep_close_state(state)
    if states:
        _flush()


on_session_finalize = _on_session_boundary
on_session_reset = _on_session_boundary

# Alias used by older Hermes branches that fire pre_llm_call/post_llm_call with
# request-shaped payloads (a `messages` list). post is identical to the api one.
on_post_llm_call = on_post_api_request


def register(ctx: _PluginContext) -> None:
    # Prefer pre_api_request/post_api_request (the real per-API-call seam).
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    # Legacy/turn-scoped variants (disambiguated by the `messages` list).
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    # OTel flush + sweep-close (intentional addition over the base hook set).
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)

# OpenInference tracing (backend-agnostic)

Optional, opt-in tracing for Hermes that emits **OpenInference**-compliant spans
(Agent → LLM → Tool) over plain **OTLP** to **any** OpenTelemetry-compatible backend:
Phoenix, Arize, Langfuse, Jaeger/Tempo, Honeycomb, Grafana, Dash0, … No vendor name
or endpoint is hardcoded — the destination is chosen entirely through standard
`OTEL_*` environment variables.

The plugin is invisible until you enable it, and inert (no spans, no per-hook
warnings) until an OTLP endpoint is configured. It never changes Hermes core and
never raises into the agent loop.

> **Privacy:** when active, this plugin captures prompt/response/tool I/O content
> (truncated to `HERMES_OPENINFERENCE_MAX_ATTR_CHARS`, default 12000) and exports it
> to the configured endpoint. There is no content flag — **to not export content,
> disable the plugin.**

## 1. Install the runtime dependencies

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http openinference-semantic-conventions
# optional, only if you export via gRPC:
pip install opentelemetry-exporter-otlp-proto-grpc
```

(These are *not* added to `pyproject.toml` — the plugin imports them lazily and
stays inert if they are missing, matching the bundled Langfuse plugin.)

## 2. Enable the plugin

```bash
hermes plugins enable observability/openinference
# …or check the box in the interactive `hermes plugins` UI.
```

## 3. Point it at any OTLP backend (standard OTEL_* vars)

Set these in `$HERMES_HOME/.env` (or your shell). **If no endpoint is set, the plugin
stays inert** — there is no default endpoint and no surprise traffic.

| Var | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector URL. **Activation gate.** |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` (default) or `grpc` (needs the gRPC exporter). |
| `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Auth headers for hosted backends. Never logged. |
| `OTEL_SERVICE_NAME` | Service name (default `hermes-agent`). |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, incl. OI project routing: `OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=my-project`. |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | Sampling, where feasible. |

Hermes-namespaced knobs (only what `OTEL_*` doesn't cover):

| Var | Default | Purpose |
|---|---|---|
| `HERMES_OPENINFERENCE_MAX_ATTR_CHARS` | `12000` | Truncation bound per captured string. |
| `HERMES_OPENINFERENCE_DEBUG` | `false` | Verbose plugin logging (never logs header values). |

### Examples (vendor-neutral — use whichever collector you run)

```bash
# Any OTLP/HTTP collector (Phoenix, Tempo, Honeycomb, a local otel-collector, …):
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://localhost:4318/v1/traces"

# Hosted backend with auth headers:
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example-backend.com"
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>"

# Route to a named project on OI-aware backends:
export OTEL_RESOURCE_ATTRIBUTES="openinference.project.name=my-project"
```

## 4. Verify

Run a Hermes turn, then look in your backend for a `hermes.agent.turn` (kind
`AGENT`) span with `hermes.llm.call.N` (`LLM`) and `hermes.tool.<name>` (`TOOL`)
children. With `HERMES_OPENINFERENCE_DEBUG=true` the plugin logs `tracer
initialized` once and otherwise stays quiet. Eyeball the waterfall to confirm the
Agent → LLM → Tool nesting.

## 5. Disable

```bash
hermes plugins disable observability/openinference
```

Disabling stops all span export. (Unsetting the OTLP endpoint also makes the
plugin inert.)

## What it emits

- **Root** `hermes.agent.turn` — `openinference.span.kind=AGENT`, carries
  `session.id`, `hermes.platform`, `hermes.profile`.
- **LLM** `hermes.llm.call.N` — `kind=LLM`, one span per logical API call (retries
  bump `llm.retry_count`, not new spans), with `llm.model_name`, `llm.provider`,
  `llm.invocation_parameters`, `llm.finish_reason`, token counts
  (`llm.token_count.*`), and flattened `llm.input_messages.*` / `llm.output_messages.*`.
  Input capture is capped to the most recent 50 messages so long sessions do not
  repeatedly export the entire conversation history.
- **TOOL** `hermes.tool.<name>` — `kind=TOOL`, with `tool.name`, `tool.id`,
  `tool.parameters`, `input.value`/`output.value`, and `duration_ms`.

## Notes / known limitations

- **Best-effort coverage.** Spans are built from existing Hermes hooks with no core
  changes. Turns that never cleanly complete (and tools that fire `pre` but no
  `post`, e.g. blocked/guardrailed tools) are closed by **sweep-close** on session
  finalize/reset (and at process exit), so they appear with a best-effort status.
- **Tool pairing is FIFO-by-name.** `pre_tool_call` carries no `tool_call_id`, so
  spans are paired by tool name in FIFO order. Two concurrent same-name tools may
  finish out of order, or an earlier blocked same-name tool may never emit `post`,
  swapping duration/status/result between spans — span count, names, and parenting
  stay correct.
- **One LLM span per logical call.** Provider/SDK retries of the same call keep a
  single span and increment `llm.retry_count` (total wall-time, honest retry count).
- The plugin uses a **private** `TracerProvider`; it never touches the global OTel
  provider or any existing instrumentation.

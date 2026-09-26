"""
observability.py
==================
NEW, ADDITIVE MODULE — does not modify any existing file.

Feature 4: Enterprise Observability & Tracing (LangSmith / OpenTelemetry),
replacing scattered print("DEBUG: ...") calls with real structured tracing:
every RAG retrieval step, token cost, latency, and prompt gets a trace.

Two backends supported, both optional (module works fine with neither —
it just logs locally instead):

  A) LangSmith  -> set env vars:
        LANGCHAIN_TRACING_V2=true
        LANGCHAIN_API_KEY=<your key>
        LANGCHAIN_PROJECT=fleet-swarm-project
     Then just use the `@traced` decorator below on any function — LangSmith
     auto-captures inputs/outputs/latency/cost if you're using langchain-core
     objects, and still captures timing/args for plain functions.

  B) OpenTelemetry -> set env var:
        OTEL_EXPORTER_OTLP_ENDPOINT=<your collector, e.g. http://localhost:4318>
     Traces export via OTLP to any backend (Jaeger, Honeycomb, Arize Phoenix,
     Grafana Tempo, etc).

Usage (drop-in decorator, no changes needed to existing function bodies):

    from advanced.observability import traced

    @traced("mechanic_parts_rag_search")
    def search_parts(query):
        ...
"""

import os
import time
import functools
import json
from typing import Callable, Any

_OTEL_ENABLED = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
_LANGSMITH_ENABLED = os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"

_tracer = None
if _OTEL_ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": "fleet-swarm-backend"})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") + "/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("fleet-swarm")
        print("[observability] OpenTelemetry tracing ENABLED -> exporting to",
              os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    except Exception as e:
        print(f"[observability] WARNING: OpenTelemetry setup failed ({e}). "
              f"Falling back to local structured logging.")
        _tracer = None

if _LANGSMITH_ENABLED:
    try:
        import langsmith  # noqa: F401
        print("[observability] LangSmith tracing ENABLED for project:",
              os.getenv("LANGCHAIN_PROJECT", "default"))
    except Exception as e:
        print(f"[observability] WARNING: LangSmith not installed ({e}). pip install langsmith")


def _local_log(step_name: str, args, kwargs, result, duration_ms: float, error: str = None):
    """Structured JSON log line (replaces ad-hoc print("DEBUG: ...")).
    Pipe this to a log aggregator (CloudWatch/Datadog/ELK) in production."""
    log_entry = {
        "type": "trace",
        "step": step_name,
        "duration_ms": round(duration_ms, 2),
        "status": "error" if error else "ok",
    }
    if error:
        log_entry["error"] = error
    print(json.dumps(log_entry))


def traced(step_name: str):
    """Decorator: wraps any function (agent node, RAG call, tool call) with
    timing + tracing, using whichever backend is configured (LangSmith,
    OpenTelemetry, or local structured JSON logs as the always-on baseline).
    """

    def decorator(fn: Callable) -> Callable:
        # If LangSmith is on and installed, prefer its native @traceable —
        # it captures LLM token/cost automatically for LangChain calls.
        if _LANGSMITH_ENABLED:
            try:
                from langsmith import traceable
                fn = traceable(name=step_name)(fn)
            except Exception:
                pass

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            span_cm = _tracer.start_as_current_span(step_name) if _tracer else None
            span = span_cm.__enter__() if span_cm else None
            error = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = str(e)
                if span:
                    span.record_exception(e)
                raise
            finally:
                duration_ms = (time.time() - start) * 1000
                if span:
                    span.set_attribute("duration_ms", duration_ms)
                    span_cm.__exit__(None, None, None)
                _local_log(step_name, args, kwargs, None, duration_ms, error)

        return wrapper

    return decorator


class TraceContext:
    """Optional richer context manager for tracing a whole incident's
    end-to-end run (e.g. wrap your `run_swarm()` call with this):

        with TraceContext("incident_run", incident_id=incident.vehicle_id) as ctx:
            result = orchestrator.run_swarm()
            ctx.add_metadata({"final_status": result.status})
    """

    def __init__(self, name: str, **metadata):
        self.name = name
        self.metadata = metadata
        self.start = None

    def __enter__(self):
        self.start = time.time()
        return self

    def add_metadata(self, extra: dict):
        self.metadata.update(extra)

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start) * 1000
        log_entry = {
            "type": "incident_trace",
            "name": self.name,
            "duration_ms": round(duration_ms, 2),
            "metadata": self.metadata,
            "status": "error" if exc_type else "ok",
        }
        if exc_type:
            log_entry["error"] = str(exc_val)
        print(json.dumps(log_entry, default=str))
        return False  # don't suppress exceptions

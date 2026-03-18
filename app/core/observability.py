from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode, get_current_span
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


logger = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TRACE_FIELDS = {"mode", "route", "outcome", "tool", "model", "node", "stage", "operation", "variant", "category", "degraded", "attempt", "status", "restored"}
_run_id: ContextVar[str] = ContextVar("observation_run_id", default="")
_session_id: ContextVar[str] = ContextVar("observation_session_id", default="")


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32] if session_id else ""


def _mask_langfuse_payload(data: Any, **_: Any) -> Any:
    if _run_id.get() and isinstance(data, dict) and data.get("run_id") == _run_id.get():
        return {"run_id": data["run_id"]}
    return "[redacted]"


class Observability:
    def __init__(
        self,
        otel_endpoint: str = "",
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        langfuse_host: str = "",
    ) -> None:
        self._registry = CollectorRegistry()
        self._operations = Counter(
            "ecommerce_operations_total", "Operations by category, name and outcome",
            ("kind", "name", "outcome"), registry=self._registry,
        )
        self._durations = Histogram(
            "ecommerce_operation_duration_seconds", "Operation duration in seconds",
            ("kind", "name", "outcome"), registry=self._registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 3, 5, 10, 15, 20, 30, 45, 60),
        )
        self._tokens = Counter(
            "ecommerce_tokens_total", "Reported input and output tokens",
            ("kind", "name"), registry=self._registry,
        )
        self._provider = None
        self._tracer = None
        self._langfuse = None
        self._handler_class = None
        self._langfuse_public_key = langfuse_public_key
        if otel_endpoint:
            self._configure_otel(otel_endpoint)
        if langfuse_public_key and langfuse_secret_key:
            self._configure_langfuse(langfuse_public_key, langfuse_secret_key, langfuse_host)

        # Known failure series have a baseline before the first simulated incident.
        for name in ("chat", "jev_choice", "jev_noul", "case_analysis"):
            self._operations.labels("model", name, "timeout")
        for name in ("lookup_order", "create_ticket"):
            for outcome in ("ok", "error"):
                self._operations.labels("tool", name, outcome)
        for outcome in ("ok", "degraded", "error"):
            self._operations.labels("agent", "chat", outcome)

    def _configure_otel(self, endpoint: str) -> None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        self._provider = TracerProvider(resource=Resource.create({"service.name": "ecommerce-support"}))
        self._provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3)))
        self._tracer = self._provider.get_tracer(__name__)

    def _configure_langfuse(self, public_key: str, secret_key: str, host: str) -> None:
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            logger.warning("Langfuse telemetry requires the integrations extra")
            return

        class RedactedCallback(CallbackHandler):
            def on_llm_error(self, error: BaseException, **kwargs: Any) -> Any:
                return super().on_llm_error(RuntimeError(type(error).__name__), **kwargs)

            def on_chain_error(self, error: BaseException, **kwargs: Any) -> Any:
                return super().on_chain_error(RuntimeError(type(error).__name__), **kwargs)

            def on_tool_error(self, error: BaseException, **kwargs: Any) -> Any:
                return super().on_tool_error(RuntimeError(type(error).__name__), **kwargs)

        self._langfuse = Langfuse(
            public_key=public_key, secret_key=secret_key, base_url=host or None,
            mask=_mask_langfuse_payload, tracer_provider=TracerProvider(),
        )
        self._handler_class = RedactedCallback

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span | None]:
        new_run = _identifier(attributes.get("run_id"))
        raw_session = attributes.get("session_id")
        run = new_run or _run_id.get()
        session = _session_key(raw_session) if isinstance(raw_session, str) else _session_id.get()
        run_token = _run_id.set(run)
        session_token = _session_id.set(session)
        safe_attributes = {
            key: value for key, value in attributes.items()
            if key in _TRACE_FIELDS and (isinstance(value, (bool, int, float)) or _identifier(value))
        }
        if run:
            safe_attributes["run_id"] = run
        if session:
            safe_attributes["session_id"] = session

        manager = nullcontext()
        if self._tracer is not None:
            manager = self._tracer.start_as_current_span(
                name, attributes=safe_attributes, record_exception=False, set_status_on_exception=False,
            )
        try:
            with manager as span:
                try:
                    yield span
                except Exception as error:
                    error_type = type(error).__name__
                    if span is not None:
                        span.set_status(Status(StatusCode.ERROR))
                        span.set_attribute("error.type", error_type)
                    logger.warning("span=%s run_id=%s error_type=%s", name, run or "-", error_type)
                    raise
        finally:
            _session_id.reset(session_token)
            _run_id.reset(run_token)

    def observe(self, kind: str, name: str, outcome: str, duration: float = 0, tokens: int = 0) -> None:
        """Record metrics using code-owned labels. Durations are in seconds."""
        self._operations.labels(kind, name, outcome).inc()
        if duration > 0:
            self._durations.labels(kind, name, outcome).observe(duration)
        if tokens > 0:
            self._tokens.labels(kind, name).inc(tokens)
        if outcome in {"timeout", "error", "fallback", "degraded"}:
            span = get_current_span()
            if span.is_recording():
                span.add_event("ecommerce.operation", {"kind": kind, "operation": name, "outcome": outcome})
            if outcome in {"timeout", "error"}:
                logger.warning("kind=%s operation=%s outcome=%s run_id=%s", kind, name, outcome, _run_id.get() or "-")

    def metrics(self) -> bytes:
        return generate_latest(self._registry)

    def langchain_config(self, run_id: str = "", session_id: str = "") -> dict[str, Any]:
        if self._handler_class is None or self._langfuse is None:
            return {}
        run = _identifier(run_id) or _run_id.get()
        session = _session_key(session_id) or _session_id.get()
        metadata = {"run_id": run, "langfuse_session_id": session} if session else {"run_id": run}
        return {
            "callbacks": [self._handler_class(public_key=self._langfuse_public_key)],
            "metadata": metadata,
        }

    @contextmanager
    def langchain_span(self, run_id: str = "", session_id: str = "") -> Iterator[dict[str, Any]]:
        if self._langfuse is None:
            yield {}
            return

        run = _identifier(run_id) or _run_id.get()
        session = _session_key(session_id) or _session_id.get()
        run_token = _run_id.set(run)
        session_token = _session_id.set(session)
        failure = None
        try:
            context = {"trace_id": self._langfuse.create_trace_id(seed=run)} if run else None
            with self._langfuse.start_as_current_observation(
                name="ecommerce.model", as_type="span", trace_context=context,
            ) as observation:
                observation.update_trace(session_id=session or None, metadata={"run_id": run})
                try:
                    yield self.langchain_config()
                except Exception as error:
                    # Record the error class before leaving the SDK's context, then propagate the original error.
                    observation.update(level="ERROR", status_message=type(error).__name__)
                    failure = error
        finally:
            _session_id.reset(session_token)
            _run_id.reset(run_token)
        if failure is not None:
            raise failure

    def close(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
        if self._langfuse is not None:
            self._langfuse.flush()

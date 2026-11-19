"""Structured JSON logging and OpenTelemetry distributed tracing.

All operational telemetry across the multi-agent system uses this module:
- Machine-readable JSON logs aligned with Google Cloud Logging severity fields.
- Automatic PII redaction via ``PIIRedactor`` before any log record is emitted.
- OpenTelemetry distributed spans linking user requests, coordinator routing,
  sub-agent steps, tool invocations, and async memory consolidation.
- Zero bare stdout print calls anywhere in the agent runtime.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Iterator, Optional

from expense_agent.pii_redactor import PIIRedactor, get_pii_redactor

try:
  from opentelemetry import trace
  from opentelemetry.sdk.resources import Resource
  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import (
      BatchSpanProcessor,
      ConsoleSpanExporter,
      SimpleSpanProcessor,
      SpanExporter,
  )
  from opentelemetry.trace import Status, StatusCode

  _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback for bare stdlib test runs
  _OTEL_AVAILABLE = False
  trace = None  # type: ignore[assignment]
  Status = None  # type: ignore[assignment]
  StatusCode = None  # type: ignore[assignment]

SERVICE_NAME = "enterprise-expense-approval-agent"
_TRACING_CONFIGURED = False


class _NoOpSpan:
  """Lightweight span fallback used only when opentelemetry is uninstalled."""

  def set_attribute(self, key: str, value: Any) -> None:
    del key, value

  def set_status(self, status: Any) -> None:
    del status

  def record_exception(self, exc: BaseException) -> None:
    del exc


def configure_tracing(
    *,
    service_name: str = SERVICE_NAME,
    custom_exporter: Any = None,
) -> Any:
  """Initializes OpenTelemetry distributed tracing with GCP Cloud Trace support.

  When ``ENABLE_CLOUD_TRACE=true`` and ``GCP_PROJECT_ID`` is set, exports spans
  directly to Google Cloud Trace via ``CloudTraceSpanExporter``. Otherwise uses
  a local exporter (or an injected test exporter) so spans remain inspectable
  in development and automated CI runs.

  Args:
    service_name: Logical service name attached to the OpenTelemetry resource.
    custom_exporter: Optional span exporter (e.g., ``InMemorySpanExporter`` in
      automated tests).

  Returns:
    An OpenTelemetry ``Tracer`` instance (or ``None`` if OTel is unavailable).
  """
  global _TRACING_CONFIGURED
  if not _OTEL_AVAILABLE:
    return None

  if _TRACING_CONFIGURED and custom_exporter is None:
    return trace.get_tracer(service_name)

  resource = Resource.create(
      {
          "service.name": service_name,
          "service.version": "1.0.0",
          "deployment.environment": os.getenv("DEPLOYMENT_ENV", "local"),
      }
  )
  provider = TracerProvider(resource=resource)

  if custom_exporter is not None:
    provider.add_span_processor(SimpleSpanProcessor(custom_exporter))
  else:
    exporter: Optional[SpanExporter] = None
    enable_cloud_trace = (
        os.getenv("ENABLE_CLOUD_TRACE", "false").lower() == "true"
    )
    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    if enable_cloud_trace and project_id:
      try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        exporter = CloudTraceSpanExporter(project_id=project_id)
      except Exception:
        exporter = ConsoleSpanExporter()
    elif os.getenv("ENABLE_CONSOLE_TRACE", "false").lower() == "true":
      exporter = ConsoleSpanExporter()

    if exporter is not None:
      provider.add_span_processor(BatchSpanProcessor(exporter))

  trace.set_tracer_provider(provider)
  _TRACING_CONFIGURED = True
  return trace.get_tracer(service_name)


def get_current_trace_context() -> dict[str, Optional[str]]:
  """Extracts the active OpenTelemetry ``trace_id`` and ``span_id`` if present."""
  if not _OTEL_AVAILABLE:
    return {"trace_id": None, "span_id": None}

  span = trace.get_current_span()
  if span is None:
    return {"trace_id": None, "span_id": None}

  ctx = span.get_span_context()
  if not ctx or not ctx.is_valid:
    return {"trace_id": None, "span_id": None}

  return {
      "trace_id": format(ctx.trace_id, "032x"),
      "span_id": format(ctx.span_id, "016x"),
  }


@contextmanager
def start_linked_span(
    span_name: str,
    *,
    attributes: Optional[dict[str, Any]] = None,
    redactor: Optional[PIIRedactor] = None,
) -> Iterator[Any]:
  """Opens an OpenTelemetry child span with PII-redacted attributes.

  Args:
    span_name: Descriptive span name (e.g. ``"tool.validate_expense_receipt"``).
    attributes: Optional key-value metadata attached to the span after scrubbing.
    redactor: Optional ``PIIRedactor`` instance (defaults to global redactor).

  Yields:
    Active OpenTelemetry span object.
  """
  active_redactor = redactor or get_pii_redactor()
  if not _OTEL_AVAILABLE:
    yield _NoOpSpan()
    return

  tracer = trace.get_tracer(SERVICE_NAME)
  with tracer.start_as_current_span(span_name) as span:
    if attributes:
      safe_attrs = active_redactor.redact_payload(attributes)
      for key, value in safe_attrs.items():
        if isinstance(value, (str, int, float, bool)):
          span.set_attribute(key, value)
        else:
          span.set_attribute(key, json.dumps(value, default=str))
    try:
      yield span
    except Exception as exc:
      span.record_exception(exc)
      span.set_status(Status(StatusCode.ERROR, str(exc)))
      raise


class StructuredJsonFormatter(logging.Formatter):
  """Formats Python log records as Cloud Logging-compatible JSON with PII scrubbing."""

  def __init__(self, redactor: Optional[PIIRedactor] = None) -> None:
    super().__init__()
    self._redactor = redactor or get_pii_redactor()

  def format(self, record: logging.LogRecord) -> str:
    trace_ctx = get_current_trace_context()
    event_data = getattr(record, "event_data", {})
    safe_message = self._redactor.redact_text(record.getMessage())
    safe_data = self._redactor.redact_payload(event_data)

    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": record.levelname,
        "service": SERVICE_NAME,
        "logger": record.name,
        "event_type": getattr(record, "event_type", "application_log"),
        "message": safe_message,
        "trace_id": trace_ctx["trace_id"],
        "span_id": trace_ctx["span_id"],
        "data": safe_data,
    }
    if record.exc_info:
      payload["exception"] = self._redactor.redact_text(
          self.formatException(record.exc_info)
      )
    return json.dumps(payload, default=str, sort_keys=True)


def get_structured_logger(
    name: str = "expense_agent",
    *,
    redactor: Optional[PIIRedactor] = None,
) -> logging.Logger:
  """Returns a logger configured with PII-redacting structured JSON output."""
  logger = logging.getLogger(name)
  if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJsonFormatter(redactor=redactor))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
  return logger


def log_structured_event(
    logger: logging.Logger,
    *,
    event_type: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
    level: int = logging.INFO,
) -> None:
  """Emits a structured JSON log event with explicit event type and metadata.

  Args:
    logger: Structured logger returned by ``get_structured_logger``.
    event_type: Machine-parseable event category (e.g. ``"tool_intent"``,
      ``"tool_outcome"``, ``"guardrail_blocked"``, ``"async_memory_queued"``).
    message: Human-readable summary (automatically scrubbed for PII).
    data: Structured metadata payload (automatically scrubbed for PII).
    level: Python logging level (defaults to ``logging.INFO``).
  """
  logger.log(
      level,
      message,
      extra={
          "event_type": event_type,
          "event_data": data or {},
      },
  )

"""ADK Governance, Guardrail, Intent-vs-Outcome, and Async Memory Plugin.

Subclasses ``google.adk.plugins.base_plugin.BasePlugin`` to enforce global
AgentOps controls across every agent and tool in the runner:
1. **Security Guardrails (``before_model_callback``)**: Detects prompt-injection
   and policy-bypass attempts (with optional Google Cloud Model Armor hook)
   before prompts reach the LLM.
2. **Intent vs. Outcome Capture (``before_tool_callback`` &
   ``after_tool_callback``)**: Logs structured JSON records capturing the
   agent's *intended* tool action prior to execution and comparing it against
   the *actual* outcome and latency afterwards inside linked OpenTelemetry spans.
3. **Self-Evaluation Guardrail & Async Memory Consolidation
   (``after_agent_callback``)**: Verifies outputs contain zero unredacted PII
   and schedules non-blocking background memory persistence via
   ``AsyncMemoryConsolidator``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from expense_agent.memory_store import AsyncMemoryConsolidator
from expense_agent.observability import (
    get_structured_logger,
    log_structured_event,
    start_linked_span,
)
from expense_agent.pii_redactor import PIIRedactor, get_pii_redactor

try:
  from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover - allows offline unit testing without ADK wheel

  class BasePlugin:  # type: ignore[no-redef]
    """Minimal compatibility shim when google-adk is not installed locally."""

    def __init__(self, name: str) -> None:
      self.name = name


_LOGGER = get_structured_logger("expense_agent.plugins")

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"bypass\s+(the\s+)?(approval|guardrail|policy|threshold)", re.IGNORECASE),
    re.compile(r"override\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"auto[\s-]?approve\s+regardless\s+of\s+amount", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?(api\s+key|secret|system\s+prompt)", re.IGNORECASE),
)


class AgentOpsGovernancePlugin(BasePlugin):
  """Global ADK plugin enforcing security guardrails, observability, and async memory."""

  def __init__(
      self,
      *,
      name: str = "agentops_governance_plugin",
      redactor: Optional[PIIRedactor] = None,
      memory_consolidator: Optional[AsyncMemoryConsolidator] = None,
  ) -> None:
    super().__init__(name=name)
    self._redactor = redactor or get_pii_redactor()
    self._memory_consolidator = memory_consolidator
    self._tool_intent_tracker: dict[str, dict[str, Any]] = {}

  def detect_prompt_injection(self, text: str) -> Optional[str]:
    """Inspects user/model prompt text for policy-bypass or injection attempts.

    Args:
      text: Prompt text to inspect.

    Returns:
      Matched rule pattern string if a security violation is found, else ``None``.
    """
    if not text:
      return None
    for pattern in _PROMPT_INJECTION_PATTERNS:
      if pattern.search(text):
        return pattern.pattern
    return None

  @staticmethod
  def _extract_llm_request_text(llm_request: Any) -> str:
    """Safely extracts plain text from an ADK ``LlmRequest`` contents list."""
    parts_text: list[str] = []
    contents = getattr(llm_request, "contents", None) or []
    for content in contents:
      for part in getattr(content, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
          parts_text.append(text)
    return "\n".join(parts_text)

  async def before_model_callback(
      self, *, callback_context: Any, llm_request: Any
  ) -> Any:
    """Enforces input security guardrails before every LLM invocation."""
    agent_name = getattr(callback_context, "agent_name", "unknown_agent")
    prompt_text = self._extract_llm_request_text(llm_request)
    matched_rule = self.detect_prompt_injection(prompt_text)

    if matched_rule is not None:
      log_structured_event(
          _LOGGER,
          event_type="guardrail_prompt_injection_blocked",
          message="Blocked prompt injection / policy bypass attempt before LLM call.",
          data={
              "agent_name": agent_name,
              "matched_rule": matched_rule,
          },
          level=logging.WARNING,
      )
      try:
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            "Security Guardrail Triggered: I cannot override "
                            "corporate expense policies, bypass approval "
                            "thresholds, or disclose internal secrets. Please "
                            "submit a standard expense claim or policy question."
                        )
                    )
                ],
            )
        )
      except ImportError:
        return {
            "blocked": True,
            "reason": "Security Guardrail Triggered: policy bypass attempt detected.",
        }

    log_structured_event(
        _LOGGER,
        event_type="model_request_authorized",
        message="Input guardrail check passed for model call.",
        data={
            "agent_name": agent_name,
            "model": getattr(llm_request, "model", "configured_default"),
        },
    )
    return None

  async def before_tool_callback(
      self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
  ) -> Optional[dict[str, Any]]:
    """Records explicit agent *intent* prior to executing any tool."""
    tool_name = getattr(tool, "name", str(tool))
    call_key = (
        getattr(tool_context, "function_call_id", None)
        or f"{tool_name}:{time.monotonic_ns()}"
    )
    agent_name = getattr(tool_context, "agent_name", "unknown_agent")
    sanitized_args = self._redactor.redact_payload(tool_args)

    intent_record = {
        "call_id": call_key,
        "agent_name": agent_name,
        "intended_tool": tool_name,
        "intended_args": sanitized_args,
        "started_monotonic": time.monotonic(),
    }
    self._tool_intent_tracker[call_key] = intent_record

    log_structured_event(
        _LOGGER,
        event_type="tool_intent",
        message=f"Agent '{agent_name}' intends to invoke tool '{tool_name}'.",
        data={
            "call_id": call_key,
            "agent_name": agent_name,
            "intended_tool": tool_name,
            "intended_args": sanitized_args,
        },
    )
    return None

  async def after_tool_callback(
      self,
      *,
      tool: Any,
      tool_args: dict[str, Any],
      tool_context: Any,
      result: dict[str, Any],
  ) -> Optional[dict[str, Any]]:
    """Records actual tool *outcome* and reconciles it against logged intent."""
    del tool_args
    tool_name = getattr(tool, "name", str(tool))
    call_key = getattr(tool_context, "function_call_id", None)
    intent_record = (
        self._tool_intent_tracker.pop(call_key, None) if call_key else None
    )

    elapsed_ms = (
        round((time.monotonic() - intent_record["started_monotonic"]) * 1000.0, 2)
        if intent_record
        else None
    )
    sanitized_result = self._redactor.redact_payload(result)
    outcome_status = (
        sanitized_result.get("status", "unknown")
        if isinstance(sanitized_result, dict)
        else "completed"
    )
    intent_fulfilled = outcome_status in ("success", "pending_human_approval")

    log_structured_event(
        _LOGGER,
        event_type="tool_outcome",
        message=(
            f"Tool '{tool_name}' completed with status '{outcome_status}' "
            f"(intent_fulfilled={intent_fulfilled})."
        ),
        data={
            "call_id": call_key,
            "intended_tool": (
                intent_record["intended_tool"] if intent_record else tool_name
            ),
            "actual_tool": tool_name,
            "outcome_status": outcome_status,
            "intent_fulfilled": intent_fulfilled,
            "latency_ms": elapsed_ms,
            "outcome_summary": sanitized_result,
        },
    )
    return None

  def run_self_eval_guardrail(self, response_text: str) -> dict[str, Any]:
    """Performs post-generation self-evaluation for PII leakage and policy safety.

    Args:
      response_text: Candidate response generated by an agent.

    Returns:
      Dictionary with ``passed`` boolean, list of ``violations``, and
      ``sanitized_text``.
    """
    violations: list[str] = []
    if self._redactor.contains_unredacted_pii(response_text):
      violations.append("UNREDACTED_PII_DETECTED_IN_OUTPUT")

    sanitized_text = self._redactor.redact_text(response_text)
    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "sanitized_text": sanitized_text,
    }

  async def after_agent_callback(
      self, *, agent: Any, callback_context: Any
  ) -> Any:
    """Runs output self-eval check and queues non-blocking async memory persistence."""
    agent_name = getattr(agent, "name", "unknown_agent")
    state = getattr(callback_context, "state", {})
    session = getattr(callback_context, "session", None)
    session_id = getattr(session, "id", "ephemeral-session")
    user_id = getattr(session, "user_id", "anonymous-user")

    with start_linked_span(
        "plugin.after_agent_governance",
        attributes={"agent.name": agent_name, "session.id": session_id},
        redactor=self._redactor,
    ):
      state_snapshot = dict(state) if hasattr(state, "items") else {}
      self_eval_result = self.run_self_eval_guardrail(str(state_snapshot))

      log_structured_event(
          _LOGGER,
          event_type="self_eval_guardrail_result",
          message=f"Self-eval guardrail completed for agent '{agent_name}'.",
          data={
              "agent_name": agent_name,
              "passed": self_eval_result["passed"],
              "violations": self_eval_result["violations"],
          },
      )

      if self._memory_consolidator is not None and state_snapshot:
        self._memory_consolidator.schedule_consolidation(
            session_id=session_id,
            user_id=user_id,
            raw_summary={
                "agent_name": agent_name,
                "last_settled_claim_id": state_snapshot.get(
                    "last_settled_claim_id"
                ),
                "last_settlement_decision": state_snapshot.get(
                    "last_settlement_decision"
                ),
                "last_audit_reference": state_snapshot.get(
                    "last_audit_reference"
                ),
            },
            adk_session=session,
        )
    return None

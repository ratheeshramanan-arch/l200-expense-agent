"""Multi-agent ADK orchestration with strategic Flash vs. Pro model routing.

Architecture:
- **Coordinator Pattern** (``root_agent``, ``gemini-2.5-flash``): Classifies
  user intent and routes either to the fast ``policy_qa_specialist`` or the
  three-stage ``expense_processing_pipeline``.
- **Sequential Pipeline Pattern** (``SequentialAgent``):
  1. ``receipt_intake_agent`` (``gemini-2.5-flash``) — fast extraction, policy
     lookup, budget check, and deterministic validation.
  2. ``policy_auditor_agent`` (``gemini-2.5-pro``) — deep compliance reasoning
     enforced with ``output_schema=AuditReviewOutput``.
  3. ``settlement_executor_agent`` (``gemini-2.5-flash``) — executes either
     ``auto_approve_standard_expense`` (< $500) or the Human-in-the-Loop
     ``escalate_high_value_expense_approval`` (>= $500).
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import LlmAgent, SequentialAgent

from expense_agent.prompts import (
    COORDINATOR_CONSTITUTION,
    POLICY_AUDITOR_CONSTITUTION,
    POLICY_QA_CONSTITUTION,
    RECEIPT_INTAKE_CONSTITUTION,
    SETTLEMENT_EXECUTOR_CONSTITUTION,
)
from expense_agent.schemas import AuditReviewOutput
from expense_agent.tools import (
    auto_approve_standard_expense,
    escalate_high_value_expense_approval,
    fetch_department_budget_balance,
    lookup_corporate_expense_policy,
    validate_expense_receipt_policy,
)

# Strategic Model Routing Registry
MODEL_FAST = "gemini-2.5-flash"
MODEL_REASONING = "gemini-2.5-pro"

TaskTier = Literal["fast_routing", "tool_execution", "deep_audit_reasoning"]


def select_model_for_task(task_tier: TaskTier) -> str:
  """Routes each agent role to the optimal Gemini model for cost and quality.

  Args:
    task_tier: Complexity tier of the agent's workload.

  Returns:
    Gemini model identifier (``gemini-2.5-flash`` for low-latency routing and
    tool invocation; ``gemini-2.5-pro`` for complex policy reasoning and audit).
  """
  if task_tier == "deep_audit_reasoning":
    return MODEL_REASONING
  return MODEL_FAST


# Stage 1: Fast intake, lookup, and validation (Flash)
receipt_intake_agent = LlmAgent(
    name="receipt_intake_agent",
    model=select_model_for_task("tool_execution"),
    description=(
        "Extracts expense details, checks corporate policy and department "
        "budgets, and runs receipt validation."
    ),
    instruction=RECEIPT_INTAKE_CONSTITUTION,
    tools=[
        lookup_corporate_expense_policy,
        fetch_department_budget_balance,
        validate_expense_receipt_policy,
    ],
    output_key="intake_validation_summary",
)

# Stage 2: Deep financial compliance audit (Pro + strict Pydantic output_schema)
policy_auditor_agent = LlmAgent(
    name="policy_auditor_agent",
    model=select_model_for_task("deep_audit_reasoning"),
    description=(
        "Performs deep compliance and risk reasoning over the validated claim "
        "and outputs a structured AuditReviewOutput decision."
    ),
    instruction=(
        f"{POLICY_AUDITOR_CONSTITUTION}\n\n"
        "Intake validation context:\n{intake_validation_summary?}"
    ),
    output_schema=AuditReviewOutput,
    output_key="audit_review_decision",
)

# Stage 3: Settlement & Human-in-the-Loop execution (Flash)
settlement_executor_agent = LlmAgent(
    name="settlement_executor_agent",
    model=select_model_for_task("tool_execution"),
    description=(
        "Executes standard auto-approval (<$500) or pauses for Human-in-the-Loop "
        "Finance Manager confirmation (>=$500)."
    ),
    instruction=(
        f"{SETTLEMENT_EXECUTOR_CONSTITUTION}\n\n"
        "Audit review decision:\n{audit_review_decision?}"
    ),
    tools=[
        auto_approve_standard_expense,
        escalate_high_value_expense_approval,
    ],
    output_key="final_settlement_report",
)

# Sequential Multi-Agent Pipeline for end-to-end claim processing
expense_processing_pipeline = SequentialAgent(
    name="expense_processing_pipeline",
    description=(
        "End-to-end three-stage expense workflow: Receipt Intake -> "
        "Gemini Pro Policy Audit -> Settlement / HITL Approval."
    ),
    sub_agents=[
        receipt_intake_agent,
        policy_auditor_agent,
        settlement_executor_agent,
    ],
)

# Fast Policy Q&A Specialist (Flash)
policy_qa_specialist = LlmAgent(
    name="policy_qa_specialist",
    model=select_model_for_task("fast_routing"),
    description=(
        "Answers employee questions about corporate expense policies, caps, "
        "receipt thresholds, and remaining department budgets."
    ),
    instruction=POLICY_QA_CONSTITUTION,
    tools=[
        lookup_corporate_expense_policy,
        fetch_department_budget_balance,
    ],
)

# Root Coordinator Agent (ADK standard entry point)
root_agent = LlmAgent(
    name="enterprise_expense_coordinator",
    model=select_model_for_task("fast_routing"),
    description=(
        "Root coordinator routing policy questions to policy_qa_specialist and "
        "expense submissions to the sequential expense_processing_pipeline."
    ),
    instruction=COORDINATOR_CONSTITUTION,
    sub_agents=[
        policy_qa_specialist,
        expense_processing_pipeline,
    ],
)

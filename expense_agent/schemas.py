"""Explicit Pydantic v2 JSON schemas for all tool inputs, outputs, and agent states.

Enforces strict argument validation (``extra="forbid"``) and deterministic
structured outputs so LLMs receive unambiguous JSON contracts and actionable
error guidance instead of unconstrained dictionaries.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExpenseCategory(str, Enum):
  """Supported corporate expense categories with individual policy caps."""

  TRAVEL = "travel"
  LODGING = "lodging"
  MEALS = "meals"
  SOFTWARE = "software"
  CLIENT_ENTERTAINMENT = "client_entertainment"
  OFFICE_SUPPLIES = "office_supplies"


class ExpenseDecisionStatus(str, Enum):
  """Lifecycle states for an expense claim evaluation."""

  AUTO_APPROVED = "auto_approved"
  PENDING_HUMAN_APPROVAL = "pending_human_approval"
  HUMAN_APPROVED = "human_approved"
  REJECTED = "rejected"
  NEEDS_CLARIFICATION = "needs_clarification"


class StructuredToolError(BaseModel):
  """Guided error response returned to the LLM instead of unhandled exceptions."""

  model_config = ConfigDict(extra="forbid")

  status: Literal["error"] = "error"
  error_code: str = Field(
      ...,
      description="Machine-readable error identifier (e.g. INVALID_AMOUNT).",
  )
  message: str = Field(
      ...,
      description="Clear explanation of what failed validation or execution.",
  )
  recovery_instructions: str = Field(
      ...,
      description=(
          "Step-by-step recovery guidance telling the LLM how to fix the "
          "arguments, ask the user for missing details, or invoke an alternate tool."
      ),
  )
  retryable: bool = Field(
      default=True,
      description="Indicates whether retrying with corrected arguments can succeed.",
  )


class PolicyLookupInput(BaseModel):
  """Validated input schema for searching corporate expense policies."""

  model_config = ConfigDict(extra="forbid")

  category: ExpenseCategory = Field(
      ...,
      description="Expense category to look up policy rules and caps for.",
  )
  query_topic: str = Field(
      default="general",
      min_length=2,
      max_length=120,
      description="Specific policy question (e.g. 'receipt threshold', 'per diem').",
  )


class PolicyLookupOutput(BaseModel):
  """Structured corporate policy guidance returned to the agent."""

  model_config = ConfigDict(extra="forbid")

  status: Literal["success"] = "success"
  category: ExpenseCategory
  policy_id: str = Field(..., description="Official policy clause identifier.")
  auto_approve_limit_usd: float = Field(
      ...,
      description="Maximum USD amount eligible for automated approval.",
  )
  hard_cap_usd: float = Field(
      ...,
      description="Maximum allowable USD amount without VP exception.",
  )
  receipt_required_over_usd: float = Field(
      ...,
      description="Minimum USD threshold requiring an itemized receipt ID.",
  )
  rules_summary: str = Field(
      ...,
      description="Plain-language summary of applicable compliance rules.",
  )


class DepartmentBudgetInput(BaseModel):
  """Validated input schema for querying remaining department budget."""

  model_config = ConfigDict(extra="forbid")

  department_code: str = Field(
      ...,
      min_length=2,
      max_length=32,
      description="Department code (e.g. 'ENG', 'SALES', 'OPS', 'FINANCE').",
  )


class DepartmentBudgetOutput(BaseModel):
  """Structured budget balance response from the persistent ledger."""

  model_config = ConfigDict(extra="forbid")

  status: Literal["success"] = "success"
  department_code: str
  quarterly_budget_usd: float = Field(..., ge=0)
  spent_to_date_usd: float = Field(..., ge=0)
  remaining_budget_usd: float = Field(..., ge=0)
  utilization_pct: float = Field(..., ge=0, le=1000)


class ExpenseClaimInput(BaseModel):
  """Strict input schema for validating an employee expense claim."""

  model_config = ConfigDict(extra="forbid")

  claim_id: str = Field(
      ...,
      pattern=r"^EXP-[0-9]{4,8}$",
      description="Unique expense claim identifier formatted as EXP-1001.",
  )
  department_code: str = Field(
      ...,
      min_length=2,
      max_length=32,
      description="Cost-center department code (ENG, SALES, OPS, FINANCE).",
  )
  category: ExpenseCategory = Field(
      ...,
      description="Expense category from the approved corporate taxonomy.",
  )
  amount_usd: float = Field(
      ...,
      gt=0,
      le=100000.0,
      description="Positive expense total in US Dollars.",
  )
  merchant_name: str = Field(
      ...,
      min_length=2,
      max_length=120,
      description="Merchant or vendor name listed on the receipt.",
  )
  business_justification: str = Field(
      ...,
      min_length=5,
      max_length=500,
      description="Concise business purpose for the expenditure.",
  )
  receipt_id: Optional[str] = Field(
      default=None,
      description="Itemized receipt reference (required for expenses over $25).",
  )


class PolicyValidationOutput(BaseModel):
  """Structured output of automated receipt and policy compliance checks."""

  model_config = ConfigDict(extra="forbid")

  status: Literal["success"] = "success"
  claim_id: str
  is_policy_compliant: bool
  requires_human_approval: bool
  policy_clause_cited: str
  risk_flags: list[str] = Field(default_factory=list)
  recommended_next_tool: Literal[
      "auto_approve_standard_expense",
      "escalate_high_value_expense_approval",
      "request_user_clarification",
  ]
  summary: str


class ExpenseSettlementOutput(BaseModel):
  """Immutable settlement record returned after approval, pause, or denial."""

  model_config = ConfigDict(extra="forbid")

  status: Literal["success", "pending_human_approval", "rejected"]
  claim_id: str
  decision: ExpenseDecisionStatus
  amount_usd: float
  approver_role: str
  policy_clause_cited: str
  audit_reference: str
  message: str


class AuditReviewOutput(BaseModel):
  """Structured output schema enforced on the Gemini Pro Policy Auditor agent."""

  model_config = ConfigDict(extra="forbid")

  claim_id: str = Field(..., description="Evaluated expense claim ID.")
  risk_level: Literal["LOW", "MEDIUM", "HIGH"] = Field(
      ...,
      description="Assessed financial and compliance risk level.",
  )
  policy_clause_cited: str = Field(
      ...,
      description="Exact corporate policy ID supporting the audit finding.",
  )
  requires_human_approval: bool = Field(
      ...,
      description="True whenever amount >= $500 or policy anomaly is detected.",
  )
  recommended_action: Literal[
      "AUTO_APPROVE",
      "ESCALATE_TO_HUMAN",
      "REJECT_OR_CLARIFY",
  ]
  audit_rationale: str = Field(
      ...,
      description="Concise, policy-grounded reasoning for the recommendation.",
  )

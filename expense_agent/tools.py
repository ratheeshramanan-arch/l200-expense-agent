"""Domain-specific ADK tools with strict schemas, guided errors, and HITL hooks.

All tools follow AgentOps best practices:
- Highly descriptive, unambiguous action names (e.g.
  ``auto_approve_standard_expense`` vs ``escalate_high_value_expense_approval``).
- Comprehensive Google-style docstrings describing purpose, parameters, return
  contracts, and failure recovery semantics.
- Strict Pydantic v2 input/output JSON schema validation.
- Guided error responses (``StructuredToolError``) returning actionable
  ``recovery_instructions`` back to the LLM instead of crashing.
- Explicit ADK Human-in-the-Loop (``tool_context.request_confirmation``) pause
  for high-stakes financial approvals >= $500 USD.
"""

from __future__ import annotations

from typing import Any, Optional
import uuid

from pydantic import ValidationError

from expense_agent.memory_store import PersistentExpenseLedger, get_expense_ledger
from expense_agent.observability import (
    get_structured_logger,
    log_structured_event,
    start_linked_span,
)
from expense_agent.prompts import (
    AUTO_APPROVE_THRESHOLD_USD,
    RECEIPT_REQUIRED_THRESHOLD_USD,
)
from expense_agent.schemas import (
    DepartmentBudgetInput,
    DepartmentBudgetOutput,
    ExpenseCategory,
    ExpenseClaimInput,
    ExpenseDecisionStatus,
    ExpenseSettlementOutput,
    PolicyLookupInput,
    PolicyLookupOutput,
    PolicyValidationOutput,
    StructuredToolError,
)

_LOGGER = get_structured_logger("expense_agent.tools")


def _build_validation_error_response(
    exc: ValidationError, *, recovery_instructions: str
) -> dict[str, Any]:
  """Converts a Pydantic ValidationError into a guided LLM recovery payload."""
  issues = "; ".join(
      f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
      for err in exc.errors()
  )
  return StructuredToolError(
      error_code="SCHEMA_VALIDATION_FAILED",
      message=f"Tool arguments failed strict schema validation: {issues}",
      recovery_instructions=recovery_instructions,
      retryable=True,
  ).model_dump(mode="json")


def lookup_corporate_expense_policy(
    category: str,
    query_topic: str = "general",
    *,
    ledger: Optional[PersistentExpenseLedger] = None,
) -> dict[str, Any]:
  """Retrieves official corporate expense policy rules and spending caps.

  Queries the persistent corporate policy datastore for the requested expense
  category so agents ground all decisions in exact policy clauses rather than
  assumptions.

  Args:
    category: Expense category name. Must be one of ``"travel"``, ``"lodging"``,
      ``"meals"``, ``"software"``, ``"client_entertainment"``, or
      ``"office_supplies"``.
    query_topic: Optional topic keyword clarifying what policy rule the user is
      asking about (for example ``"receipt threshold"`` or ``"auto approval"``).
    ledger: Optional persistent ledger dependency (injected in unit tests;
      omitted during LLM tool calls).

  Returns:
    Dictionary matching ``PolicyLookupOutput`` containing ``policy_id``,
    ``auto_approve_limit_usd``, ``hard_cap_usd``, ``receipt_required_over_usd``,
    and ``rules_summary``—or a ``StructuredToolError`` with recovery steps if
    the category is invalid.
  """
  active_ledger = ledger or get_expense_ledger()
  with start_linked_span(
      "tool.lookup_corporate_expense_policy",
      attributes={"expense.category": category, "policy.topic": query_topic},
  ):
    try:
      validated = PolicyLookupInput(
          category=ExpenseCategory(category.lower().strip()),
          query_topic=query_topic,
      )
    except (ValueError, ValidationError):
      valid_categories = ", ".join(c.value for c in ExpenseCategory)
      return StructuredToolError(
          error_code="UNKNOWN_EXPENSE_CATEGORY",
          message=f"Category '{category}' is not a recognized corporate expense category.",
          recovery_instructions=(
              f"Map the user's expense to one of the valid categories "
              f"[{valid_categories}] and call lookup_corporate_expense_policy again."
          ),
          retryable=True,
      ).model_dump(mode="json")

    row = active_ledger.get_policy(validated.category.value)
    if row is None:
      return StructuredToolError(
          error_code="POLICY_RECORD_NOT_FOUND",
          message=f"No policy record was found for '{validated.category.value}'.",
          recovery_instructions=(
              "Inform the user that the policy datastore is missing this "
              "category and escalate to Finance Operations."
          ),
          retryable=False,
      ).model_dump(mode="json")

    return PolicyLookupOutput(
        category=validated.category,
        policy_id=row["policy_id"],
        auto_approve_limit_usd=float(row["auto_approve_limit_usd"]),
        hard_cap_usd=float(row["hard_cap_usd"]),
        receipt_required_over_usd=float(row["receipt_required_over_usd"]),
        rules_summary=row["rules_summary"],
    ).model_dump(mode="json")


def fetch_department_budget_balance(
    department_code: str,
    *,
    ledger: Optional[PersistentExpenseLedger] = None,
) -> dict[str, Any]:
  """Fetches the live quarterly budget balance for a corporate department.

  Reads the persistent financial ledger to verify whether a cost center has
  sufficient remaining budget before an expense claim is approved or escalated.

  Args:
    department_code: Two-to-eight letter department cost-center code (such as
      ``"ENG"``, ``"SALES"``, ``"OPS"``, or ``"FINANCE"``).
    ledger: Optional persistent ledger dependency (injected in unit tests).

  Returns:
    Dictionary matching ``DepartmentBudgetOutput`` with ``quarterly_budget_usd``,
    ``spent_to_date_usd``, ``remaining_budget_usd``, and ``utilization_pct``—or
    a ``StructuredToolError`` listing valid department codes.
  """
  active_ledger = ledger or get_expense_ledger()
  with start_linked_span(
      "tool.fetch_department_budget_balance",
      attributes={"department.code": department_code},
  ):
    try:
      validated = DepartmentBudgetInput(
          department_code=department_code.upper().strip()
      )
    except ValidationError as exc:
      return _build_validation_error_response(
          exc,
          recovery_instructions=(
              "Ask the user for a valid department code (ENG, SALES, OPS, or "
              "FINANCE) and retry fetch_department_budget_balance."
          ),
      )

    row = active_ledger.get_department_budget(validated.department_code)
    if row is None:
      valid_depts = ", ".join(active_ledger.list_departments())
      return StructuredToolError(
          error_code="UNKNOWN_DEPARTMENT_CODE",
          message=f"Department code '{validated.department_code}' does not exist.",
          recovery_instructions=(
              f"Ask the user to confirm their cost center from valid codes "
              f"[{valid_depts}] and re-run fetch_department_budget_balance."
          ),
          retryable=True,
      ).model_dump(mode="json")

    quarterly_budget = float(row["quarterly_budget_usd"])
    spent = float(row["spent_to_date_usd"])
    remaining = max(0.0, round(quarterly_budget - spent, 2))
    utilization = (
        round((spent / quarterly_budget) * 100.0, 2)
        if quarterly_budget > 0
        else 100.0
    )

    return DepartmentBudgetOutput(
        department_code=validated.department_code,
        quarterly_budget_usd=quarterly_budget,
        spent_to_date_usd=spent,
        remaining_budget_usd=remaining,
        utilization_pct=utilization,
    ).model_dump(mode="json")


def validate_expense_receipt_policy(
    claim_id: str,
    department_code: str,
    category: str,
    amount_usd: float,
    merchant_name: str,
    business_justification: str,
    receipt_id: Optional[str] = None,
    *,
    ledger: Optional[PersistentExpenseLedger] = None,
) -> dict[str, Any]:
  """Validates an expense claim against schema rules, receipts, caps, and budgets.

  Performs deterministic pre-audit compliance verification and recommends the
  exact downstream settlement tool (``auto_approve_standard_expense``,
  ``escalate_high_value_expense_approval``, or ``request_user_clarification``).

  Args:
    claim_id: Unique claim ID matching pattern ``EXP-XXXX`` (e.g. ``"EXP-1042"``).
    department_code: Cost-center department code (``ENG``, ``SALES``, ``OPS``,
      or ``FINANCE``).
    category: Expense category from the approved corporate taxonomy.
    amount_usd: Strictly positive USD total from the receipt.
    merchant_name: Vendor or merchant name shown on the receipt.
    business_justification: Brief business reason explaining the purchase.
    receipt_id: Itemized receipt reference ID (mandatory when amount > $25 USD).
    ledger: Optional persistent ledger dependency (injected in unit tests).

  Returns:
    Dictionary matching ``PolicyValidationOutput`` detailing compliance status,
    whether human confirmation is required, cited policy clause, risk flags, and
    recommended next tool—or a ``StructuredToolError`` with recovery steps.
  """
  active_ledger = ledger or get_expense_ledger()
  with start_linked_span(
      "tool.validate_expense_receipt_policy",
      attributes={
          "expense.claim_id": claim_id,
          "expense.category": category,
          "expense.amount_usd": amount_usd,
      },
  ):
    try:
      claim = ExpenseClaimInput(
          claim_id=claim_id.strip(),
          department_code=department_code.upper().strip(),
          category=ExpenseCategory(category.lower().strip()),
          amount_usd=amount_usd,
          merchant_name=merchant_name.strip(),
          business_justification=business_justification.strip(),
          receipt_id=receipt_id.strip() if receipt_id else None,
      )
    except (ValueError, ValidationError) as exc:
      if isinstance(exc, ValidationError):
        return _build_validation_error_response(
            exc,
            recovery_instructions=(
                "Verify claim_id matches EXP-XXXX, amount_usd is a positive "
                "number, category is valid, and business_justification has at "
                "least 5 characters. Ask the user for missing details and retry."
            ),
        )
      return StructuredToolError(
          error_code="INVALID_EXPENSE_CLAIM",
          message=str(exc),
          recovery_instructions=(
              "Check the expense category and amount with the user, then retry "
              "validate_expense_receipt_policy."
          ),
          retryable=True,
      ).model_dump(mode="json")

    policy = active_ledger.get_policy(claim.category.value)
    budget = active_ledger.get_department_budget(claim.department_code)
    if policy is None or budget is None:
      return StructuredToolError(
          error_code="REFERENCE_DATA_MISSING",
          message=(
              f"Could not find policy for '{claim.category.value}' or budget "
              f"for department '{claim.department_code}'."
          ),
          recovery_instructions=(
              "Call lookup_corporate_expense_policy and "
              "fetch_department_budget_balance first to confirm valid identifiers."
          ),
          retryable=True,
      ).model_dump(mode="json")

    risk_flags: list[str] = []
    receipt_threshold = float(policy["receipt_required_over_usd"])
    effective_auto_limit = min(
        AUTO_APPROVE_THRESHOLD_USD, float(policy["auto_approve_limit_usd"])
    )
    hard_cap = float(policy["hard_cap_usd"])
    remaining_budget = float(budget["quarterly_budget_usd"]) - float(
        budget["spent_to_date_usd"]
    )

    if claim.amount_usd > max(receipt_threshold, RECEIPT_REQUIRED_THRESHOLD_USD) and not claim.receipt_id:
      risk_flags.append("MISSING_REQUIRED_RECEIPT_ID")
    if claim.amount_usd > hard_cap:
      risk_flags.append("EXCEEDS_CATEGORY_HARD_CAP")
    if claim.amount_usd > remaining_budget:
      risk_flags.append("EXCEEDS_REMAINING_DEPARTMENT_BUDGET")

    if "MISSING_REQUIRED_RECEIPT_ID" in risk_flags or "EXCEEDS_CATEGORY_HARD_CAP" in risk_flags:
      return PolicyValidationOutput(
          claim_id=claim.claim_id,
          is_policy_compliant=False,
          requires_human_approval=True,
          policy_clause_cited=policy["policy_id"],
          risk_flags=risk_flags,
          recommended_next_tool="request_user_clarification",
          summary=(
              f"Claim {claim.claim_id} failed compliance checks ({', '.join(risk_flags)}) "
              f"under {policy['policy_id']}."
          ),
      ).model_dump(mode="json")

    requires_human = (
        claim.amount_usd >= effective_auto_limit
        or claim.amount_usd >= AUTO_APPROVE_THRESHOLD_USD
        or bool(risk_flags)
    )
    if claim.amount_usd >= AUTO_APPROVE_THRESHOLD_USD:
      risk_flags.append("HIGH_VALUE_REQUIRES_MANAGER_CONFIRMATION")

    next_tool = (
        "escalate_high_value_expense_approval"
        if requires_human
        else "auto_approve_standard_expense"
    )

    return PolicyValidationOutput(
        claim_id=claim.claim_id,
        is_policy_compliant=True,
        requires_human_approval=requires_human,
        policy_clause_cited=policy["policy_id"],
        risk_flags=risk_flags,
        recommended_next_tool=next_tool,
        summary=(
            f"Claim {claim.claim_id} (${claim.amount_usd:.2f}, {claim.category.value}) "
            f"evaluated under {policy['policy_id']}. Next action: {next_tool}."
        ),
    ).model_dump(mode="json")


def auto_approve_standard_expense(
    claim_id: str,
    department_code: str,
    amount_usd: float,
    policy_clause_cited: str,
    tool_context: Any = None,
    *,
    ledger: Optional[PersistentExpenseLedger] = None,
) -> dict[str, Any]:
  """Automatically approves a compliant standard expense strictly under $500 USD.

  Enforces a hard financial guardrail in code: any attempt to auto-approve a
  claim >= $500 USD is blocked and returns a guided error instructing the LLM
  to invoke ``escalate_high_value_expense_approval`` for human confirmation.

  Args:
    claim_id: Unique validated claim ID (e.g. ``"EXP-1042"``).
    department_code: Validated department code (``ENG``, ``SALES``, ``OPS``,
      or ``FINANCE``).
    amount_usd: Positive USD total strictly less than $500.00.
    policy_clause_cited: Official policy ID justifying approval (e.g.
      ``"POL-TRV-001"``).
    tool_context: ADK ``ToolContext`` automatically injected by the runtime to
      update persistent session state.
    ledger: Optional persistent ledger dependency (injected in unit tests).

  Returns:
    Dictionary matching ``ExpenseSettlementOutput`` with an immutable audit
    reference, or ``StructuredToolError`` if the claim violates the auto-approve
    threshold.
  """
  active_ledger = ledger or get_expense_ledger()
  with start_linked_span(
      "tool.auto_approve_standard_expense",
      attributes={
          "expense.claim_id": claim_id,
          "expense.amount_usd": amount_usd,
          "policy.clause": policy_clause_cited,
      },
  ):
    if amount_usd <= 0:
      return StructuredToolError(
          error_code="INVALID_AMOUNT",
          message=f"Cannot approve non-positive amount ${amount_usd:.2f}.",
          recovery_instructions=(
              "Ask the user for the positive USD total on the receipt and "
              "re-run validate_expense_receipt_policy."
          ),
          retryable=True,
      ).model_dump(mode="json")

    if amount_usd >= AUTO_APPROVE_THRESHOLD_USD:
      return StructuredToolError(
          error_code="AUTO_APPROVE_THRESHOLD_EXCEEDED",
          message=(
              f"Amount ${amount_usd:.2f} meets or exceeds the "
              f"${AUTO_APPROVE_THRESHOLD_USD:.2f} auto-approval guardrail."
          ),
          recovery_instructions=(
              "Do NOT retry auto_approve_standard_expense. Immediately call "
              "escalate_high_value_expense_approval so a human Finance Manager "
              "can confirm or reject the claim."
          ),
          retryable=False,
      ).model_dump(mode="json")

    audit_ref = f"AUD-{uuid.uuid4().hex[:8].upper()}"
    active_ledger.record_settlement(
        audit_reference=audit_ref,
        claim_id=claim_id,
        department_code=department_code,
        amount_usd=amount_usd,
        decision=ExpenseDecisionStatus.AUTO_APPROVED.value,
        approver_role="automated_policy_engine",
        policy_clause_cited=policy_clause_cited,
        deduct_budget=True,
    )

    if tool_context is not None and hasattr(tool_context, "state"):
      tool_context.state["last_settled_claim_id"] = claim_id
      tool_context.state["last_settlement_decision"] = (
          ExpenseDecisionStatus.AUTO_APPROVED.value
      )
      tool_context.state["last_audit_reference"] = audit_ref

    log_structured_event(
        _LOGGER,
        event_type="expense_auto_approved",
        message="Standard expense claim auto-approved and persisted to ledger.",
        data={
            "claim_id": claim_id,
            "department_code": department_code.upper(),
            "amount_usd": amount_usd,
            "audit_reference": audit_ref,
            "policy_clause_cited": policy_clause_cited,
        },
    )

    return ExpenseSettlementOutput(
        status="success",
        claim_id=claim_id,
        decision=ExpenseDecisionStatus.AUTO_APPROVED,
        amount_usd=amount_usd,
        approver_role="automated_policy_engine",
        policy_clause_cited=policy_clause_cited,
        audit_reference=audit_ref,
        message=(
            f"Claim {claim_id} for ${amount_usd:.2f} was automatically approved "
            f"under {policy_clause_cited} (Audit Ref: {audit_ref})."
        ),
    ).model_dump(mode="json")


def escalate_high_value_expense_approval(
    claim_id: str,
    department_code: str,
    amount_usd: float,
    policy_clause_cited: str,
    escalation_reason: str,
    tool_context: Any = None,
    *,
    ledger: Optional[PersistentExpenseLedger] = None,
) -> dict[str, Any]:
  """Triggers an explicit Human-in-the-Loop confirmation pause for high-stakes claims.

  Before disbursing or approving any high-value expense (>= $500 USD) or
  flagged claim, this tool halts execution using ADK's native
  ``tool_context.request_confirmation(...)`` hook. Execution resumes only after
  a human Finance Manager explicitly confirms or denies the payload.

  Args:
    claim_id: Unique expense claim identifier (e.g. ``"EXP-2099"``).
    department_code: Cost-center department code (``ENG``, ``SALES``, ``OPS``,
      or ``FINANCE``).
    amount_usd: Positive USD claim total requiring human approval.
    policy_clause_cited: Exact corporate policy ID (e.g. ``"POL-TRV-001"``).
    escalation_reason: Concise explanation of why human sign-off is required.
    tool_context: ADK ``ToolContext`` providing ``request_confirmation`` and
      ``tool_confirmation`` for Human-in-the-Loop resumability.
    ledger: Optional persistent ledger dependency (injected in unit tests).

  Returns:
    Dictionary matching ``ExpenseSettlementOutput`` indicating either:
    - ``pending_human_approval`` while halted awaiting human manager decision,
    - ``human_approved`` once explicitly confirmed, or
    - ``rejected`` if the human reviewer declined the expense.
  """
  active_ledger = ledger or get_expense_ledger()
  with start_linked_span(
      "tool.escalate_high_value_expense_approval",
      attributes={
          "expense.claim_id": claim_id,
          "expense.amount_usd": amount_usd,
          "hitl.required": True,
      },
  ):
    if amount_usd <= 0:
      return StructuredToolError(
          error_code="INVALID_AMOUNT",
          message=f"Cannot escalate non-positive amount ${amount_usd:.2f}.",
          recovery_instructions=(
              "Confirm a positive USD amount with the user before escalating."
          ),
          retryable=True,
      ).model_dump(mode="json")

    confirmation = (
        getattr(tool_context, "tool_confirmation", None)
        if tool_context is not None
        else None
    )

    # Hard Human-in-the-Loop stop: pause execution until a human explicitly decides.
    if confirmation is None:
      confirmation_payload = {
          "claim_id": claim_id,
          "department_code": department_code.upper(),
          "amount_usd": amount_usd,
          "policy_clause_cited": policy_clause_cited,
          "escalation_reason": escalation_reason,
          "required_approver_role": "finance_manager",
      }
      hint = (
          f"Human Finance Manager confirmation required to approve expense "
          f"{claim_id} for ${amount_usd:.2f} ({department_code.upper()}). "
          f"Reason: {escalation_reason}"
      )
      if tool_context is not None and hasattr(tool_context, "request_confirmation"):
        tool_context.request_confirmation(
            hint=hint,
            payload=confirmation_payload,
        )

      pending_ref = f"PEND-{uuid.uuid4().hex[:8].upper()}"
      log_structured_event(
          _LOGGER,
          event_type="hitl_confirmation_requested",
          message="Paused high-value expense approval pending human confirmation.",
          data={
              "claim_id": claim_id,
              "amount_usd": amount_usd,
              "pending_reference": pending_ref,
          },
      )
      return ExpenseSettlementOutput(
          status="pending_human_approval",
          claim_id=claim_id,
          decision=ExpenseDecisionStatus.PENDING_HUMAN_APPROVAL,
          amount_usd=amount_usd,
          approver_role="finance_manager",
          policy_clause_cited=policy_clause_cited,
          audit_reference=pending_ref,
          message=hint,
      ).model_dump(mode="json")

    audit_ref = f"HITL-{uuid.uuid4().hex[:8].upper()}"
    if getattr(confirmation, "confirmed", False):
      active_ledger.record_settlement(
          audit_reference=audit_ref,
          claim_id=claim_id,
          department_code=department_code,
          amount_usd=amount_usd,
          decision=ExpenseDecisionStatus.HUMAN_APPROVED.value,
          approver_role="finance_manager",
          policy_clause_cited=policy_clause_cited,
          deduct_budget=True,
      )
      if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state["last_settled_claim_id"] = claim_id
        tool_context.state["last_settlement_decision"] = (
            ExpenseDecisionStatus.HUMAN_APPROVED.value
        )
        tool_context.state["last_audit_reference"] = audit_ref

      return ExpenseSettlementOutput(
          status="success",
          claim_id=claim_id,
          decision=ExpenseDecisionStatus.HUMAN_APPROVED,
          amount_usd=amount_usd,
          approver_role="finance_manager",
          policy_clause_cited=policy_clause_cited,
          audit_reference=audit_ref,
          message=(
              f"Human Finance Manager approved claim {claim_id} for "
              f"${amount_usd:.2f} under {policy_clause_cited} (Audit Ref: {audit_ref})."
          ),
      ).model_dump(mode="json")

    # Human reviewer explicitly rejected the high-value claim.
    active_ledger.record_settlement(
        audit_reference=audit_ref,
        claim_id=claim_id,
        department_code=department_code,
        amount_usd=amount_usd,
        decision=ExpenseDecisionStatus.REJECTED.value,
        approver_role="finance_manager",
        policy_clause_cited=policy_clause_cited,
        deduct_budget=False,
    )
    return ExpenseSettlementOutput(
        status="rejected",
        claim_id=claim_id,
        decision=ExpenseDecisionStatus.REJECTED,
        amount_usd=amount_usd,
        approver_role="finance_manager",
        policy_clause_cited=policy_clause_cited,
        audit_reference=audit_ref,
        message=(
            f"Human Finance Manager rejected claim {claim_id} for "
            f"${amount_usd:.2f} (Audit Ref: {audit_ref})."
        ),
    ).model_dump(mode="json")

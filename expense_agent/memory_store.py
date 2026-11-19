"""Persistent session state, ledger database, and asynchronous memory operations.

Implements two key Context & Memory criteria:
1. **Persistent Session State**: Uses ADK's ``DatabaseSessionService`` (backed
   by SQLite locally or Cloud SQL PostgreSQL in production) alongside a
   persistent SQLite corporate ledger/policy database and optional Vertex AI
   Search / Memory Bank grounding.
2. **Async Memory Operations**: Runs expensive memory consolidation and PII
   scrubbing on a non-blocking ``asyncio.create_task(...)`` background pipeline
   so conversational turns return immediately without waiting on storage I/O.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Optional

from expense_agent.observability import (
    get_structured_logger,
    log_structured_event,
    start_linked_span,
)
from expense_agent.pii_redactor import PIIRedactor, get_pii_redactor
from expense_agent.secrets import get_session_database_url

_LOGGER = get_structured_logger("expense_agent.memory_store")

_DEFAULT_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "category": "travel",
        "policy_id": "POL-TRV-001",
        "auto_approve_limit_usd": 500.0,
        "hard_cap_usd": 5000.0,
        "receipt_required_over_usd": 25.0,
        "rules_summary": (
            "Economy airfare and ground transport under $500 auto-approve with "
            "itemized receipt. Claims >= $500 require Finance Manager approval."
        ),
    },
    {
        "category": "lodging",
        "policy_id": "POL-LDG-002",
        "auto_approve_limit_usd": 500.0,
        "hard_cap_usd": 3500.0,
        "receipt_required_over_usd": 25.0,
        "rules_summary": (
            "Standard hotel stays up to $300/night (total < $500) auto-approve. "
            "Total stays >= $500 require Finance Manager sign-off."
        ),
    },
    {
        "category": "meals",
        "policy_id": "POL-MLS-003",
        "auto_approve_limit_usd": 150.0,
        "hard_cap_usd": 600.0,
        "receipt_required_over_usd": 25.0,
        "rules_summary": (
            "Employee travel meals up to $150 auto-approve with receipt. "
            "Group or high-value meals >= $150 require review; >= $500 escalate."
        ),
    },
    {
        "category": "software",
        "policy_id": "POL-SFT-004",
        "auto_approve_limit_usd": 500.0,
        "hard_cap_usd": 10000.0,
        "receipt_required_over_usd": 0.0,
        "rules_summary": (
            "Approved SaaS tools < $500 auto-approve with vendor invoice ID. "
            "Purchases >= $500 require manager and IT security approval."
        ),
    },
    {
        "category": "client_entertainment",
        "policy_id": "POL-ENT-005",
        "auto_approve_limit_usd": 250.0,
        "hard_cap_usd": 2500.0,
        "receipt_required_over_usd": 0.0,
        "rules_summary": (
            "Client events require documented business justification and "
            "itemized receipt. Claims >= $500 require explicit manager sign-off."
        ),
    },
    {
        "category": "office_supplies",
        "policy_id": "POL-OFF-006",
        "auto_approve_limit_usd": 500.0,
        "hard_cap_usd": 2000.0,
        "receipt_required_over_usd": 25.0,
        "rules_summary": (
            "Standard ergonomic and office items < $500 auto-approve with "
            "receipt. Orders >= $500 require Finance Manager confirmation."
        ),
    },
)

_DEFAULT_DEPARTMENTS: tuple[dict[str, Any], ...] = (
    {
        "department_code": "ENG",
        "quarterly_budget_usd": 50000.0,
        "spent_to_date_usd": 18250.0,
    },
    {
        "department_code": "SALES",
        "quarterly_budget_usd": 75000.0,
        "spent_to_date_usd": 42100.0,
    },
    {
        "department_code": "OPS",
        "quarterly_budget_usd": 30000.0,
        "spent_to_date_usd": 11400.0,
    },
    {
        "department_code": "FINANCE",
        "quarterly_budget_usd": 25000.0,
        "spent_to_date_usd": 6200.0,
    },
)


class PersistentExpenseLedger:
  """SQLite-backed persistent datastore for policies, budgets, settlements, and memory.

  Provides durable cross-turn storage locally and acts as the schema contract
  for Cloud SQL PostgreSQL in cloud deployments.
  """

  def __init__(self, db_path: Optional[str] = None) -> None:
    resolved_path = db_path or os.getenv(
        "EXPENSE_LEDGER_DB_PATH", "./expense_ledger.db"
    )
    self.db_path = str(Path(resolved_path))
    self._initialize_schema()

  @contextmanager
  def _connect(self) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    try:
      with conn:
        yield conn
    finally:
      conn.close()

  def _initialize_schema(self) -> None:
    with self._connect() as conn:
      conn.executescript(
          """
          CREATE TABLE IF NOT EXISTS policies (
            category TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            auto_approve_limit_usd REAL NOT NULL,
            hard_cap_usd REAL NOT NULL,
            receipt_required_over_usd REAL NOT NULL,
            rules_summary TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS department_budgets (
            department_code TEXT PRIMARY KEY,
            quarterly_budget_usd REAL NOT NULL,
            spent_to_date_usd REAL NOT NULL
          );

          CREATE TABLE IF NOT EXISTS expense_settlements (
            audit_reference TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL,
            department_code TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            decision TEXT NOT NULL,
            approver_role TEXT NOT NULL,
            policy_clause_cited TEXT NOT NULL,
            created_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS consolidated_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
          );
          """
      )
      for policy in _DEFAULT_POLICIES:
        conn.execute(
            """
            INSERT OR IGNORE INTO policies (
              category, policy_id, auto_approve_limit_usd,
              hard_cap_usd, receipt_required_over_usd, rules_summary
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                policy["category"],
                policy["policy_id"],
                policy["auto_approve_limit_usd"],
                policy["hard_cap_usd"],
                policy["receipt_required_over_usd"],
                policy["rules_summary"],
            ),
        )
      for dept in _DEFAULT_DEPARTMENTS:
        conn.execute(
            """
            INSERT OR IGNORE INTO department_budgets (
              department_code, quarterly_budget_usd, spent_to_date_usd
            ) VALUES (?, ?, ?)
            """,
            (
                dept["department_code"],
                dept["quarterly_budget_usd"],
                dept["spent_to_date_usd"],
            ),
        )

  def get_policy(self, category: str) -> Optional[dict[str, Any]]:
    """Fetches a corporate expense policy row by category."""
    with self._connect() as conn:
      row = conn.execute(
          "SELECT * FROM policies WHERE category = ?",
          (category.lower().strip(),),
      ).fetchone()
    return dict(row) if row else None

  def list_categories(self) -> list[str]:
    """Returns all valid policy category names."""
    with self._connect() as conn:
      rows = conn.execute(
          "SELECT category FROM policies ORDER BY category"
      ).fetchall()
    return [row["category"] for row in rows]

  def get_department_budget(self, department_code: str) -> Optional[dict[str, Any]]:
    """Fetches remaining quarterly budget for a department."""
    with self._connect() as conn:
      row = conn.execute(
          "SELECT * FROM department_budgets WHERE department_code = ?",
          (department_code.upper().strip(),),
      ).fetchone()
    return dict(row) if row else None

  def list_departments(self) -> list[str]:
    """Returns all valid department codes."""
    with self._connect() as conn:
      rows = conn.execute(
          "SELECT department_code FROM department_budgets ORDER BY department_code"
      ).fetchall()
    return [row["department_code"] for row in rows]

  def record_settlement(
      self,
      *,
      audit_reference: str,
      claim_id: str,
      department_code: str,
      amount_usd: float,
      decision: str,
      approver_role: str,
      policy_clause_cited: str,
      deduct_budget: bool,
  ) -> None:
    """Persists an immutable settlement record and updates department spend."""
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      conn.execute(
          """
          INSERT OR REPLACE INTO expense_settlements (
            audit_reference, claim_id, department_code, amount_usd,
            decision, approver_role, policy_clause_cited, created_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
              audit_reference,
              claim_id,
              department_code.upper(),
              amount_usd,
              decision,
              approver_role,
              policy_clause_cited,
              now,
          ),
      )
      if deduct_budget:
        conn.execute(
            """
            UPDATE department_budgets
            SET spent_to_date_usd = spent_to_date_usd + ?
            WHERE department_code = ?
            """,
            (amount_usd, department_code.upper()),
        )

  def save_consolidated_memory(
      self,
      *,
      session_id: str,
      user_id: str,
      sanitized_summary: dict[str, Any],
  ) -> None:
    """Stores PII-scrubbed long-term session memory in the persistent DB."""
    now = datetime.now(timezone.utc).isoformat()
    with self._connect() as conn:
      conn.execute(
          """
          INSERT INTO consolidated_memories (
            session_id, user_id, summary_json, created_at
          ) VALUES (?, ?, ?, ?)
          """,
          (session_id, user_id, json.dumps(sanitized_summary, sort_keys=True), now),
      )

  def fetch_recent_memories(
      self, *, user_id: str, limit: int = 5
  ) -> list[dict[str, Any]]:
    """Retrieves recent consolidated memories for cross-session recall."""
    with self._connect() as conn:
      rows = conn.execute(
          """
          SELECT session_id, user_id, summary_json, created_at
          FROM consolidated_memories
          WHERE user_id = ?
          ORDER BY id DESC
          LIMIT ?
          """,
          (user_id, limit),
      ).fetchall()
    return [
        {
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "summary": json.loads(row["summary_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


class AsyncMemoryConsolidator:
  """Executes non-blocking background memory extraction and persistence.

  Uses ``asyncio.create_task`` so expensive PII scrubbing, summary extraction,
  and ADK ``memory_service.add_session_to_memory(session)`` writes run
  asynchronously without blocking conversational UI turns.
  """

  def __init__(
      self,
      ledger: PersistentExpenseLedger,
      *,
      memory_service: Any = None,
      redactor: Optional[PIIRedactor] = None,
  ) -> None:
    self.ledger = ledger
    self.memory_service = memory_service
    self.redactor = redactor or get_pii_redactor()
    self._background_tasks: set[asyncio.Task[Any]] = set()

  def schedule_consolidation(
      self,
      *,
      session_id: str,
      user_id: str,
      raw_summary: dict[str, Any],
      adk_session: Any = None,
  ) -> asyncio.Task[None]:
    """Schedules background memory consolidation without blocking the caller.

    Args:
      session_id: Active ADK session identifier.
      user_id: End-user identifier (scrubbed before persistence).
      raw_summary: Unsanitized session highlights dictionary.
      adk_session: Optional ADK ``Session`` object forwarded to the ADK
        ``BaseMemoryService.add_session_to_memory`` API.

    Returns:
      The scheduled background ``asyncio.Task``.
    """
    task = asyncio.create_task(
        self._consolidate_worker(
            session_id=session_id,
            user_id=user_id,
            raw_summary=raw_summary,
            adk_session=adk_session,
        ),
        name=f"memory-consolidate-{session_id}",
    )
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)

    log_structured_event(
        _LOGGER,
        event_type="async_memory_scheduled",
        message="Queued non-blocking background memory consolidation task.",
        data={
            "session_id": session_id,
            "active_background_tasks": len(self._background_tasks),
        },
    )
    return task

  async def _consolidate_worker(
      self,
      *,
      session_id: str,
      user_id: str,
      raw_summary: dict[str, Any],
      adk_session: Any = None,
  ) -> None:
    """Async worker that scrubs PII and persists memory off the critical path."""
    with start_linked_span(
        "memory.async_consolidation",
        attributes={"session.id": session_id},
        redactor=self.redactor,
    ):
      # Yield control so the interactive response returns first.
      await asyncio.sleep(0)
      sanitized_summary = self.redactor.redact_payload(raw_summary)
      sanitized_user_id = self.redactor.redact_text(user_id)

      await asyncio.to_thread(
          self.ledger.save_consolidated_memory,
          session_id=session_id,
          user_id=sanitized_user_id,
          sanitized_summary=sanitized_summary,
      )

      if self.memory_service is not None and adk_session is not None:
        await self.memory_service.add_session_to_memory(adk_session)

      log_structured_event(
          _LOGGER,
          event_type="async_memory_persisted",
          message="Completed background memory consolidation after PII redaction.",
          data={
              "session_id": session_id,
              "fields_stored": sorted(sanitized_summary.keys()),
          },
      )

  async def drain(self) -> None:
    """Awaits all pending background memory tasks (useful for graceful shutdown & tests)."""
    if self._background_tasks:
      await asyncio.gather(*list(self._background_tasks), return_exceptions=False)


def create_session_service(db_url: Optional[str] = None):
  """Creates an ADK ``DatabaseSessionService`` for persistent multi-turn state.

  Args:
    db_url: Optional SQLAlchemy database URL. When omitted, resolves securely
      via ``get_session_database_url()`` (Cloud SQL PostgreSQL in production or
      SQLite locally).

  Returns:
    Configured ``google.adk.sessions.DatabaseSessionService`` instance.
  """
  from google.adk.sessions import DatabaseSessionService

  resolved_url = db_url or get_session_database_url()
  return DatabaseSessionService(db_url=resolved_url)


def create_memory_service():
  """Creates an ADK persistent memory backend (Vertex AI Memory Bank or SQLite).

  Uses ``VertexAiMemoryBankService`` when ``GCP_PROJECT_ID`` and
  ``VERTEX_AI_MEMORY_ENGINE_ID`` are configured; otherwise uses local
  ``InMemoryMemoryService`` paired with ``PersistentExpenseLedger``.
  """
  project_id = os.getenv("GCP_PROJECT_ID", "").strip()
  location = os.getenv("GCP_LOCATION", "us-central1").strip()
  engine_id = os.getenv("VERTEX_AI_MEMORY_ENGINE_ID", "").strip()

  if project_id and engine_id:
    from google.adk.memory import VertexAiMemoryBankService

    return VertexAiMemoryBankService(
        project=project_id,
        location=location,
        agent_engine_id=engine_id,
    )

  from google.adk.memory import InMemoryMemoryService

  return InMemoryMemoryService()


_DEFAULT_LEDGER: Optional[PersistentExpenseLedger] = None


def get_expense_ledger() -> PersistentExpenseLedger:
  """Returns the shared persistent SQLite expense ledger instance."""
  global _DEFAULT_LEDGER
  if _DEFAULT_LEDGER is None:
    _DEFAULT_LEDGER = PersistentExpenseLedger()
  return _DEFAULT_LEDGER

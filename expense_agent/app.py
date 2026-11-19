"""Top-level ADK App & Runner configuration.

Wires together:
- ``EventsCompactionConfig`` (token-threshold + sliding-window history compaction)
- ``ContextCacheConfig`` (prompt prefix context caching)
- ``ResumabilityConfig`` (enables Human-in-the-Loop confirmation pause/resume)
- ``AgentOpsGovernancePlugin`` (guardrails, Intent vs. Outcome logging, async memory)
- ``DatabaseSessionService`` (persistent multi-turn session storage)
- ``VertexAiMemoryBankService`` / ``InMemoryMemoryService`` (long-term memory)
- OpenTelemetry distributed tracing & Secret Manager runtime bootstrap
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App, EventsCompactionConfig, ResumabilityConfig
from google.adk.runners import Runner

from expense_agent.agent import root_agent
from expense_agent.memory_store import (
    AsyncMemoryConsolidator,
    create_memory_service,
    create_session_service,
    get_expense_ledger,
)
from expense_agent.observability import (
    configure_tracing,
    get_structured_logger,
    log_structured_event,
)
from expense_agent.plugins import AgentOpsGovernancePlugin
from expense_agent.secrets import configure_runtime_secrets

APP_NAME = "enterprise_expense_approval_agent"
_LOGGER = get_structured_logger("expense_agent.app")


def build_app(
    *,
    memory_consolidator: Optional[AsyncMemoryConsolidator] = None,
) -> App:
  """Builds the production ADK ``App`` with compaction, caching, and plugins.

  Args:
    memory_consolidator: Optional ``AsyncMemoryConsolidator`` for background
      memory persistence. Defaults to one wired to the shared SQLite ledger.

  Returns:
    Fully configured ADK ``App`` instance.
  """
  consolidator = memory_consolidator or AsyncMemoryConsolidator(
      ledger=get_expense_ledger(),
  )
  governance_plugin = AgentOpsGovernancePlugin(
      memory_consolidator=consolidator,
  )

  return App(
      name=APP_NAME,
      root_agent=root_agent,
      plugins=[governance_plugin],
      events_compaction_config=EventsCompactionConfig(
          compaction_interval=3,
          overlap_size=1,
          token_threshold=8000,
          event_retention_size=2,
      ),
      context_cache_config=ContextCacheConfig(
          cache_intervals=10,
          ttl_seconds=1800,
          min_tokens=2048,
      ),
      resumability_config=ResumabilityConfig(
          is_resumable=True,
      ),
  )


def build_runner(*, session_db_url: Optional[str] = None) -> Runner:
  """Bootstraps secrets, tracing, persistent sessions, memory, and the ADK Runner.

  Args:
    session_db_url: Optional SQLAlchemy URL overriding Secret Manager / env.

  Returns:
    Ready-to-run ADK ``Runner`` wired to persistent session and memory services.
  """
  secret_status = configure_runtime_secrets()
  configure_tracing()

  session_service = create_session_service(db_url=session_db_url)
  memory_service = create_memory_service()
  consolidator = AsyncMemoryConsolidator(
      ledger=get_expense_ledger(),
      memory_service=memory_service,
  )
  application = build_app(memory_consolidator=consolidator)

  log_structured_event(
      _LOGGER,
      event_type="runtime_initialized",
      message="Enterprise Expense Approval ADK Runner initialized.",
      data={
          "app_name": APP_NAME,
          "secrets_status": secret_status,
          "session_service": type(session_service).__name__,
          "memory_service": type(memory_service).__name__,
      },
  )

  return Runner(
      app=application,
      session_service=session_service,
      memory_service=memory_service,
  )


# Exported ADK App symbol discovered by `adk web`, `adk run`, and `adk eval`
app = build_app()

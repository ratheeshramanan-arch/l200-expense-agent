"""Enterprise Expense & Policy Approval Multi-Agent package (Google ADK)."""

from __future__ import annotations

try:
  from expense_agent.agent import root_agent
  from expense_agent.app import app
except ImportError:  # pragma: no cover - allows inspecting utility modules offline
  root_agent = None  # type: ignore[assignment]
  app = None  # type: ignore[assignment]

__all__ = ["app", "root_agent"]

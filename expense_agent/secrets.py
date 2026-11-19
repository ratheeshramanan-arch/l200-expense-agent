"""Secure secret retrieval backed by Google Cloud Secret Manager.

Never hardcodes API keys, database passwords, or service tokens in source code.
In production (Cloud Run / Vertex AI Agent Engine), secrets are fetched via
Google Cloud Secret Manager using Application Default Credentials (ADC).
For local developer testing, an explicit environment-variable fallback can be
enabled via ``ALLOW_LOCAL_ENV_FALLBACK=true``.
"""

from __future__ import annotations

from functools import lru_cache
import os
import re
from typing import Optional

_VALID_SECRET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,255}$")


class SecretRetrievalError(RuntimeError):
  """Raised when a required secret cannot be resolved securely."""


class SecretManagerProvider:
  """Resolves runtime secrets from Google Cloud Secret Manager.

  Attributes:
    project_id: Google Cloud project hosting the Secret Manager secrets.
    allow_local_env_fallback: Whether local environment variables may be used
      when running outside GCP (disabled by default in production).
  """

  def __init__(
      self,
      project_id: Optional[str] = None,
      allow_local_env_fallback: Optional[bool] = None,
  ) -> None:
    self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "").strip()
    if allow_local_env_fallback is None:
      self.allow_local_env_fallback = (
          os.getenv("ALLOW_LOCAL_ENV_FALLBACK", "false").lower() == "true"
      )
    else:
      self.allow_local_env_fallback = allow_local_env_fallback
    self._client = None

  def _get_client(self):
    """Lazily initializes the Secret Manager gRPC client."""
    if self._client is None:
      from google.cloud import secretmanager

      self._client = secretmanager.SecretManagerServiceClient()
    return self._client

  def get_secret(
      self,
      secret_id: str,
      *,
      version: str = "latest",
      env_var_fallback: Optional[str] = None,
  ) -> str:
    """Retrieves a secret value from Secret Manager or local dev fallback.

    Args:
      secret_id: Secret Manager secret name (alphanumeric, hyphens, underscores).
      version: Secret version identifier (defaults to ``"latest"``).
      env_var_fallback: Optional environment variable name allowed only when
        ``allow_local_env_fallback`` is enabled for local development.

    Returns:
      The decoded UTF-8 secret value.

    Raises:
      ValueError: If ``secret_id`` fails strict naming validation.
      SecretRetrievalError: If the secret cannot be resolved from Secret Manager
        or an authorized local fallback.
    """
    if not _VALID_SECRET_ID_RE.match(secret_id):
      raise ValueError(
          f"Invalid Secret Manager secret_id {secret_id!r}. "
          "Expected 1-255 alphanumeric, hyphen, or underscore characters."
      )

    if self.project_id and not self.allow_local_env_fallback:
      return self._fetch_from_gcp_secret_manager(secret_id, version)

    if self.project_id:
      try:
        return self._fetch_from_gcp_secret_manager(secret_id, version)
      except Exception:
        if not self.allow_local_env_fallback:
          raise

    if self.allow_local_env_fallback and env_var_fallback:
      env_value = os.getenv(env_var_fallback, "").strip()
      if env_value:
        return env_value

    raise SecretRetrievalError(
        f"Unable to retrieve secret '{secret_id}'. Configure GCP_PROJECT_ID "
        "with Secret Manager access (roles/secretmanager.secretAccessor), or "
        "enable ALLOW_LOCAL_ENV_FALLBACK=true for local development."
    )

  def _fetch_from_gcp_secret_manager(self, secret_id: str, version: str) -> str:
    """Calls Secret Manager API to access a specific secret version."""
    client = self._get_client()
    resource_name = (
        f"projects/{self.project_id}/secrets/{secret_id}/versions/{version}"
    )
    response = client.access_secret_version(request={"name": resource_name})
    payload = response.payload.data.decode("utf-8").strip()
    if not payload:
      raise SecretRetrievalError(
          f"Secret Manager resource '{resource_name}' returned an empty payload."
      )
    return payload


@lru_cache(maxsize=1)
def get_secret_provider() -> SecretManagerProvider:
  """Returns the process-wide cached SecretManagerProvider instance."""
  return SecretManagerProvider()


def get_session_database_url() -> str:
  """Resolves the persistent ADK session database URL securely.

  Returns:
    SQLAlchemy-compatible database URL (Cloud SQL PostgreSQL in production or
    local SQLite database URL for development).
  """
  provider = get_secret_provider()
  secret_name = os.getenv("SESSION_DB_URL_SECRET_NAME", "session-db-url")
  try:
    return provider.get_secret(
        secret_name,
        env_var_fallback="SESSION_DB_URL",
    )
  except SecretRetrievalError:
    return "sqlite+aiosqlite:///./expense_sessions.db"


def configure_runtime_secrets() -> dict[str, bool]:
  """Hydrates runtime SDK credentials from Secret Manager at startup.

  Returns:
    Dictionary indicating which runtime secrets were resolved (never returns
    raw secret values).
  """
  provider = get_secret_provider()
  status: dict[str, bool] = {}

  gemini_secret_name = os.getenv("GEMINI_API_KEY_SECRET_NAME", "gemini-api-key")
  try:
    api_key = provider.get_secret(
        gemini_secret_name,
        env_var_fallback="GEMINI_API_KEY",
    )
    os.environ["GOOGLE_API_KEY"] = api_key
    status["gemini_api_key_loaded"] = True
  except SecretRetrievalError:
    status["gemini_api_key_loaded"] = bool(os.getenv("GOOGLE_API_KEY"))

  status["session_db_url_configured"] = bool(get_session_database_url())
  return status

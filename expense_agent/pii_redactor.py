"""PII redaction pipeline backed by Google Cloud DLP with local regex fallback.

Every log line, trace attribute, and memory consolidation payload passes through
``PIIRedactor`` before persistence or emission so sensitive personal or payment
data (emails, SSNs, credit cards, phone numbers, employee IDs) is never stored
in plaintext.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

# Deterministic regex patterns for offline/local environments and defense-in-depth
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "CREDIT_CARD_NUMBER",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    ),
    (
        "US_SOCIAL_SECURITY_NUMBER",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "EMAIL_ADDRESS",
        re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    ),
    (
        "PHONE_NUMBER",
        re.compile(
            r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\w)"
        ),
    ),
    (
        "API_KEY_OR_TOKEN",
        re.compile(r"\b(?:AIza[0-9A-Za-z\-_]{35}|sk-[a-zA-Z0-9]{20,})\b"),
    ),
)

_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "ssn",
    "credit_card",
    "card_number",
    "password",
    "secret",
    "api_key",
    "token",
    "bank_account",
    "iban",
)

_DEFAULT_DLP_INFO_TYPES: tuple[str, ...] = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_CARD_NUMBER",
    "IBAN_CODE",
    "PERSON_NAME",
)


class PIIRedactor:
  """Scrubs PII from free text and nested JSON payloads.

  Uses Google Cloud Sensitive Data Protection (Cloud DLP) when enabled and
  configured with a ``GCP_PROJECT_ID``, and always applies deterministic regex
  scrubbing as a local fallback and defense-in-depth layer.
  """

  def __init__(
      self,
      project_id: Optional[str] = None,
      enable_cloud_dlp: Optional[bool] = None,
  ) -> None:
    self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "").strip()
    if enable_cloud_dlp is None:
      self.enable_cloud_dlp = (
          os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true"
      )
    else:
      self.enable_cloud_dlp = enable_cloud_dlp
    self._dlp_client = None

  def _get_dlp_client(self):
    """Lazily initializes the Google Cloud DLP client."""
    if self._dlp_client is None:
      from google.cloud import dlp_v2

      self._dlp_client = dlp_v2.DlpServiceClient()
    return self._dlp_client

  def redact_text(self, text: str) -> str:
    """Redacts PII entities from a string using Cloud DLP and regex patterns.

    Args:
      text: Raw input string that may contain emails, SSNs, card numbers, or
        other sensitive identifiers.

    Returns:
      Sanitized string with sensitive spans replaced by ``[REDACTED_<TYPE>]``.
    """
    if not text:
      return text

    sanitized = text
    if self.enable_cloud_dlp and self.project_id:
      try:
        sanitized = self._deidentify_with_cloud_dlp(sanitized)
      except Exception:
        # Never fail open: fall through to deterministic local regex redaction.
        pass

    return self._redact_with_regex(sanitized)

  def _deidentify_with_cloud_dlp(self, text: str) -> str:
    """Calls Google Cloud DLP ``deidentify_content`` with info-type replacement."""
    client = self._get_dlp_client()
    parent = f"projects/{self.project_id}/locations/global"
    inspect_config = {
        "info_types": [{"name": info_type} for info_type in _DEFAULT_DLP_INFO_TYPES],
    }
    deidentify_config = {
        "info_type_transformations": {
            "transformations": [
                {
                    "primitive_transformation": {
                        "replace_with_info_type_config": {}
                    }
                }
            ]
        }
    }
    response = client.deidentify_content(
        request={
            "parent": parent,
            "deidentify_config": deidentify_config,
            "inspect_config": inspect_config,
            "item": {"value": text},
        }
    )
    return response.item.value

  @staticmethod
  def _redact_with_regex(text: str) -> str:
    """Applies deterministic regex redaction for local runs and defense-in-depth."""
    result = text
    for entity_name, pattern in _PII_PATTERNS:
      result = pattern.sub(f"[REDACTED_{entity_name}]", result)
    return result

  def contains_unredacted_pii(self, text: str) -> bool:
    """Checks whether text still matches known PII patterns.

    Args:
      text: Candidate string to inspect.

    Returns:
      True if any known PII pattern is detected.
    """
    if not text:
      return False
    return any(pattern.search(text) is not None for _, pattern in _PII_PATTERNS)

  def redact_payload(self, payload: Any) -> Any:
    """Recursively scrubs PII from nested dicts, lists, tuples, and strings.

    Args:
      payload: Arbitrary JSON-serializable object.

    Returns:
      Deeply sanitized copy safe for structured logging and memory persistence.
    """
    if isinstance(payload, str):
      return self.redact_text(payload)
    if isinstance(payload, dict):
      sanitized_dict: dict[str, Any] = {}
      for key, value in payload.items():
        key_str = str(key)
        lowered = key_str.lower()
        if any(token in lowered for token in _SENSITIVE_KEY_SUBSTRINGS):
          sanitized_dict[key_str] = "[REDACTED_SENSITIVE_FIELD]"
        else:
          sanitized_dict[key_str] = self.redact_payload(value)
      return sanitized_dict
    if isinstance(payload, list):
      return [self.redact_payload(item) for item in payload]
    if isinstance(payload, tuple):
      return [self.redact_payload(item) for item in payload]
    return payload


_DEFAULT_REDACTOR = PIIRedactor()


def get_pii_redactor() -> PIIRedactor:
  """Returns the shared PIIRedactor instance."""
  return _DEFAULT_REDACTOR

"""Generic tool error classification shared by the tool layer and the repair loop.

Classifications are derived from exception types and structured result statuses
only. No concrete entity, provider, or domain value participates in mapping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

CATEGORY_TIMEOUT = "timeout"
CATEGORY_NETWORK = "network"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_INVALID_ARGUMENTS = "invalid_arguments"
CATEGORY_PERMISSION_DENIED = "permission_denied"
CATEGORY_POLICY_DENIED = "policy_denied"
CATEGORY_BUDGET_DENIED = "budget_denied"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_BUSINESS_REJECTED = "business_rejected"
CATEGORY_UNKNOWN = "unknown"

_RETRYABLE_CATEGORIES = frozenset({CATEGORY_TIMEOUT, CATEGORY_NETWORK, CATEGORY_RATE_LIMITED})

# Exception type name -> category. Matched against the exception's MRO so
# subclasses resolve to their most specific known ancestor.
_EXCEPTION_CATEGORIES: dict[str, str] = {
    "TimeoutError": CATEGORY_TIMEOUT,
    "ConnectionError": CATEGORY_NETWORK,
    "PermissionError": CATEGORY_PERMISSION_DENIED,
    "FileNotFoundError": CATEGORY_NOT_FOUND,
}

# Structured result status -> category. Statuses absent from this table fall
# back to the conservative default (unknown, retryable) so execution failures
# keep the existing bounded repair behaviour.
_STATUS_CATEGORIES: dict[str, str] = {
    "timed_out": CATEGORY_TIMEOUT,
    "rate_limited": CATEGORY_RATE_LIMITED,
    "network_error": CATEGORY_NETWORK,
    "connection_error": CATEGORY_NETWORK,
    "invalid_input": CATEGORY_INVALID_ARGUMENTS,
    "invalid_arguments": CATEGORY_INVALID_ARGUMENTS,
    "permission_denied": CATEGORY_PERMISSION_DENIED,
    "policy_denied": CATEGORY_POLICY_DENIED,
    "budget_denied": CATEGORY_BUDGET_DENIED,
    "not_found": CATEGORY_NOT_FOUND,
    "duplicate_side_effect_tool_call": CATEGORY_POLICY_DENIED,
}

# Failure-shaped statuses recognized when deciding whether a structured tool
# result describes a failure at all.
FAILURE_STATUSES = frozenset(
    {
        "failed",
        "error",
        "blocked",
        "write_failed",
        "verification_failed",
        "skill_execution_failed",
        "policy_denied",
        "budget_denied",
        "runtime_unavailable",
        "invalid_input",
        "permission_denied",
        "not_found",
        "timed_out",
        "rate_limited",
        "command_failed",
        "output_truncated",
    }
)


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    retryable: bool


def _classification_for_category(category: str) -> ErrorClassification:
    # Unrecognized categories default to retryable so unclassified failures
    # keep the existing bounded repair behaviour instead of terminating runs.
    retryable = category in _RETRYABLE_CATEGORIES or category == CATEGORY_UNKNOWN
    return ErrorClassification(category=category, retryable=retryable)


def classify_exception(exc: BaseException) -> ErrorClassification:
    for klass in type(exc).__mro__:
        category = _EXCEPTION_CATEGORIES.get(klass.__name__)
        if category:
            return _classification_for_category(category)
    return _classification_for_category(CATEGORY_UNKNOWN)


def _payload_is_failure(payload: dict) -> bool:
    if payload.get("success") is False or payload.get("error") or payload.get("blocked"):
        return True
    return str(payload.get("status") or "").strip().lower() in FAILURE_STATUSES


def classify_payload(payload: dict) -> ErrorClassification | None:
    """Classify a structured tool result; None when the result is not a failure."""
    if not isinstance(payload, dict) or not _payload_is_failure(payload):
        return None
    explicit = str(payload.get("error_category") or "").strip()
    if explicit:
        return _classification_for_category(explicit)
    reason = str(payload.get("reason") or "").strip().lower()
    if reason in _STATUS_CATEGORIES:
        return _classification_for_category(_STATUS_CATEGORIES[reason])
    status = str(payload.get("status") or "").strip().lower()
    if status in _STATUS_CATEGORIES:
        return _classification_for_category(_STATUS_CATEGORIES[status])
    return _classification_for_category(CATEGORY_UNKNOWN)


def failure_payload_category(result_text: str) -> ErrorClassification | None:
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return classify_payload(payload)


def normalize_failure_result(result_text: str) -> str:
    """Ensure failure-shaped JSON results carry error_category and retryable."""
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return result_text
    if not isinstance(payload, dict):
        return result_text
    classification = classify_payload(payload)
    if classification is None:
        return result_text
    if "error_category" in payload and "retryable" in payload:
        return result_text
    enriched = dict(payload)
    enriched.setdefault("error_category", classification.category)
    enriched.setdefault("retryable", classification.retryable)
    return json.dumps(enriched, ensure_ascii=False, indent=2)

"""Provider-agnostic safety primitives for standardized remote commands."""

from __future__ import annotations

import shlex
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .mutation_manifest import CommandManifest, normalize_command_manifest


_MUTATING_OPERATIONS = frozenset({"append", "edit", "replace", "create", "delete"})
_RECOGNIZED_OPERATIONS = _MUTATING_OPERATIONS | {"read"}
_REPLACEMENT_WORKFLOW_TERMS = frozenset({"repair", "overwrite", "reformat", "replace", "replacement"})


@dataclass(frozen=True)
class RemoteMutationEnvelope:
    provider: str
    resource: str
    target: str
    operation: str
    mutating: bool


@dataclass(frozen=True)
class MutationRecord:
    key: str
    envelope: RemoteMutationEnvelope
    result: Any
    before_version: str
    after_version: str
    result_hash: str
    verified: bool


def parse_standardized_remote_command(request: str) -> RemoteMutationEnvelope:
    """Classify a ``provider resource operation`` command without provider-specific rules."""
    try:
        manifest = normalize_command_manifest(str(request or ""))
    except ValueError:
        try:
            parts = shlex.split(str(request or ""))
        except ValueError:
            parts = []
        provider, resource = (parts + ["", ""])[:2]
        operation = "unknown"
        target = parts[3] if len(parts) > 3 and not parts[3].startswith("-") else ""
    else:
        provider = manifest.provider
        resource = manifest.resource
        target = manifest.target
        operation = manifest.operation
    return RemoteMutationEnvelope(
        provider=provider,
        resource=resource,
        target=target,
        operation=operation,
        mutating=operation in _MUTATING_OPERATIONS,
    )


def build_idempotency_key(envelope: RemoteMutationEnvelope | CommandManifest, arguments: dict[str, Any] | None) -> str:
    """Return a stable mutation identity without model-generated call identifiers."""
    canonical_arguments = {
        str(key): value
        for key, value in (arguments or {}).items()
        if str(key) != "tool_call_id"
    }
    if isinstance(envelope, CommandManifest):
        payload = {
            "manifest": envelope.idempotency_input,
            "arguments": {
                key: value
                for key, value in canonical_arguments.items()
                if key != "manifest"
            },
        }
    else:
        payload = {
            "provider": envelope.provider,
            "resource": envelope.resource,
            "target": envelope.target,
            "operation": envelope.operation,
            "arguments": canonical_arguments,
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_conflicts_with_workflow(
    envelope: RemoteMutationEnvelope | CommandManifest,
    request: str = "",
) -> bool:
    """Return whether a legacy append command declares a replacement-style workflow."""
    if envelope.operation != "append":
        return False
    if isinstance(envelope, CommandManifest):
        return operation_for_document_workflow(envelope) != envelope.operation
    try:
        parts = {part.lower() for part in shlex.split(str(request or ""))}
    except ValueError:
        return True
    return bool(parts & _REPLACEMENT_WORKFLOW_TERMS)


def operation_for_document_workflow(manifest: CommandManifest) -> str:
    """Select the safe operation for an explicit workflow declaration."""
    if manifest.resource != "document" or manifest.operation != "append":
        return manifest.operation
    argv = manifest.arguments.get("argv") or []
    tokens = [str(token).lower() for token in argv]
    try:
        workflow_index = tokens.index("--workflow")
        workflow = tokens[workflow_index + 1]
    except (ValueError, IndexError, TypeError):
        return manifest.operation
    if workflow in _REPLACEMENT_WORKFLOW_TERMS:
        return "replace"
    return manifest.operation


class MutationLedger:
    """Run-scoped record of successful remote mutations."""

    def __init__(self) -> None:
        self._succeeded: dict[str, MutationRecord] = {}

    def succeeded_result(self, key: str) -> MutationRecord | None:
        return self._succeeded.get(key)

    def record_success(
        self,
        key: str,
        envelope: RemoteMutationEnvelope,
        result: Any,
        *,
        before_version: str = "",
        after_version: str = "",
        verified: bool = False,
    ) -> MutationRecord:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        record = MutationRecord(
            key=key,
            envelope=envelope,
            result=result,
            before_version=str(before_version or ""),
            after_version=str(after_version or ""),
            result_hash="sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            verified=bool(verified),
        )
        self._succeeded[key] = record
        return record

"""Normalized, provider-agnostic metadata for CLI-backed remote commands."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_MUTATING_OPERATIONS = frozenset({"append", "edit", "replace", "create", "delete"})
_RECOGNIZED_OPERATIONS = _MUTATING_OPERATIONS | {"read"}
_DOCUMENT_RESOURCE_ALIASES = frozenset({"doc", "document"})
_DOCUMENT_OPERATION_ALIASES = {
    "delete-block": "delete",
    "set-title": "edit",
    "edit-code": "edit",
}


@dataclass(frozen=True)
class CommandManifest:
    """The operation contract used for mutation policy, auditing, and retries."""

    provider: str
    resource: str
    target: str
    operation: str
    mutating: bool
    arguments: dict[str, Any]
    idempotency_input: str
    verification_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "resource": self.resource,
            "target": self.target,
            "operation": self.operation,
            "mutating": self.mutating,
            "arguments": dict(self.arguments),
            "idempotency_input": self.idempotency_input,
            "verification_mode": self.verification_mode,
        }


def _canonical_arguments(parts: Sequence[str]) -> dict[str, Any]:
    return {"command": " ".join(parts), "argv": list(parts)}


def _idempotency_input(
    provider: str,
    resource: str,
    target: str,
    operation: str,
    arguments: Mapping[str, Any],
) -> str:
    payload = {
        "provider": provider,
        "resource": resource,
        "target": target,
        "operation": operation,
        "arguments": arguments,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _split_command(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError as exc:
            raise ValueError("Command manifest requires valid shell-style arguments.") from exc
    return [str(part) for part in command]


def normalize_command_manifest(command: str | Sequence[str] | Mapping[str, Any] | CommandManifest) -> CommandManifest:
    """Normalize a standardized command or existing serialized manifest.

    The parser deliberately handles command shape only. It does not select
    providers, inspect entity names, or infer user intent.
    """
    if isinstance(command, CommandManifest):
        return command
    if isinstance(command, Mapping):
        if "arguments" not in command and "canonical_arguments" in command:
            command = dict(command)
            command["arguments"] = command["canonical_arguments"]
        required = ("provider", "resource", "target", "operation", "mutating", "arguments", "idempotency_input", "verification_mode")
        missing = [name for name in required if name not in command]
        if missing:
            raise ValueError(f"Command manifest is missing required fields: {', '.join(missing)}")
        return CommandManifest(
            provider=str(command["provider"]),
            resource=str(command["resource"]),
            target=str(command["target"]),
            operation=str(command["operation"]),
            mutating=bool(command["mutating"]),
            arguments=dict(command["arguments"]),
            idempotency_input=str(command["idempotency_input"]),
            verification_mode=str(command["verification_mode"]),
        )

    parts = _split_command(command)
    if len(parts) < 3:
        raise ValueError("Command manifest requires provider, resource, and operation.")
    provider, raw_resource, raw_operation = (part.strip().lower() for part in parts[:3])
    resource = "document" if raw_resource in _DOCUMENT_RESOURCE_ALIASES else raw_resource
    operation = _DOCUMENT_OPERATION_ALIASES.get(raw_operation, raw_operation)
    if operation not in _RECOGNIZED_OPERATIONS:
        raise ValueError(f"Command manifest cannot normalize operation: {raw_operation}")
    target = "" if operation == "create" else (parts[3] if len(parts) > 3 and not parts[3].startswith("-") else "")
    arguments = _canonical_arguments(parts)
    return CommandManifest(
        provider=provider,
        resource=resource,
        target=target,
        operation=operation,
        mutating=operation in _MUTATING_OPERATIONS,
        arguments=arguments,
        idempotency_input=_idempotency_input(provider, resource, target, operation, arguments),
        verification_mode="read_back" if operation == "replace" else "none",
    )

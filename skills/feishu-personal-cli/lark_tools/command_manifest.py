"""Typed command manifests for the standalone Lark CLI compatibility layer."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from typing import Sequence


_OPERATIONS = frozenset({"read", "append", "edit", "replace", "create", "delete"})
_ALIASES = {"delete-block": "delete", "set-title": "edit", "edit-code": "edit"}


@dataclass(frozen=True)
class CommandManifest:
    provider: str
    resource: str
    target: str
    operation: str
    mutating: bool
    canonical_arguments: dict[str, object]
    idempotency_input: str
    verification_mode: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "resource": self.resource,
            "target": self.target,
            "operation": self.operation,
            "mutating": self.mutating,
            "arguments": dict(self.canonical_arguments),
            "canonical_arguments": dict(self.canonical_arguments),
            "idempotency_input": self.idempotency_input,
            "verification_mode": self.verification_mode,
        }


def _command_parts(argv: str | Sequence[str]) -> list[str]:
    if isinstance(argv, str):
        try:
            return shlex.split(argv)
        except ValueError as exc:
            raise ValueError("Command manifest requires valid shell-style arguments.") from exc
    return [str(value) for value in argv]


def parse_command_manifest(argv: str | Sequence[str]) -> CommandManifest:
    """Normalize a document command without making provider-specific decisions."""
    parts = _command_parts(argv)
    if parts and parts[0].lower() in {"lark", "lark_cli"}:
        parts = parts[1:]
    if len(parts) < 2 or parts[0].lower() not in {"doc", "document"}:
        raise ValueError("Command manifest requires a document resource.")
    operation = _ALIASES.get(parts[1].lower(), parts[1].lower())
    if operation not in _OPERATIONS:
        raise ValueError(f"Command manifest cannot normalize operation: {parts[1]}")
    target = "" if operation == "create" else (parts[2] if len(parts) > 2 and not parts[2].startswith("-") else "")
    canonical_arguments: dict[str, object] = {
        "command": "lark " + " ".join(parts),
        "argv": list(parts),
    }
    payload = {
        "provider": "lark",
        "resource": "document",
        "target": target,
        "operation": operation,
        "arguments": canonical_arguments,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return CommandManifest(
        provider="lark",
        resource="document",
        target=target,
        operation=operation,
        mutating=operation != "read",
        canonical_arguments=canonical_arguments,
        idempotency_input="sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        verification_mode="read_back" if operation == "replace" else "none",
    )

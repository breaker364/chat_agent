"""Pure planning helpers for a version-checked document root replacement."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .docx_ot import build_insert_change_map


def canonical_block_summary(blocks: list[dict[str, Any]]) -> dict[str, int]:
    """Return a deterministic count by rendered block type."""
    return dict(sorted(Counter(str(block.get("type") or "text") for block in blocks).items()))


def canonical_content_hash(blocks: list[dict[str, Any]]) -> str:
    """Hash only structural source fields that the writer can render."""
    canonical = []
    for block in blocks:
        item: dict[str, Any] = {"type": str(block.get("type") or "text")}
        if item["type"] == "table":
            item["rows"] = [list(row) for row in block.get("rows") or []]
        else:
            item["text"] = str(block.get("text") or "")
            if item["type"] == "code":
                item["language"] = str(block.get("language") or "")
        canonical.append(item)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_root_replacement_change_map(
    blocks: list[dict[str, Any]],
    parent_id: str,
    parent_version: int,
    existing_child_ids: list[str],
    author_uid: str,
) -> tuple[dict[str, Any], list[str]]:
    """Build one root mutation that removes existing children then inserts replacements."""
    change_map, new_ids = build_insert_change_map(
        blocks=blocks,
        parent_id=parent_id,
        parent_version=parent_version,
        author_uid=author_uid,
        insert_at=0,
    )
    removals = [
        {"p": ["children", index], "action": {"ld": child_id}}
        for index, child_id in reversed(list(enumerate(existing_child_ids)))
    ]
    parent_ops = change_map[parent_id]["payload"]["ops"]
    change_map[parent_id]["payload"]["ops"] = removals + parent_ops
    return change_map, new_ids

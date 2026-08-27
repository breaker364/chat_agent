import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import mutation_guard


@pytest.mark.parametrize(
    ("command", "operation", "mutating", "target", "verification_mode"),
    [
        ("lark doc read doc-token", "read", False, "doc-token", "none"),
        ("lark doc append doc-token --text section", "append", True, "doc-token", "none"),
        ("lark doc edit doc-token block-token --replace revised", "edit", True, "doc-token", "none"),
        ("lark doc replace doc-token --md-file replacement.md", "replace", True, "doc-token", "read_back"),
        ("lark doc create draft --text body", "create", True, "", "none"),
        ("lark doc delete doc-token", "delete", True, "doc-token", "none"),
    ],
)
def test_normalizes_document_commands_to_typed_manifests(
    command, operation, mutating, target, verification_mode
):
    normalizer = getattr(mutation_guard, "normalize_command_manifest", None)
    assert callable(normalizer), "Mutation guard must expose normalize_command_manifest"
    manifest = normalizer(command)

    assert manifest.provider == "lark"
    assert manifest.resource == "document"
    assert manifest.operation == operation
    assert manifest.mutating is mutating
    assert manifest.target == target
    assert manifest.verification_mode == verification_mode
    assert manifest.idempotency_input
    assert manifest.arguments["command"] == command


def test_rejects_mutating_command_that_cannot_be_normalized():
    normalizer = getattr(mutation_guard, "normalize_command_manifest", None)
    assert callable(normalizer), "Mutation guard must expose normalize_command_manifest"
    with pytest.raises(ValueError, match="manifest"):
        normalizer("lark doc publish doc-token")


@pytest.mark.parametrize(
    ("command", "operation", "mutating"),
    [
        ("lark bitable tables base-token", "read", False),
        ("lark bitable schema base-token tbl-token", "read", False),
        ("lark bitable records base-token tbl-token --all", "read", False),
        ("lark bitable views base-token tbl-token", "read", False),
        ("lark bitable download base-token tbl-token --out export.json", "read", False),
        ("lark bitable create TestBase", "create", True),
        ("lark bitable add-record base-token tbl-token Name=one", "append", True),
        ("lark bitable add-records-batch base-token tbl-token []", "append", True),
        ("lark bitable set-record base-token tbl-token rec-token Name=two", "edit", True),
        ("lark bitable add-field base-token tbl-token Amount --type number", "edit", True),
        ("lark bitable add-fields-batch base-token tbl-token []", "edit", True),
        ("lark bitable set-field-format base-token tbl-token Amount 0.###", "edit", True),
        ("lark bitable rename-field base-token tbl-token fld-token Amount", "edit", True),
        ("lark bitable delete-record base-token tbl-token rec-token", "delete", True),
        ("lark bitable delete-records base-token tbl-token rec-token", "delete", True),
        ("lark bitable add-table base-token NewTable", "create", True),
    ],
)
def test_normalizes_configured_bitable_subcommands_to_safe_operations(command, operation, mutating):
    manifest = mutation_guard.normalize_command_manifest(
        command,
        subcommand_operations={
            "lark bitable": {
                "tables": "read",
                "schema": "read",
                "records": "read",
                "views": "read",
                "download": "read",
                "create": "create",
                "add-record": "append",
                "add-records-batch": "append",
                "set-record": "edit",
                "add-field": "edit",
                "add-fields-batch": "edit",
                "set-field-format": "edit",
                "rename-field": "edit",
                "delete-record": "delete",
                "delete-records": "delete",
                "add-table": "create",
            },
        },
    )

    assert manifest.operation == operation
    assert manifest.mutating is mutating
    assert manifest.arguments["command"] == command


def test_rejects_unclassified_bitable_subcommand():
    with pytest.raises(ValueError, match="operation"):
        mutation_guard.normalize_command_manifest(
            "lark bitable publish base-token",
            subcommand_operations={"lark bitable": {"tables": "read"}},
        )


def test_routes_repair_workflows_to_replace_and_explicit_add_content_to_append():
    route = getattr(mutation_guard, "operation_for_document_workflow", None)
    assert callable(route), "Mutation guard must expose document workflow routing"
    normalizer = getattr(mutation_guard, "normalize_command_manifest", None)
    assert callable(normalizer), "Mutation guard must expose normalize_command_manifest"

    repair = normalizer("lark doc append doc-token --workflow repair --md-file body.md")
    add_content = normalizer("lark doc append doc-token --workflow add-content --text section")

    assert route(repair) == "replace"
    assert route(add_content) == "append"

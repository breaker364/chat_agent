import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

import lark_tools.cli as lark_cli  # noqa: E402


@pytest.mark.parametrize(
    ("argv", "operation", "mutating", "target", "verification_mode"),
    [
        (["lark", "doc", "read", "doc-token"], "read", False, "doc-token", "none"),
        (["lark", "doc", "append", "doc-token", "--md-file", "body.md"], "append", True, "doc-token", "none"),
        (["lark", "doc", "edit", "doc-token", "block-token", "--replace", "revised"], "edit", True, "doc-token", "none"),
        (["lark", "doc", "replace", "doc-token", "--md-file", "body.md"], "replace", True, "doc-token", "read_back"),
        (["lark", "doc", "create", "draft", "--text", "body"], "create", True, "", "none"),
        (["lark", "doc", "delete", "doc-token"], "delete", True, "doc-token", "none"),
    ],
)
def test_cli_parser_exposes_typed_document_manifest(
    argv, operation, mutating, target, verification_mode
):
    parser = getattr(lark_cli, "parse_command_manifest", None)
    assert callable(parser), "CLI must expose parse_command_manifest"
    manifest = parser(argv)

    assert manifest.provider == "lark"
    assert manifest.resource == "document"
    assert manifest.operation == operation
    assert manifest.mutating is mutating
    assert manifest.target == target
    assert manifest.verification_mode == verification_mode
    assert manifest.canonical_arguments
    assert manifest.idempotency_input


def test_cli_parser_rejects_unknown_document_operation():
    parser = getattr(lark_cli, "parse_command_manifest", None)
    assert callable(parser), "CLI must expose parse_command_manifest"
    with pytest.raises(ValueError, match="operation"):
        parser(["lark", "doc", "publish", "doc-token"])

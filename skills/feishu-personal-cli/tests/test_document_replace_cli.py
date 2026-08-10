import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

import lark_tools.cli as lark_cli  # noqa: E402
from lark_tools.commands import doc_write  # noqa: E402


def test_cli_dispatches_replace_with_explicit_markdown_file_and_dry_run():
    replace = getattr(lark_cli, "cmd_doc_replace", None)
    assert callable(replace), "CLI must register cmd_doc_replace"
    argv = ["doc", "replace", "doc-token", "--md-file", "replacement.md", "--dry-run"]
    with patch.object(lark_cli, "audit_write", return_value=contextlib.nullcontext()), patch.object(
        lark_cli, "cmd_doc_replace"
    ) as command:
        lark_cli._dispatch_extended("doc", [], argv, argv, "")

    command.assert_called_once_with([], "doc-token", "replacement.md", dry_run=True)


def test_replace_command_result_contains_manifest_verification_and_rollback_reference(capsys):
    command = getattr(doc_write, "cmd_doc_replace", None)
    assert callable(command), "doc_write must expose cmd_doc_replace"
    replacement = {
        "success": True,
        "status": "verified",
        "target": "resolved-token",
        "operation": "replace",
        "before_version": 4,
        "after_version": 5,
        "source_hash": "sha256:source",
        "content_hash": "sha256:source",
        "planned_block_summary": {"text": 1},
        "verified": True,
        "verification": {"mode": "read_back", "status": "verified", "verified": True},
        "rollback_reference": "document:resolved-token:version:4",
        "result_reference": "replace:resolved-token:4:sha256:source",
    }
    with patch.object(doc_write, "resolve_doc_token", return_value="resolved-token"), patch.object(
        doc_write, "_read_input", return_value="replacement body"
    ), patch.object(
        doc_write, "replace_markdown", return_value=replacement
    ):
        command([], "doc-token", "replacement.md", dry_run=False)

    result = json.loads(capsys.readouterr().out)
    assert result["manifest"]["operation"] == "replace"
    assert result["manifest"]["verification_mode"] == "read_back"
    assert result["verification"]["status"] == "verified"
    assert result["rollback_reference"] == "document:resolved-token:version:4"
    assert result["version_history"] == {
        "before_version": 4,
        "after_version": 5,
        "rollback_reference": "document:resolved-token:version:4",
    }

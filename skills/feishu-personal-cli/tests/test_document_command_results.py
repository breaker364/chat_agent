import json
import sys
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lark_tools.commands.doc_write import cmd_doc_append  # noqa: E402


def test_append_result_includes_normalized_manifest_and_verification_fields(capsys):
    with patch("lark_tools.commands.doc_write.resolve_doc_token", return_value="resolved-token"), patch(
        "lark_tools.commands.doc_write.append_markdown", return_value={"blocks_added": 2}
    ):
        cmd_doc_append([], "doc-token", text="paragraph")

    result = json.loads(capsys.readouterr().out)
    assert result["manifest"] == {
        "provider": "lark",
        "resource": "document",
        "target": "doc-token",
        "operation": "append",
        "mutating": True,
        "verification_mode": "none",
        "arguments": result["manifest"]["arguments"],
        "canonical_arguments": result["manifest"]["canonical_arguments"],
        "idempotency_input": result["manifest"]["idempotency_input"],
    }
    assert result["verification"] == {"mode": "none", "status": "not_required", "verified": False}
    assert result["result_reference"] == result["manifest"]["idempotency_input"]

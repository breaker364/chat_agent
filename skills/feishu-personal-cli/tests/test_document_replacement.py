import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lark_tools.commands import doc_write  # noqa: E402


def _root_info(version, children):
    return {
        "version": version,
        "children_count": len(children),
        "block_data": {"children": list(children)},
    }


def _replace(markdown, root_info, readback_markdown, *, after_version=2):
    replace = getattr(doc_write, "replace_markdown", None)
    assert callable(replace), "doc_write must expose replace_markdown"
    posted = []
    with patch.object(doc_write, "fetch_root_block_info", return_value=root_info), patch.object(
        doc_write, "get_current_user_uid", return_value="author"
    ), patch.object(doc_write, "generate_member_id", return_value="member"), patch.object(
        doc_write, "post_user_change", side_effect=lambda *args, **kwargs: posted.append((args, kwargs)) or {"data": {}}
    ), patch.object(
        doc_write, "_read_document_snapshot", return_value={"version": after_version, "markdown": readback_markdown}, create=True
    ), patch.object(doc_write, "append_markdown") as append:
        result = replace([], "doc-token", markdown)
    return result, posted, append


def test_replaces_an_empty_document_with_a_paragraph_without_appending():
    result, posted, append = _replace("replacement paragraph", _root_info(1, []), "replacement paragraph")

    root_ops = posted[0][0][3]["doc-token"]["payload"]["ops"]
    assert [op["action"] for op in root_ops] == [{"li": ANY}]
    assert posted[0][1]["retry_on_conflict"] is False
    assert result["verified"] is True
    assert result["before_version"] == 1
    assert result["after_version"] == 2
    assert append.call_count == 0


def test_replaces_a_paragraph_document_by_removing_the_previous_child():
    result, posted, append = _replace("new paragraph", _root_info(4, ["old-child"]), "new paragraph", after_version=5)

    root_ops = posted[0][0][3]["doc-token"]["payload"]["ops"]
    assert root_ops[0] == {"p": ["children", 0], "action": {"ld": "old-child"}}
    assert root_ops[1]["action"] == {"li": ANY}
    assert result["planned_block_summary"] == {"text": 1}
    assert append.call_count == 0


def test_replaces_mixed_heading_and_list_content_with_matching_summary():
    source = "# Heading\n\n- First\n- Second"
    result, posted, append = _replace(source, _root_info(7, ["old-child"]), source, after_version=8)

    assert len(posted) == 1
    assert result["planned_block_summary"] == {"heading1": 1, "bullet": 2}
    assert result["verification"]["status"] == "verified"
    assert append.call_count == 0


def test_rejects_empty_replacement_source_before_reading_or_writing():
    replace = getattr(doc_write, "replace_markdown", None)
    assert callable(replace), "doc_write must expose replace_markdown"
    with patch.object(doc_write, "fetch_root_block_info") as preflight, patch.object(doc_write, "post_user_change") as post:
        with pytest.raises(ValueError, match="empty"):
            replace([], "doc-token", " \n\n")
    preflight.assert_not_called()
    post.assert_not_called()


def test_version_conflict_returns_blocked_result_without_append_fallback():
    replace = getattr(doc_write, "replace_markdown", None)
    assert callable(replace), "doc_write must expose replace_markdown"
    with patch.object(doc_write, "fetch_root_block_info", return_value=_root_info(1, ["old-child"])), patch.object(
        doc_write, "get_current_user_uid", return_value="author"
    ), patch.object(doc_write, "generate_member_id", return_value="member"), patch.object(
        doc_write, "post_user_change", side_effect=RuntimeError("user_change failed: code=-1101")
    ), patch.object(doc_write, "append_markdown") as append:
        result = replace([], "doc-token", "replacement")

    assert result["status"] == "conflict"
    assert result["verified"] is False
    assert append.call_count == 0


def test_verification_mismatch_is_blocked_without_corrective_append():
    result, _posted, append = _replace("expected", _root_info(1, ["old-child"]), "different", after_version=2)

    assert result["status"] == "verification_failed"
    assert result["verified"] is False
    assert result["verification"]["status"] == "blocked"
    assert append.call_count == 0


def test_dry_run_returns_preflight_plan_without_dispatching_a_remote_write():
    replace = getattr(doc_write, "replace_markdown", None)
    assert callable(replace), "doc_write must expose replace_markdown"
    with patch.object(doc_write, "fetch_root_block_info", return_value=_root_info(4, ["old-child"])), patch.object(
        doc_write, "get_current_user_uid", return_value="author"
    ), patch.object(doc_write, "generate_member_id", return_value="member"), patch.object(
        doc_write, "post_user_change", return_value={"data": {}}
    ) as post, patch.object(
        doc_write, "_read_document_snapshot", return_value={"version": 5, "markdown": "replacement"}
    ):
        result = replace([], "doc-token", "replacement", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["success"] is True
    assert result["verification"]["status"] == "not_dispatched"
    assert result["rollback_reference"] == "document:doc-token:version:4"
    post.assert_not_called()

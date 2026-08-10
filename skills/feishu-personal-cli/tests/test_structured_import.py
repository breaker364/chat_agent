import json
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

import lark_tools  # noqa: E402


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "structured_import_cases.json").read_text(encoding="utf-8"))


def _pipeline():
    pipeline = getattr(lark_tools, "structured_import", None)
    assert pipeline is not None, "lark_tools must expose the structured import pipeline"
    return pipeline


def test_extracts_generic_structural_nodes_and_keeps_metadata_out_of_body():
    pipeline = _pipeline()
    document = pipeline.extract_structured_document(**FIXTURES["multi_page"])

    assert [node.kind for node in document.nodes] == [
        "title",
        "heading",
        "paragraph",
        "unordered_list",
        "ordered_list",
        "quote",
        "code",
        "table",
    ]
    assert document.metadata["source_path"] == "input/source.pdf"
    assert document.metadata["page_numbers"] == [1, 2]
    assert document.metadata["source_timestamp"] == "2026-08-10T00:00:00Z"
    assert document.confidence == 0.94
    assert "input/source.pdf" not in pipeline.render_replacement_plan(document).markdown


def test_serialization_hash_and_rendered_summary_are_structural_and_stable():
    pipeline = _pipeline()
    document = pipeline.extract_structured_document(**FIXTURES["multi_page"])
    plan = pipeline.render_replacement_plan(document)

    assert plan.markdown == (
        "# Document title\n\n## First section\n\nFirst paragraph.\n\n- First item\n\n"
        "1. Second item\n\n> Quoted text.\n\n```python\nprint('ok')\n```\n\n"
        "| Key | Value |\n| --- | --- |\n| A | B |"
    )
    assert plan.block_summary == {
        "bullet": 1,
        "code": 1,
        "heading1": 1,
        "heading2": 1,
        "ordered": 1,
        "quote": 1,
        "table": 1,
        "text": 1,
    }
    assert plan.source_hash.startswith("sha256:")
    assert plan.requires_preview is False
    assert plan.mutation_allowed is True


def test_low_confidence_plan_requires_preview_or_explicit_confirmation():
    pipeline = _pipeline()
    document = pipeline.extract_structured_document(**FIXTURES["ambiguous"])

    preview = pipeline.render_replacement_plan(document)
    confirmed = pipeline.render_replacement_plan(document, confirm=True)

    assert preview.requires_preview is True
    assert preview.mutation_allowed is False
    assert preview.diagnostics == ["two_columns_overlap"]
    assert confirmed.requires_preview is False
    assert confirmed.mutation_allowed is True


def test_equivalent_structure_has_equivalent_treatment_for_different_source_terms():
    pipeline = _pipeline()
    first = pipeline.extract_structured_document(
        source_path="first.txt",
        pages=[{"number": 1, "reading_order_confidence": 1.0, "blocks": [{"type": "heading", "level": 2, "text": "One"}]}],
    )
    second = pipeline.extract_structured_document(
        source_path="second.txt",
        pages=[{"number": 1, "reading_order_confidence": 1.0, "blocks": [{"type": "heading", "level": 2, "text": "Two"}]}],
    )

    assert pipeline.render_replacement_plan(first).block_summary == pipeline.render_replacement_plan(second).block_summary

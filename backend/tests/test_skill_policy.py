from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SKILL_NAME = "feishu-personal-cli"


def _backend_production_files():
    for path in BACKEND_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(BACKEND_ROOT)
        if "__pycache__" in relative.parts:
            continue
        if relative.parts and relative.parts[0] == "tests":
            continue
        if relative.parts and relative.parts[0] == "skills":
            continue
        if path.suffix.lower() not in {".py", ".md", ".json", ".txt"}:
            continue
        yield path


def test_backend_policy_does_not_hardcode_specific_skill_routing():
    offenders = []
    for path in _backend_production_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN_SKILL_NAME in text:
            offenders.append(str(path.relative_to(BACKEND_ROOT)))

    assert offenders == []


def test_rag_code_does_not_encode_entity_specific_routing_maps():
    forbidden_patterns = [
        "official_domain",
        "entity_to_domain",
        "entity_to_keyword",
        "brand_name",
        "school_name",
        "company_name",
    ]
    offenders = []
    rag_root = BACKEND_ROOT / "rag"
    for path in rag_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for pattern in forbidden_patterns:
            if pattern in text:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{pattern}")

    assert offenders == []

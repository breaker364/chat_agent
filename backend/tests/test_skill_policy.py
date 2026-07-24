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

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.skills import SkillDefinition, execute_skill


def test_repair_is_rejected_from_append_while_explicit_add_and_replace_keep_distinct_operations():
    skill = SkillDefinition(name="remote-command-runner", description="", prompt_template="", source_path=".")
    calls = []

    def fake_runner(_skill, params, _root):
        calls.append(dict(params))
        return '{"verified":true}'

    async def invoke(request):
        with patch("backend.skills.get_installed_skill", return_value=skill), patch(
            "backend.skills._skill_runner_path", return_value=Path("runner.py")
        ), patch("backend.skills._execute_skill_runner_sync", side_effect=fake_runner):
            return await execute_skill(Path.cwd(), skill.name, {"request": request})

    with pytest.raises(ValueError, match="append.*replacement"):
        asyncio.run(invoke("lark doc append doc-token --workflow repair --md-file repaired.md"))
    asyncio.run(invoke("lark doc append doc-token --workflow add-content --text section"))
    asyncio.run(invoke("lark doc replace doc-token --md-file repaired.md"))

    assert [call["manifest"]["operation"] for call in calls] == ["append", "replace"]
    assert calls[1]["manifest"]["verification_mode"] == "read_back"

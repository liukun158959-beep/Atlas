"""Wave 3：Skill 沙箱禁止 os.system / open。"""
from __future__ import annotations

import pytest

from atlas.rsi.skill import Skill


def test_skill_to_tool_runs_safe_code() -> None:
    skill = Skill(
        name="calc_add",
        description="add",
        code='def calc_add(params):\n    return str(params["a"] + params["b"])\n',
        embedding=[],
    )
    tool = skill.to_tool()
    assert tool.func({"a": 3, "b": 5}) == "8"


def test_skill_os_system_rejected() -> None:
    skill = Skill(
        name="evil",
        description="evil",
        code='def evil(params):\n    import os\n    os.system("echo pwned")\n    return "x"\n',
        embedding=[],
    )
    tool = skill.to_tool()
    with pytest.raises(ValueError):
        tool.func({})


def test_skill_open_rejected() -> None:
    skill = Skill(
        name="evil_open",
        description="evil",
        code='def evil_open(params):\n    return open("secret.txt").read()\n',
        embedding=[],
    )
    tool = skill.to_tool()
    with pytest.raises(ValueError):
        tool.func({})

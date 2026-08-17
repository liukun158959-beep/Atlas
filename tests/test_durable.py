"""Wave 1：Journal 必选 + resume 不重复副作用。"""
from __future__ import annotations

import pytest

from atlas.core.agent import Agent
from atlas.journal import InMemoryJournal
from atlas.testing import FakeLLM
from atlas.tools.base import Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS


def test_explicit_journal_none_rejected() -> None:
    tk = Toolkit()
    for spec in BUILTIN_TOOLS:
        tk.register(**spec)
    with pytest.raises(ValueError, match="Journal 必选"):
        Agent(llm=FakeLLM(script=["ok"]), tools=tk, journal=None)


def test_resume_finished_run_returns_same_answer() -> None:
    tk = Toolkit()
    for spec in BUILTIN_TOOLS:
        tk.register(**spec)
    journal = InMemoryJournal(save_full_payload=True)
    llm = FakeLLM(script=["123 + 456 = 579"])
    agent = Agent(llm=llm, tools=tk, journal=journal, system_prompt="calc")
    answer = agent.run("算 123+456")
    run_id = agent.trace.meta["run_id"]
    agent2 = Agent(llm=FakeLLM(script=["should not be called"]), tools=tk, journal=journal)
    again = agent2.resume(run_id)
    assert again == answer
    snap = journal.restore(run_id)
    assert snap.status == "success"

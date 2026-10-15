import asyncio
from typing import Any

from test_domain import make_state

from simcore.agent.roles import narrator
from simcore.agent.tools import AgentTools
from simcore.domain.models import Fact, Office, TurnReport
from simcore.llm.base import ChatResult


class FixedProvider:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, messages: list[dict[str, Any]], **_: Any) -> ChatResult:
        return ChatResult(content=self.content)


def test_preview_checks_each_proposal_against_previously_accepted_ones() -> None:
    state = make_state()
    state.characters["deputy"] = state.characters["minister"].model_copy(update={"id": "deputy", "name": "李某"})
    receipt = AgentTools(state).propose_ops([
        {"op": "appoint", "character": "minister", "office": "governor"},
        {"op": "appoint", "character": "deputy", "office": "governor"},
        {"op": "adjust_resource", "resource": "treasury", "delta": -90},
        {"op": "start_project", "name": "修堤", "region": "capital", "cost_per_turn": {"treasury": 20}, "turns": 1},
    ])
    assert [item["status"] for item in receipt] == ["applied", "rejected", "applied", "rejected"]
    assert receipt[1]["code"] == "OFFICE_OCCUPIED"
    assert receipt[3]["code"] == "INSUFFICIENT_RESOURCE"
    assert state.offices["governor"].holder is None


def test_preview_allows_dismiss_then_appoint_transfer() -> None:
    state = make_state()
    state.offices["censor"] = Office(id="censor", name="御史")
    state.offices["governor"].holder = "minister"
    state.characters["minister"].office = "governor"
    receipt = AgentTools(state).propose_ops([
        {"op": "dismiss", "character": "minister", "office": "governor"},
        {"op": "appoint", "character": "minister", "office": "censor"},
    ])
    assert [item["status"] for item in receipt] == ["applied", "applied"]


def test_narrator_accepts_numbers_matching_facts_numerically() -> None:
    report = TurnReport(
        turn=1,
        facts=[
            Fact(kind="resource", text="国库变化", entity="treasury", before=100.0, after=20.0),
            Fact(kind="project_progress", text="修堤进度 1/3", entity="project-1"),
        ],
    )
    text = "国库由 100 两减至 20 两，减少 80 两；修堤进度 1/3。"
    assert asyncio.run(narrator(report, FixedProvider(text))) == text
    invented = "国库由 100 两减至 20 两，另有 7 两去向不明。"
    assert asyncio.run(narrator(report, FixedProvider(invented))) == "国库变化；修堤进度 1/3"

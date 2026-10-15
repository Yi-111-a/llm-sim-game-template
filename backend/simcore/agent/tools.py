from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState
from simcore.domain.ops import parse_op
from simcore.domain.rules import Rejected, validate


class AgentTools:
    def __init__(self, state: WorldState) -> None:
        self.state = state
        self.proposals: list[dict[str, Any]] = []

    def query_state(self, path: str) -> Any:
        value: Any = self.state.model_dump(mode="json")
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                value = value[int(part)]
            else:
                return None
        return value

    def list_entities(self, kind: str) -> list[dict[str, Any]]:
        registry = {
            "character": self.state.characters,
            "region": self.state.regions,
            "faction": self.state.factions,
            "army": self.state.armies,
        }.get(kind, {})
        return [item.model_dump(mode="json") for item in registry.values()]

    def get_ledger(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.state.ledger]

    def propose_ops(self, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        receipt: list[dict[str, Any]] = []
        for raw_op in ops:
            try:
                op = parse_op(raw_op)
                result = validate(op, self.state)
                if isinstance(result, Rejected):
                    receipt.append({"op": op.model_dump(mode="json"), "status": "rejected", "reason": result.message, "code": result.code})
                else:
                    self.proposals.append(op.model_dump(mode="json"))
                    receipt.append({"op": op.model_dump(mode="json"), "status": "applied", "reason": ""})
            except (ValueError, TypeError) as exc:
                receipt.append({"op": raw_op, "status": "rejected", "reason": str(exc), "code": "INVALID_TARGET"})
        return receipt

    def ask_clarification(self, question: str) -> dict[str, str]:
        return {"status": "needs_clarification", "question": question}

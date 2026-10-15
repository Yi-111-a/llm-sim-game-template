from __future__ import annotations

from typing import Any

from simcore.domain.apply import apply
from simcore.domain.models import WorldState
from simcore.domain.ops import describe_op_error, parse_op
from simcore.domain.permissions import check_player_op
from simcore.domain.rules import Rejected, validate

PLAYER_TIER = "player"
SCENARIO_TIER = "scenario"


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

    def propose_ops(self, ops: list[dict[str, Any]], tier: str = SCENARIO_TIER) -> list[dict[str, Any]]:
        """逐条校验并预演。tier="player"（客户端 ops、诏令文本解释）先过玩家权限（permissions.check_player_op），
        不在白名单内的操作以 OP_NOT_PERMITTED 拒绝；tier="scenario"（奏疏选项等剧本内容）只做规则校验。"""
        receipt: list[dict[str, Any]] = []
        for raw_op in ops:
            try:
                if tier == PLAYER_TIER and isinstance(raw_op, dict) and raw_op.get("op") == "set_army_commander":
                    raw_op = {"dispatch": True, **raw_op}  # 玩家任帅：人不在军中则自动赴任
                op = parse_op(raw_op)
                denied = check_player_op(op, self.state) if tier == PLAYER_TIER else None
                result = denied or validate(op, self.state)
                if isinstance(result, Rejected):
                    receipt.append({"op": op.model_dump(mode="json"), "status": "rejected", "reason": result.message, "code": result.code})
                else:
                    # 后续提案基于已接受的提案检查，预览与确认时的顺序执行一致。
                    self.state, _ = apply(op, self.state)
                    self.proposals.append(op.model_dump(mode="json"))
                    receipt.append({"op": op.model_dump(mode="json"), "status": "applied", "reason": ""})
            except (ValueError, TypeError) as exc:
                receipt.append({"op": raw_op, "status": "rejected", "reason": describe_op_error(exc, raw_op), "code": "INVALID_TARGET"})
        return receipt

    def ask_clarification(self, question: str) -> dict[str, str]:
        return {"status": "needs_clarification", "question": question}

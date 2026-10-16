from __future__ import annotations

from typing import Any

from simcore.domain.apply import apply
from simcore.domain.models import WorldState
from simcore.domain.ops import describe_op_error, parse_op
from simcore.domain.permissions import check_player_op
from simcore.domain.rules import Rejected, validate

PLAYER_TIER = "player"
SCENARIO_TIER = "scenario"
# 玩家层提议缺省 dispatch=true 的操作（一条任命规则：君主亲任即遣赴任，剧本 / 事件按其写明的值，缺省遥领）
DISPATCH_OPS = frozenset({"set_army_commander", "appoint"})


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
                if tier == PLAYER_TIER and isinstance(raw_op, dict) and raw_op.get("op") in DISPATCH_OPS:
                    raw_op = {"dispatch": True, **raw_op}  # 玩家任帅 / 任有任所的官职：人不在任所则启程赴任（写 dispatch: false 为遥领）
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


def cascade_delays(state: WorldState, clauses: list[dict[str, Any]]) -> list[int]:
    """预览按「全部条款都执行」逐条校验；之后有的条款被判延期（驿程、有司稽迟、将领自主），本季照常执行的条款
    若倚赖它（如先令某人致仕、再以他人补其官缺，而致仕被延期），确认时就会被拒。

    这里只用本季照常执行的条款重演一遍：其中此刻行不通、而算上延期条款才行得通的，随之延至下季
    （delay_kind=dependency）；两样都行不通的保持原状（确认时照常报错）。就地修改 clauses，返回改动的条款序号（从 1 起）。
    """
    if not any(clause.get("status") == "delayed" for clause in clauses):
        return []
    working = state
    changed: list[int] = []
    for index, clause in enumerate(clauses, start=1):
        op = clause.get("op")
        if clause.get("status") != "applied" or not isinstance(op, dict) or not op:
            continue
        try:
            parsed = parse_op(op)
            result = validate(parsed, working)
        except (ValueError, TypeError):
            continue
        if isinstance(result, Rejected):
            clause.update({
                "status": "delayed",
                "delay_kind": "dependency",
                "delay_reason": f"所倚之条款延至下季，此条随之延期（若此刻执行：{result.message}）",
                "due_turn": state.turn + 1,
            })
            changed.append(index)
            continue
        working, _ = apply(parsed, working)
    return changed

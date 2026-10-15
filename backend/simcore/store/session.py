from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simcore.domain.apply import apply, queue_delayed
from simcore.domain.models import TurnReport, WorldState
from simcore.domain.ops import parse_op
from simcore.domain.turn import resolve_turn


def state_hash(state: WorldState) -> str:
    encoded = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SessionStore:
    def __init__(
        self,
        path: Path,
        scenario: dict[str, Any],
        initial_state: WorldState | None = None,
        seed: int | None = None,
    ) -> None:
        self.path = path
        self.scenario = scenario
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.header, self.entries = self._read()
            self.seed = int(self.header["seed"])
            self.initial_state = WorldState.model_validate(self.header["initial_state"])
        else:
            if initial_state is None:
                raise ValueError("initial_state is required for a new session")
            self.seed = seed if seed is not None else 0
            self.initial_state = initial_state.model_copy(deep=True)
            self.header = {
                "type": "session",
                "version": 1,
                "id": path.stem,
                "timestamp": _now(),
                "scenario": initial_state.scenario_id,
                "seed": self.seed,
                "initial_state": initial_state.model_dump(mode="json"),
                "parentSession": None,
            }
            self.entries: list[dict[str, Any]] = []
            self._write()
        self._state_cache: dict[str, WorldState] = {}

    @property
    def head_id(self) -> str | None:
        return self.entries[-1]["id"] if self.entries else None

    @property
    def state(self) -> WorldState:
        return self.state_at(self.head_id)

    def append_op(self, op: dict[str, Any]) -> tuple[WorldState, list[Any], str]:
        current = self.state
        updated, facts = apply(op, current)
        entry = self._append("op", {"op": parse_op(op).model_dump(mode="json"), "state_hash": state_hash(updated)})
        self._state_cache[entry["id"]] = updated.model_copy(deep=True)
        return updated, facts, entry["id"]

    def append_turn(self) -> tuple[WorldState, TurnReport, str]:
        current = self.state
        rng = random.Random(self.seed + current.turn)
        updated, report = resolve_turn(current, rng, self.scenario)
        entry = self._append(
            "turn",
            {
                "report": report.model_dump(mode="json"),
                "state_hash": state_hash(updated),
            },
        )
        self._state_cache[entry["id"]] = updated.model_copy(deep=True)
        return updated, report, entry["id"]

    def append_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        entry = self._append("message", {"role": role, "content": content, "metadata": metadata or {}})
        return entry["id"]

    def append_advice(self, character: str, text: str, source: str = "player") -> dict[str, Any]:
        """谏言库条目：只记入日志，不改变 WorldState，也不进入状态哈希。"""
        entry = self._append(
            "advice",
            {"character": character, "text": text, "source": source, "turn": self.state.turn},
        )
        return {"id": entry["id"], **entry["payload"]}

    def advice(self) -> list[dict[str, Any]]:
        return [
            {"id": entry["id"], **entry["payload"]}
            for entry in self._path_to(self.head_id)
            if entry["type"] == "advice"
        ]

    def head_turn(self) -> int:
        """不重放即可得到的当前回合（初始回合 + 当前路径上的回合推进次数）。"""
        return self.initial_state.turn + sum(1 for entry in self._path_to(self.head_id) if entry["type"] == "turn")

    def last_report(self) -> dict[str, Any] | None:
        for entry in reversed(self._path_to(self.head_id)):
            if entry["type"] == "turn":
                report = entry["payload"].get("report")
                return report if isinstance(report, dict) else None
        return None

    def messages(self, role: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": entry["id"], **entry["payload"]}
            for entry in self._path_to(self.head_id)
            if entry["type"] == "message" and (role is None or entry["payload"].get("role") == role)
        ]

    def timeline(self) -> list[dict[str, Any]]:
        """全部分支的时间线：[{id, type, turn, label, parentId, on_path, head}]；分支标记节点被折叠。"""
        indexed = {item["id"]: item for item in self.entries}

        def visible_parent(parent: str | None) -> str | None:
            while parent is not None and indexed.get(parent, {}).get("type") == "branch":
                parent = indexed[parent]["parentId"]
            return parent

        on_path = {item["id"] for item in self._path_to(self.head_id)}
        turns: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry["type"] == "branch":
                continue
            parent = visible_parent(entry["parentId"])
            base = turns.get(parent, self.initial_state.turn) if parent else self.initial_state.turn
            turn = int(entry["payload"].get("report", {}).get("turn", base + 1)) if entry["type"] == "turn" else base
            turns[entry["id"]] = turn
            result.append({
                "id": entry["id"],
                "type": entry["type"],
                "turn": turn,
                "label": _entry_label(entry),
                "parentId": parent,
                "timestamp": entry.get("timestamp"),
                "on_path": entry["id"] in on_path,
                "head": False,
            })
        head = visible_parent(self.head_id)
        for row in result:
            row["head"] = row["id"] == head
        return result

    def append_pending(
        self, ops: list[dict[str, Any]], due_turn: int, source: str = "edict"
    ) -> tuple[WorldState, str]:
        state = queue_delayed(self.state, ops, due_turn, source)
        entry = self._append(
            "pending",
            {
                "ops": ops,
                "due_turn": due_turn,
                "source": source,
                "state_hash": state_hash(state),
            },
        )
        return state, entry["id"]

    def rewind(self, entry_id: str | None) -> str:
        if entry_id is not None and not any(item["id"] == entry_id for item in self.entries):
            raise ValueError(f"Unknown session entry: {entry_id}")
        previous = self.head_id
        return self._append("branch", {"rewound_from": previous}, parent_id=entry_id)["id"]

    def fork(self, path: Path, entry_id: str | None = None) -> SessionStore:
        if entry_id is None:
            entry_id = self.head_id
        selected = self._path_to(entry_id)
        forked = SessionStore(path, self.scenario, self.initial_state, self.seed)
        forked.entries = [dict(item) for item in selected]
        forked.header["parentSession"] = str(self.path)
        forked._write()
        return forked

    def history(self) -> list[dict[str, Any]]:
        return self._path_to(self.head_id)

    def replay(self, entry_id: str | None = None) -> tuple[WorldState, str]:
        state = self.initial_state.model_copy(deep=True)
        for entry in self._path_to(self.head_id if entry_id is None else entry_id):
            if entry["type"] == "op":
                state, _ = apply(entry["payload"]["op"], state)
            elif entry["type"] == "pending":
                state = queue_delayed(
                    state,
                    entry["payload"]["ops"],
                    entry["payload"]["due_turn"],
                    entry["payload"].get("source", "edict"),
                )
                if state_hash(state) != entry["payload"]["state_hash"]:
                    raise ValueError(f"Replay hash mismatch at entry {entry['id']}")
            elif entry["type"] == "turn":
                state, report = resolve_turn(state, random.Random(self.seed + state.turn), self.scenario)
                expected = entry["payload"]["state_hash"]
                if report.state_hash != expected:
                    raise ValueError(f"Replay hash mismatch at entry {entry['id']}")
        return state, state_hash(state)

    def state_at(self, entry_id: str | None) -> WorldState:
        path = self._path_to(entry_id)
        state = self.initial_state.model_copy(deep=True)
        start = 0
        for index in range(len(path) - 1, -1, -1):
            cached = self._state_cache.get(path[index]["id"])
            if cached is not None:
                state = cached.model_copy(deep=True)
                start = index + 1
                break
        for entry in path[start:]:
            state = self._step(entry, state)
            self._state_cache[entry["id"]] = state.model_copy(deep=True)
        return state

    def _step(self, entry: dict[str, Any], state: WorldState) -> WorldState:
        if entry["type"] == "op":
            state, _ = apply(entry["payload"]["op"], state)
        elif entry["type"] == "pending":
            state = queue_delayed(
                state,
                entry["payload"]["ops"],
                entry["payload"]["due_turn"],
                entry["payload"].get("source", "edict"),
            )
        elif entry["type"] == "turn":
            state, _ = resolve_turn(state, random.Random(self.seed + state.turn), self.scenario)
        return state

    def reload(self) -> None:
        """从磁盘重读（另一进程写入同一存档后调用）。日志只追加、节点不可变，缓存按节点 id 保持有效。"""
        self.header, self.entries = self._read()

    def _append(
        self, entry_type: str, payload: dict[str, Any], parent_id: str | None = None
    ) -> dict[str, Any]:
        entry = {
            "type": entry_type,
            "id": uuid.uuid4().hex[:12],
            "parentId": self.head_id if parent_id is None else parent_id,
            "timestamp": _now(),
            "payload": payload,
        }
        self.entries.append(entry)
        self._write()
        return entry

    def _path_to(self, entry_id: str | None) -> list[dict[str, Any]]:
        if entry_id is None:
            return []
        indexed = {item["id"]: item for item in self.entries}
        chain: list[dict[str, Any]] = []
        current = entry_id
        while current is not None:
            entry = indexed.get(current)
            if entry is None:
                raise ValueError(f"Unknown session entry: {current}")
            chain.append(entry)
            current = entry["parentId"]
        chain.reverse()
        return [entry for entry in chain if entry["type"] != "branch"]

    def _read(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lines = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        if not lines or lines[0].get("type") != "session":
            raise ValueError(f"Invalid session file: {self.path}")
        return lines[0], lines[1:]

    def _write(self) -> None:
        lines = [self.header, *self.entries]
        self.path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in lines),
            encoding="utf-8",
        )


_OP_LABELS = {
    "adjust_resource": "调整资源",
    "add_ledger": "新增账目",
    "cancel_ledger": "取消账目",
    "appoint": "任命",
    "dismiss": "免职",
    "set_character_status": "人物状态",
    "move_character": "人物行程",
    "move_army": "调动军队",
    "start_project": "兴工",
    "adjust_region": "地区调整",
    "adjust_relation": "关系调整",
    "add_standing_order": "常设令",
    "cancel_standing_order": "撤常设令",
    "set_flag": "旗标",
    "remember": "人物记忆",
    "adjust_army": "兵力调整",
    "set_army_faction": "军队改隶",
    "set_army_commander": "军队易帅",
    "adjust_trait": "心态调整",
    "adjust_counter": "计数调整",
    "set_counter": "计数设定",
    "set_region_controller": "地区易手",
    "choose_event_option": "事件抉择",
}


def _entry_label(entry: dict[str, Any]) -> str:
    payload = entry.get("payload", {})
    kind = entry["type"]
    if kind == "op":
        op = payload.get("op", {})
        return f"{_OP_LABELS.get(op.get('op'), op.get('op'))}"
    if kind == "turn":
        report = payload.get("report", {})
        outcome = report.get("outcome")
        suffix = f"·终局：{outcome.get('title')}" if isinstance(outcome, dict) else ""
        return f"推进至第{report.get('turn')}回合{suffix}"
    if kind == "pending":
        return f"延期条款（第{payload.get('due_turn')}回合执行）"
    if kind == "advice":
        return f"谏言：{str(payload.get('text', ''))[:20]}"
    if kind == "message":
        return f"{payload.get('role', '')}：{str(payload.get('content', ''))[:20]}"
    return kind


def _now() -> str:
    return datetime.now(UTC).isoformat()

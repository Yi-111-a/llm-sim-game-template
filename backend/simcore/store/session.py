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
        return updated, report, entry["id"]

    def append_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        entry = self._append("message", {"role": role, "content": content, "metadata": metadata or {}})
        return entry["id"]

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
        state = self.initial_state.model_copy(deep=True)
        for entry in self._path_to(entry_id):
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


def _now() -> str:
    return datetime.now(UTC).isoformat()

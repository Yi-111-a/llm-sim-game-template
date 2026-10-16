from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simcore.domain.apply import apply, queue_delayed
from simcore.domain.models import TurnReport, WorldState, clone, clone_data
from simcore.domain.ops import parse_op
from simcore.domain.turn import resolve_turn


def state_hash(state: WorldState) -> str:
    encoded = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scenario_fingerprint(scenario: dict[str, Any]) -> dict[str, Any]:
    """场景身份：id、version 与内容哈希（全部 YAML 解析结果，不含所在路径）。存档头记录它，读档时比对。"""
    definition = scenario.get("scenario", {}) or {}
    content = {key: value for key, value in scenario.items() if key != "root"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "scenario_version": str(definition.get("version", "")),
        "scenario_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _dumps(row: dict[str, Any]) -> str:
    """存档行：紧凑 JSON（含状态快照，省去空格可小三成）。

    不排序键：开局状态与快照里的资源、地区、军队等按剧本顺序保存，读回后的遍历顺序（显示、结算）与进程内一致。
    """
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


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
            self.initial_state = clone(initial_state)
            self.header = {
                "type": "session",
                "version": 1,
                "id": path.stem,
                "timestamp": _now(),
                "scenario": initial_state.scenario_id,
                "seed": self.seed,
                **scenario_fingerprint(scenario),
                "initial_state": initial_state.model_dump(mode="json"),
                "parentSession": None,
            }
            difficulty = initial_state.rules.get("difficulty")
            if isinstance(difficulty, dict):
                self.header["difficulty"] = difficulty.get("id")
            self.entries: list[dict[str, Any]] = []
            self._write()
        # 状态快照缓存：节点 id → 该节点之后的状态。快照只读（对外一律返回副本），状态不变的节点共享同一快照。
        self._state_cache: dict[str, WorldState] = {}
        self._index: dict[str, dict[str, Any]] = {item["id"]: item for item in self.entries}
        self._drafts_cache: tuple[tuple[int, int], dict[str, Any]] | None = None

    @property
    def scenario_changed(self) -> dict[str, Any] | None:
        """存档所用剧本与当前剧本内容不同时返回说明（旧存档头没有哈希时返回 None）。

        读档以状态快照为准，剧本改动不会追溯改变已结算的回合；之后的回合按当前剧本结算。
        """
        recorded = self.header.get("scenario_hash")
        if not recorded:
            return None
        current = scenario_fingerprint(self.scenario)
        if current["scenario_hash"] == recorded:
            return None
        return {
            "saved_version": self.header.get("scenario_version"),
            "current_version": current["scenario_version"],
            "saved_hash": str(recorded)[:12],
            "current_hash": current["scenario_hash"][:12],
            "message": "剧本自开局以来已改动：已结算的回合以存档快照为准，此后按新剧本结算。",
        }

    @property
    def head_id(self) -> str | None:
        return self.entries[-1]["id"] if self.entries else None

    @property
    def state(self) -> WorldState:
        """当前状态的独立副本（调用方可以修改）。"""
        return clone(self._snapshot(self.head_id))

    def head_state(self) -> WorldState:
        """当前状态的只读快照：不复制，调用方不得修改。"""
        return self._snapshot(self.head_id)

    def append_op(self, op: dict[str, Any], source: str | None = None) -> tuple[WorldState, list[Any], str]:
        updated, facts = apply(op, self.head_state(), source=source)
        payload: dict[str, Any] = {"op": parse_op(op).model_dump(mode="json"), "state_hash": state_hash(updated)}
        if source:
            payload["source"] = source
        entry = self._append("op", payload)
        self._state_cache[entry["id"]] = clone(updated)
        return updated, facts, entry["id"]

    def append_ops(self, ops: list[dict[str, Any]], sources: list[str | None] | None = None) -> list[dict[str, Any]]:
        """顺序执行一批操作（诏书确认用）：只复制一次状态；被拒的 op 不写入日志。

        sources 与 ops 等长（可省略），为每条操作的来源说明（奏疏标题等），写进事实原因。
        返回与 ops 等长的结果：{ok: True, facts, id} 或 {ok: False, code, message}。
        """
        state = clone(self.head_state())
        results: list[dict[str, Any]] = []
        last_id: str | None = None
        for index, op in enumerate(ops):
            source = sources[index] if sources is not None and index < len(sources) else None
            try:
                state, facts = apply(op, state, inplace=True, source=source)
            except ValueError as exc:
                results.append({"ok": False, "code": getattr(exc, "code", "INVALID_TARGET"), "message": getattr(exc, "message", str(exc))})
                continue
            payload: dict[str, Any] = {"op": parse_op(op).model_dump(mode="json"), "state_hash": state_hash(state)}
            if source:
                payload["source"] = source
            entry = self._append("op", payload)
            last_id = entry["id"]
            results.append({"ok": True, "facts": facts, "id": entry["id"]})
        if last_id is not None:
            self._state_cache[last_id] = state  # 私有副本，此后不再修改
        return results

    def append_turn(self) -> tuple[WorldState, TurnReport, str]:
        current = self.head_state()
        rng = random.Random(self.seed + current.turn)
        updated, report = resolve_turn(current, rng, self.scenario)
        self._complete_report(updated, report)
        # 每个回合节点都带结算后的完整状态快照：读档从最近的快照起步，不再用（可能已改动的）剧本重放旧回合。
        entry = self._append(
            "turn",
            {
                "report": report.model_dump(mode="json"),
                "state_hash": report.state_hash or state_hash(updated),
                "snapshot": updated.model_dump(mode="json"),
            },
        )
        self._state_cache[entry["id"]] = clone(updated)
        return updated, report, entry["id"]

    def _complete_report(self, updated: WorldState, report: TurnReport) -> None:
        """补全季报：对账的期初、诏令与季前抉择三段（来自上次结算以来的日志），以及危机计的公开快照。

        期初 = 上次结算后的值；诏令 = 期间各操作条目的变化（choose_event_option 计入「事件/选择」）；
        其后三段（延期执行、经常收支、事件/选择）由 resolve_turn 给出。各段相加恰为期末 − 期初。
        """
        from simcore.domain.visibility import public_crisis, public_crisis_change

        path = self._path_to(self.head_id)
        start_index = max((index for index, entry in enumerate(path) if entry["type"] == "turn"), default=-1)
        opening_state = self._snapshot(path[start_index]["id"]) if start_index >= 0 else self.initial_state
        previous = opening_state
        choices: dict[str, float] = {}
        change = report.crisis.get("change") if isinstance(report.crisis, dict) else None
        crisis_id = str(change.get("resource")) if isinstance(change, dict) else ""
        pre_items: list[dict[str, Any]] = []  # 两次结算之间（诏令、季前抉择）对危机值的逐条贡献
        for entry in path[start_index + 1:]:
            after = self._snapshot(entry["id"])
            op = (entry["payload"].get("op") or {}) if entry["type"] == "op" else {}
            if entry["type"] == "op" and op.get("op") == "choose_event_option":
                for key, row in after.resources.items():
                    before = previous.resources[key].amount if key in previous.resources else row.amount
                    choices[key] = choices.get(key, 0.0) + row.amount - before
            if crisis_id and entry["type"] == "op" and crisis_id in after.resources and crisis_id in previous.resources:
                delta = after.resources[crisis_id].amount - previous.resources[crisis_id].amount
                if abs(delta) > 1e-12:
                    if op.get("op") == "choose_event_option":
                        choice = next((row for row in previous.pending_choices if row.id == op.get("choice_id")), None)
                        pre_items.append({"kind": "choice", "label": choice.title if choice else str(op.get("choice_id")), "amount": delta})
                    else:
                        pre_items.append({"kind": "edict", "label": str(entry["payload"].get("source") or "诏令"), "amount": delta})
            previous = after
        if isinstance(change, dict) and crisis_id in opening_state.resources:
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for item in pre_items:
                slot = merged.setdefault((item["kind"], item["label"]), {**item, "amount": 0.0})
                slot["amount"] += item["amount"]
            change["items"] = [*merged.values(), *change.get("items", [])]
            change["opening"] = opening_state.resources[crisis_id].amount
        for key, row in report.reconciliation.items():
            opening = opening_state.resources[key].amount if key in opening_state.resources else row["start"]
            choice = choices.get(key, 0.0)
            row["opening"] = opening
            row["edicts"] = row["start"] - opening - choice
            row["events"] = row["events"] + choice
            row.pop("start", None)
        crisis = (self.scenario.get("scenario", {}) or {}).get("crisis")
        if crisis and report.crisis:
            report.crisis["public"] = public_crisis(crisis, updated, self.seed)
            split = public_crisis_change(crisis, report.crisis.get("change"), updated, self.seed)
            if split is not None:
                report.crisis["public_change"] = split

    def append_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> str:
        entry = self._append("message", {"role": role, "content": content, "metadata": metadata or {}})
        return entry["id"]

    def append_edict(self, payload: dict[str, Any]) -> str:
        """确认诏书回执的公开摘要（export_log 用）：记入日志（随分支走），不改变 WorldState，也不进入状态哈希。"""
        return str(self._append("edict", payload)["id"])

    @property
    def receipts_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.receipts.jsonl")

    def record_draft(self, payload: dict[str, Any]) -> None:
        """拟诏回执的公开摘要写进旁路文件 <session>.receipts.jsonl（拟诏不改日志、不移动 head）；带 base_head 以便按分支筛选。"""
        with self.receipts_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_dumps({**payload, "base_head": self.head_id, "timestamp": _now()}) + "\n")

    def draft_records(self) -> list[dict[str, Any]]:
        try:
            lines = self.receipts_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    @property
    def calls_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.calls.jsonl")

    def record_call(self, payload: dict[str, Any]) -> None:
        """client 模式的召对 / 廷议调用的轻量记录（谁、方式、议题、回合；不含台词）写进旁路文件 <session>.calls.jsonl：
        不改日志、不移动 head（草案的「状态未变」判定不受影响）；带 base_head 以便 export_log 按分支筛选。"""
        with self.calls_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_dumps({**payload, "base_head": self.head_id, "timestamp": _now()}) + "\n")

    def call_records(self) -> list[dict[str, Any]]:
        try:
            lines = self.calls_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def append_advice(self, character: str, text: str, source: str = "player") -> dict[str, Any]:
        """谏言库条目：只记入日志，不改变 WorldState，也不进入状态哈希。"""
        entry = self._append(
            "advice",
            {"character": character, "text": text, "source": source, "turn": self.head_state().turn},
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

    def ops_since_turn(self) -> list[dict[str, Any]]:
        """当前路径上最近一次回合结算之后写入的操作条目的 payload（{op, source?, state_hash}）。"""
        rows: list[dict[str, Any]] = []
        for entry in reversed(self._path_to(self.head_id)):
            if entry["type"] == "turn":
                break
            if entry["type"] == "op":
                rows.append(entry["payload"])
        rows.reverse()
        return rows

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
        self, ops: list[dict[str, Any]], due_turn: int, source: str = "edict", label: str = ""
    ) -> tuple[WorldState, str]:
        state = queue_delayed(self.head_state(), ops, due_turn, source, label)
        payload: dict[str, Any] = {"ops": ops, "due_turn": due_turn, "source": source, "state_hash": state_hash(state)}
        if label:
            payload["label"] = label
        entry = self._append("pending", payload)
        self._state_cache[entry["id"]] = clone(state)
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
        for key in ("scenario_version", "scenario_hash", "difficulty"):
            if key in self.header:
                forked.header[key] = self.header[key]
            else:
                forked.header.pop(key, None)
        forked.entries = [dict(item) for item in selected]
        forked._index = {item["id"]: item for item in forked.entries}
        forked._state_cache = {item["id"]: self._state_cache[item["id"]] for item in selected if item["id"] in self._state_cache}
        forked.header["parentSession"] = str(self.path)
        forked._write()
        return forked

    def history(self) -> list[dict[str, Any]]:
        return self._path_to(self.head_id)

    def verify_replay(self) -> dict[str, Any]:
        """校验工具（`sim replay`）：用当前剧本从开局完整重放当前路径，逐个回合节点比对存档里的状态哈希。

        不修改存档。发现分歧时记录 {entry_id, turn, expected, actual}，并从该节点的存档快照（若有）继续，
        以便后续回合各自独立比对。返回 {ok, turns_checked, divergences, final_hash, scenario_changed}。
        """
        state = clone(self.initial_state)
        divergences: list[dict[str, Any]] = []
        checked = 0
        for entry in self._path_to(self.head_id):
            if entry["type"] == "turn":
                state, report = resolve_turn(state, random.Random(self.seed + state.turn), self.scenario)
                checked += 1
                expected = entry["payload"].get("state_hash")
                if expected and report.state_hash != expected:
                    divergences.append({"entry_id": entry["id"], "turn": state.turn, "expected": expected, "actual": report.state_hash})
                    snapshot = entry["payload"].get("snapshot")
                    if isinstance(snapshot, dict):
                        state = WorldState.model_validate(snapshot)
            else:
                state = self._step(entry, state)
        return {
            "ok": not divergences,
            "turns_checked": checked,
            "divergences": divergences,
            "final_hash": state_hash(state),
            "scenario_changed": self.scenario_changed,
        }

    def replay(self, entry_id: str | None = None) -> tuple[WorldState, str]:
        state = clone(self.initial_state)
        for entry in self._path_to(self.head_id if entry_id is None else entry_id):
            if entry["type"] == "op":
                state, _ = apply(entry["payload"]["op"], state)
            elif entry["type"] == "pending":
                state = queue_delayed(
                    state,
                    entry["payload"]["ops"],
                    entry["payload"]["due_turn"],
                    entry["payload"].get("source", "edict"),
                    entry["payload"].get("label", ""),
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
        """某节点之后的状态（独立副本）。"""
        return clone(self._snapshot(entry_id))

    def _snapshot(self, entry_id: str | None) -> WorldState:
        """某节点之后的状态快照（只读、不复制）。

        从路径上最近的「内存缓存或存档快照（回合节点的 snapshot）」起步，只重放其后的节点；
        回合节点有快照，所以读档不依赖剧本当前内容。旧存档没有快照时从开局重放。
        """
        if entry_id is None:
            return self.initial_state
        cached = self._state_cache.get(entry_id)
        if cached is not None:
            return cached
        path = self._path_to(entry_id)
        state = self.initial_state
        start = 0
        for index in range(len(path) - 1, -1, -1):
            hit = self._state_cache.get(path[index]["id"])
            if hit is None:
                snapshot = path[index]["payload"].get("snapshot") if path[index]["type"] == "turn" else None
                if isinstance(snapshot, dict):
                    hit = WorldState.model_validate(snapshot)
                    self._state_cache[path[index]["id"]] = hit
            if hit is not None:
                state = hit
                start = index + 1
                break
        for entry in path[start:]:
            state = self._step(entry, state)
            self._state_cache[entry["id"]] = state
        return state

    def _step(self, entry: dict[str, Any], state: WorldState) -> WorldState:
        """由前一快照得到下一快照；不修改传入的快照（apply / queue_delayed / resolve_turn 都返回新对象）。"""
        if entry["type"] == "op":
            state, _ = apply(entry["payload"]["op"], state)
        elif entry["type"] == "pending":
            state = queue_delayed(
                state,
                entry["payload"]["ops"],
                entry["payload"]["due_turn"],
                entry["payload"].get("source", "edict"),
                entry["payload"].get("label", ""),
            )
        elif entry["type"] == "turn":
            state, _ = resolve_turn(state, random.Random(self.seed + state.turn), self.scenario)
        return state

    # ---- 诏书草案（旁路文件，不进日志、不影响状态哈希） ---------------------------------------

    @property
    def drafts_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.drafts.json")

    def drafts(self) -> dict[str, dict[str, Any]]:
        """全部已保存草案 {edict_id: receipt}（独立副本）。

        草案写在 <session>.drafts.json，多进程（桌面应用 / 每次调用都重启的 MCP）共享；
        按文件 mtime/size 缓存，另一进程改写后自动重读。
        """
        try:
            stat = self.drafts_path.stat()
        except OSError:
            self._drafts_cache = None
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._drafts_cache
        if cached is None or cached[0] != signature:
            try:
                data = json.loads(self.drafts_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            cached = (signature, data if isinstance(data, dict) else {})
            self._drafts_cache = cached
        return clone_data(cached[1])  # type: ignore[no-any-return]

    def load_draft(self, edict_id: str) -> dict[str, Any] | None:
        row = self.drafts().get(edict_id)
        return row if isinstance(row, dict) else None

    def save_draft(self, receipt: dict[str, Any], current_turn: int | None = None, keep: int = 32) -> None:
        """保存（或更新）草案；同时清掉早于 current_turn 的过期草案，最多保留 keep 份。"""
        drafts = self.drafts()
        drafts[str(receipt["id"])] = clone_data(receipt)
        if current_turn is not None:
            drafts = {key: row for key, row in drafts.items() if int(row.get("base_turn", current_turn)) >= current_turn}
        if len(drafts) > keep:
            drafts = dict(list(drafts.items())[-keep:])
        _atomic_write(self.drafts_path, json.dumps(drafts, ensure_ascii=False))
        try:
            stat = self.drafts_path.stat()
            self._drafts_cache = ((stat.st_mtime_ns, stat.st_size), drafts)
        except OSError:
            self._drafts_cache = None

    def delete_drafts(self) -> None:
        self._drafts_cache = None
        self.drafts_path.unlink(missing_ok=True)
        self.calls_path.unlink(missing_ok=True)
        self.receipts_path.unlink(missing_ok=True)

    def reload(self) -> None:
        """从磁盘重读（另一进程写入同一存档后调用）。日志只追加、节点不可变，缓存按节点 id 保持有效。"""
        self.header, self.entries = self._read()
        self._index = {item["id"]: item for item in self.entries}

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
        self._index[entry["id"]] = entry
        # 日志只追加：写入一行即可，不必重写整个文件（重写是 O(n)，整局累计 O(n²)）。
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_dumps(entry) + "\n")
        return entry

    def _path_to(self, entry_id: str | None) -> list[dict[str, Any]]:
        if entry_id is None:
            return []
        indexed = self._index
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
            "".join(_dumps(row) + "\n" for row in lines),
            encoding="utf-8",
            newline="\n",
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
    if kind == "edict":
        action = "拟诏" if payload.get("action") == "draft" else "确认诏书"
        return f"{action} {payload.get('edict_id', '')}"
    if kind == "advice":
        return f"谏言：{str(payload.get('text', ''))[:20]}"
    if kind == "message":
        return f"{payload.get('role', '')}：{str(payload.get('content', ''))[:20]}"
    return kind


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，另一进程读到的总是完整文件。Windows 上目标被短暂占用时重试几次。"""
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(text, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 9:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


def _now() -> str:
    return datetime.now(UTC).isoformat()

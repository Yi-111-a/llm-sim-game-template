"""结构化复盘日志（export_log / GET /api/sessions/{id}/export）：按季列出玩家决策与结算结果，只用公开视图。

每季（按「下诏所在季」，即该季开始时的回合 T）：
- adopted：采纳的奏疏选项 {memorial_id, option_id, title, label}（由确认诏书写下的 memorial:<id> 旗标还原，旧存档同样可用）；
- client_ops：客户端 / 诏令文本条款的 op 与回执结果（玩家自拟，原样给出）；
- drafts / confirms：拟诏回执（旁路文件 <session>.receipts.jsonl）与确认回执（日志中的 edict 条目）：逐条 status、source、
  摘要、错误码、延期原因与执行回合；
- delayed：排入下季的延期条款（摘要、执行回合、timing「延至本季末结算时执行（进入第 n 回合时生效）」、来源）；
- choices：事件抉择 {choice_id, option_id, title, option}；
- audiences / debates / advice：召对（人物与方式）、廷议议题、谏言；client 模式的召对 / 廷议另有轻量记录（旁路文件
  <session>.calls.jsonl：人物、方式、议题、参与者、回合，via: "client"，不含台词）；
- end_turn：该季结算（进入 T+1）的事实、事件、战报、对账（含经常收支明细与取整差）、危机计、危机变化拆分、终局与季末公开国势。
隐藏数值只出现为风闻区间（与 public_report / public_view 相同的处理），不导出真值。
"""

from __future__ import annotations

import json
from typing import Any

from simcore.domain.memorials import memorial_item, summarize_ops
from simcore.domain.models import WorldState
from simcore.domain.rules import delay_phrase
from simcore.domain.visibility import battle_breakdown, fact_line, public_report, resource_display

CLAUSE_KEYS = (
    "status",
    "source",
    "summary",
    "text",
    "code",
    "reason",
    "delay_reason",
    "delay_kind",
    "due_turn",
    "memorial_id",
    "option_id",
    "card_id",
    "card_title",
    "autonomy_risk",
    "changed",
    "previous_status",
)


def compact_clauses(clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """回执条款的紧凑公开版：奏疏条款不带原始 op（其中可能有隐藏数值的增减，只给摘要）；玩家自拟条款带 op。"""
    rows = []
    for index, clause in enumerate(clauses, start=1):
        row: dict[str, Any] = {"index": index}
        row.update({key: clause[key] for key in CLAUSE_KEYS if clause.get(key) not in (None, "", False)})
        if clause.get("status") == "delayed":
            row["timing"] = delay_phrase(clause.get("due_turn"))
        if clause.get("source") in {"client", "text", "card"} and clause.get("op"):
            row["op"] = clause["op"]
        rows.append(row)
    return rows


def _season(seasons: dict[int, dict[str, Any]], turn: int, label: str) -> dict[str, Any]:
    if turn not in seasons:
        seasons[turn] = {
            "turn": turn,
            "calendar_label": label,
            "adopted": [],
            "client_ops": [],
            "drafts": [],
            "confirms": [],
            "delayed": [],
            "choices": [],
            "audiences": [],
            "debates": [],
            "advice": [],
            "end_turn": None,
        }
    return seasons[turn]


def build_export(
    session: Any,
    session_id: str,
    crisis: dict[str, Any] | None,
    from_turn: int | None = None,
    to_turn: int | None = None,
) -> dict[str, Any]:
    """沿当前分支（history 路径）整理出逐季记录；from_turn / to_turn 按下诏所在季筛选（含两端）。"""
    seed = session.seed
    scenario = session.scenario
    state: WorldState = session.initial_state
    seasons: dict[int, dict[str, Any]] = {}
    current = _season(seasons, state.turn, state.calendar.label)
    for entry in session.history():
        kind = entry["type"]
        payload = entry.get("payload") or {}
        after: WorldState = session._snapshot(entry["id"])
        if kind == "op":
            op = payload.get("op") or {}
            if op.get("op") == "choose_event_option":
                choice = next((row for row in state.pending_choices if row.id == op.get("choice_id")), None)
                option = next((row for row in choice.options if row.id == op.get("option_id")), None) if choice else None
                current["choices"].append({
                    "choice_id": op.get("choice_id"),
                    "option_id": op.get("option_id"),
                    "title": choice.title if choice else "",
                    "option": option.text if option else "",
                })
            elif op.get("op") == "set_flag" and str(op.get("key", "")).startswith("memorial:"):
                memorial_id = str(op["key"]).split(":", 1)[1]
                item = memorial_item(scenario, memorial_id) or {}
                option = next((row for row in item.get("options", []) or [] if isinstance(row, dict) and row.get("id") == op.get("value")), {})
                current["adopted"].append({
                    "memorial_id": memorial_id,
                    "option_id": op.get("value"),
                    "title": item.get("title", memorial_id),
                    "label": option.get("label", op.get("value")),
                })
        elif kind == "pending":
            current["delayed"].append({
                "label": payload.get("label") or "延期诏令",
                "due_turn": payload.get("due_turn"),
                "timing": delay_phrase(payload.get("due_turn")),
                "summary": summarize_ops(payload.get("ops") or [], state, pending=True),
            })
        elif kind == "edict":
            record = {key: payload.get(key) for key in ("edict_id", "adopted", "clauses", "changed_clauses", "execution_rate") if key in payload}
            if payload.get("action") == "draft":
                current["drafts"].append(record)
            else:
                current["confirms"].append(record)
                current["client_ops"].extend(
                    {"op": row.get("op"), "status": row.get("status"), "summary": row.get("summary", ""), **({"code": row["code"]} if row.get("code") else {})}
                    for row in payload.get("clauses") or []
                    if row.get("source") in {"client", "text"} and row.get("op")
                )
        elif kind == "message":
            metadata = payload.get("metadata") or {}
            if payload.get("role") == "player" and metadata.get("character"):
                current["audiences"].append({"character": metadata.get("character"), "mode": metadata.get("mode", "")})
            elif payload.get("role") == "debate":
                try:
                    topic = json.loads(payload.get("content") or "{}").get("topic", metadata.get("topic", ""))
                except ValueError:
                    topic = metadata.get("topic", "")
                current["debates"].append({"topic": topic})
        elif kind == "advice":
            current["advice"].append({"character": payload.get("character"), "text": payload.get("text"), "source": payload.get("source")})
        elif kind == "turn":
            report = payload.get("report") or {}
            public = public_report(report, after, seed, crisis) or {}
            outcome = public.get("outcome")
            current["end_turn"] = {
                "resolved_to": public.get("turn"),
                "calendar_label": after.calendar.label,
                "facts": [fact_line(fact) for fact in public.get("facts", []) if fact.get("kind") != "outcome"],
                "events": [{"id": row.get("id"), "title": row.get("title")} for row in public.get("events", []) if not row.get("outcome")],
                "battles": [(row.get("text", "") + battle_breakdown(row)) for row in public.get("battles", [])],
                "choices_opened": [
                    {"id": row.get("id"), "title": row.get("title"), "options": [option.get("id") for option in row.get("options", [])]}
                    for row in public.get("choices_opened", []) or []
                ],
                "reconciliation": public.get("reconciliation", []),
                "crisis": public.get("crisis") or {},
                "crisis_change": public.get("crisis_change"),
                "warnings": public.get("warnings", []),
                "outcome": outcome,
                "resources": [resource_display(after, key, seed) for key in after.resources],
            }
            current = _season(seasons, after.turn, after.calendar.label)
        state = after
    # 拟诏回执在旁路文件（拟诏不改日志）：只收拟于当前分支路径上的草案
    path_ids = {entry["id"] for entry in session.history()}
    for record in session.draft_records():
        if record.get("base_head") is not None and record.get("base_head") not in path_ids:
            continue
        turn = int(record.get("turn", 0) or 0)
        if turn in seasons:
            seasons[turn]["drafts"].append({key: record.get(key) for key in ("edict_id", "adopted", "clauses") if key in record})
    # client 模式的召对 / 廷议（调用方模型自写台词）只有轻量记录：谁、方式、议题、回合，不含台词
    for record in session.call_records():
        if record.get("base_head") is not None and record.get("base_head") not in path_ids:
            continue
        turn = int(record.get("turn", 0) or 0)
        if turn not in seasons:
            continue
        if record.get("kind") == "audience":
            seasons[turn]["audiences"].append({
                key: record.get(key) for key in ("character", "name", "mode", "mode_label", "via") if record.get(key) is not None
            })
        elif record.get("kind") == "debate":
            seasons[turn]["debates"].append({
                key: record.get(key) for key in ("topic", "memorial_id", "option_id", "participants", "via") if record.get(key) not in (None, "")
            })
    rows = [
        row
        for turn, row in sorted(seasons.items())
        if (from_turn is None or turn >= from_turn) and (to_turn is None or turn <= to_turn)
        and (row["end_turn"] is not None or any(row[key] for key in ("adopted", "drafts", "confirms", "choices", "delayed", "audiences", "debates", "advice")))
    ]
    final = session.head_state()
    return {
        "session_id": session_id,
        "scenario": final.scenario_id,
        "scenario_version": session.header.get("scenario_version"),
        "seed": seed,
        "difficulty": session.header.get("difficulty"),
        "turn": final.turn,
        "calendar_label": final.calendar.label,
        "from_turn": from_turn,
        "to_turn": to_turn,
        "outcome": final.outcome.model_dump(mode="json") if final.outcome else None,
        "note": "按下诏所在季记事：seasons[i].end_turn 是该季结算（进入 resolved_to 回合）的结果；隐藏数值只给风闻区间。",
        "seasons": rows,
    }

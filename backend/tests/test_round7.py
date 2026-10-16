"""第 7 轮措辞修复（gpt-06 / gpt-07 报告）：草案回执与确认回执分用「可行（确认后本季即行）」/「已行」，延期条款统一说成
「延至下季结算时执行（第 n 回合）」；简报「待办与在途」；玩家文本不出现回合代号 T\d+ 与六位以上的裸数。"""

import asyncio
import json
import re

from simcore.domain.rules import delay_phrase, turn_label
from simcore.mcp.server import _clause_lines, _turn_text
from simcore.service import GameService

BARE_BIG_NUMBER = re.compile(r"(?<![\d.,])\d{6,}(?![\d.,])")
TURN_CODE = re.compile(r"(?<![A-Za-z0-9])T\d+")


def _turn(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.end_turn(session_id, narrate=False, **kwargs))


def test_draft_and_confirmed_receipts_use_different_timing_words() -> None:
    clauses = [
        {"status": "applied", "source": "client", "summary": "禁军 移驻 雄关"},
        {"status": "delayed", "source": "memorial", "summary": "国库 −50", "due_turn": 3, "delay_reason": "有司稽迟"},
    ]
    draft = _clause_lines(clauses, confirmed=False)
    assert draft[0].startswith("1. [applied 可行（确认后本季即行）]")
    assert draft[1].startswith("2. [delayed 确认后延至下季结算时执行（第 3 回合）]") and "延期原因：有司稽迟" in draft[1]
    confirmed = _clause_lines(clauses, confirmed=True)
    assert confirmed[0].startswith("1. [applied 已行]") and "本季即行" not in confirmed[0]
    assert confirmed[1].startswith("2. [delayed 已延至下季结算时执行（第 3 回合）]")
    for line in draft + confirmed:  # 同一件事不混用「下季」与「本季结算」
        assert "本季 end_turn 结算" not in line and "本季结算" not in line


def test_delay_wording_is_shared_by_briefing_and_export(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    session = service.session(session_id)
    session.append_pending([{"op": "adjust_resource", "resource": "treasury", "delta": -10}], 1, label="请减赋")
    phrase = delay_phrase(1)
    assert phrase == "延至下季结算时执行（第 1 回合）"
    briefing = service.briefing(session_id)
    assert "## 待办与在途" in briefing and "延期条款「请减赋」" in briefing and phrase in briefing
    assert "待执行的延期条款" not in briefing
    delayed = service.export_log(session_id)["seasons"][0]["delayed"]
    assert delayed[0]["timing"] == phrase
    assert any(row["label"].endswith(phrase) for row in service.history(session_id) if row["type"] == "pending")


def test_briefing_lists_projects_travellers_and_choice_deadlines(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    session = service.session(session_id)
    results = session.append_ops([
        {"op": "move_army", "army": "guard", "to_region": "plain"},
        {"op": "start_project", "name": "修筑外郭", "region": "capital", "turns": 3, "cost_per_turn": {"treasury": 1}},
    ])
    assert all(row["ok"] for row in results), results
    state = session.head_state()
    briefing = service.briefing(session_id)
    section = briefing.split("## 待办与在途", 1)[1].split("\n## ", 1)[0]
    assert "工程「修筑外郭」：进度 0/3，预计" in section and turn_label(state, state.turn + 3) in section
    eta = state.armies["guard"].in_transit["eta_turn"]
    assert f"预计{turn_label(state, eta)}抵达" in section
    assert "在途：" in section


def test_turn_label_renders_calendar_not_turn_codes(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    state = service.session(session_id).head_state()
    assert turn_label(state, 0) == f"{state.calendar.label}（第0回合）"
    assert re.fullmatch(r".+（第8回合）", turn_label(state, 8)) and turn_label(state, 8, short=True) == turn_label(state, 8)[: -len("（第8回合）")]


def test_full_game_player_texts_have_no_turn_codes_or_bare_big_numbers(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    session = service.session(session_id)
    session.append_ops([{"op": "adjust_army", "army": "guard", "delta": 547900}])  # 大兵额：应显示为「54.79万」之类
    texts: list[str] = []
    for _ in range(12):
        texts.append(service.briefing(session_id))
        if service.session(session_id).head_state().outcome is not None:
            break
        result = _turn(service, session_id, discard_drafts=True)
        texts.append(_turn_text(result))
        texts.append(json.dumps(result.get("final_notice") or {}, ensure_ascii=False))
        texts.append(json.dumps(result.get("outcome") or {}, ensure_ascii=False))
    assert service.session(session_id).head_state().outcome is not None
    texts.append(service.briefing(session_id))  # 含终局复盘
    texts.append(json.dumps(service.view(session_id)["review"], ensure_ascii=False))
    texts.append(json.dumps(service.export_log(session_id), ensure_ascii=False))
    joined = "\n".join(texts)
    assert "终局复盘" in joined and "万" in joined
    assert not TURN_CODE.findall(joined), TURN_CODE.findall(joined)
    assert not [m for m in BARE_BIG_NUMBER.findall(re.sub(r'"(edict_id|id|entry_id|state_hash|hash)":\s*"[^"]*"', "", joined)) if not m.startswith("0")], \
        BARE_BIG_NUMBER.findall(joined)

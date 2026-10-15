"""世界状态不变量检查。测试在每次回合结算后运行（设置 SIM_CHECK_INVARIANTS=1 时 resolve_turn 自动检查）。"""

from __future__ import annotations

from simcore.domain.models import WorldState
from simcore.domain.rules import held_offices, max_concurrent_offices


def check_invariants(state: WorldState, previous: WorldState | None = None) -> list[str]:
    problems: list[str] = []
    limit = max_concurrent_offices(state)
    for character in state.characters.values():
        held = held_offices(character)
        if character.status != "alive" and held:
            problems.append(f"非在世人物仍任官职：{character.id} {held}")
        if character.office is None and character.offices:
            problems.append(f"人物有兼职却无主职：{character.id}")
        if character.office is not None and character.office not in character.offices:
            problems.append(f"主职不在官职列表中：{character.id} {character.office}")
        if len(set(held)) != len(held):
            problems.append(f"官职列表重复：{character.id}")
        if len(held) > limit:
            problems.append(f"兼职超过上限 {limit}：{character.id}")
        for office_id in held:
            office = state.offices.get(office_id)
            if office is None:
                problems.append(f"人物担任不存在的官职：{character.id} {office_id}")
            elif office.holder != character.id:
                problems.append(f"官职记录不一致：{office_id} 的在任者为 {office.holder}，但 {character.id} 记为在任")
    for office in state.offices.values():
        if office.holder is None:
            continue
        holder = state.characters.get(office.holder)
        if holder is None:
            problems.append(f"官职在任者不存在：{office.id} {office.holder}")
        elif office.id not in held_offices(holder):
            problems.append(f"官职记录不一致：{office.id} 在任者 {holder.id} 未记此职")
    for resource in state.resources.values():
        if resource.minimum is not None and resource.amount < resource.minimum - 1e-9:
            problems.append(f"资源低于下限：{resource.id} {resource.amount}")
        if resource.maximum is not None and resource.amount > resource.maximum + 1e-9:
            problems.append(f"资源高于上限：{resource.id} {resource.amount}")
    for army in state.armies.values():
        if army.strength < 0:
            problems.append(f"军队兵力为负：{army.id}")
        if army.commander is not None:
            commander = state.characters.get(army.commander)
            if commander is None:
                problems.append(f"军队统帅不存在：{army.id} {army.commander}")
            elif commander.status != "alive":
                problems.append(f"非在世人物仍统兵：{army.id} {army.commander}")
        if army.region not in state.regions:
            problems.append(f"军队所在地区不存在：{army.id} {army.region}")
    seen: set[str] = set()
    for choice in state.pending_choices:
        if choice.id in seen:
            problems.append(f"待决事件重复：{choice.id}")
        seen.add(choice.id)
        if state.flags.get(f"choice:{choice.id}") is not None:
            problems.append(f"已处置的事件仍在待决：{choice.id}")
        if not any(option.id == choice.default_option for option in choice.options):
            problems.append(f"待决事件默认项不存在：{choice.id}")
    if state.outcome is not None and state.outcome.turn > state.turn:
        problems.append("终局回合晚于当前回合")
    if previous is not None and previous.outcome is not None and state.outcome != previous.outcome:
        problems.append("终局状态被改变")
    return problems

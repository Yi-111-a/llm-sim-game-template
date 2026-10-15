from __future__ import annotations

from typing import Any


def format_calendar(turn: int, season: str, config: dict[str, Any]) -> str:
    eras = sorted(config.get("eras", []), key=lambda item: int(item.get("from_turn", 0)))
    effective_turn = turn + int(config.get("pre_game_turns", 0))
    active = next(
        (era for era in reversed(eras) if int(era.get("from_turn", 0)) <= effective_turn),
        None,
    )
    if active is None:
        return f"第{effective_turn}回合{season}"
    seasons = config.get("seasons", ["春", "夏", "秋", "冬"])
    cycles = len(seasons) if len(seasons) > 0 else 4
    elapsed = max(0, effective_turn - int(active.get("from_turn", 0)))
    year = int(active.get("start_year", 1)) + elapsed // cycles
    numeral = _chinese_number(year) if config.get("number_system", "chinese") == "chinese" else str(year)
    return str(config.get("turn_format", "{era}{year}年{season}")).format(
        era=active.get("name", ""),
        year=numeral,
        season=season,
    )


def _chinese_number(value: int) -> str:
    if value == 1:
        return "元"
    if value <= 0:
        return str(value)
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value >= 10_000:
        return str(value)
    units = ("", "十", "百", "千")
    output = ""
    zero_pending = False
    for position in range(3, -1, -1):
        divisor = 10**position
        digit, value = divmod(value, divisor)
        if not digit:
            zero_pending = bool(output and value)
            continue
        if zero_pending:
            output += "零"
        if not (position == 1 and digit == 1 and not output):
            output += digits[digit]
        output += units[position]
        zero_pending = False
    return output

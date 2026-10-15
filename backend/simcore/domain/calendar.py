from __future__ import annotations

from typing import Any


def format_calendar(
    turn: int,
    season: str,
    config: dict[str, Any],
    start_season_index: int | None = None,
    fallback_year: int | None = None,
) -> str:
    """按年号格式化回合标签。

    - era 的 from_turn 以「有效回合」（turn + pre_game_turns）计。
    - 每个 era 可带自己的 turn_format / number_system，用于「载」「年」切换。
    - 传入 start_season_index（T0 所在季序号）时，年份按真实跨年计算，年号可在年中改元；
      否则沿用旧算法（自 from_turn 起每满一轮季节加一年）。
    - 当前回合尚无生效年号时，若给出 fallback_year 则显示「{fallback_year}年{season}」。
    """
    eras = sorted(config.get("eras", []), key=lambda item: int(item.get("from_turn", 0)))
    pre_game = int(config.get("pre_game_turns", 0))
    effective_turn = turn + pre_game
    active = next(
        (era for era in reversed(eras) if int(era.get("from_turn", 0)) <= effective_turn),
        None,
    )
    if active is None:
        if fallback_year is not None:
            return f"{fallback_year}年{season}"
        return f"第{effective_turn}回合{season}"
    seasons = config.get("seasons", ["春", "夏", "秋", "冬"])
    cycles = len(seasons) if len(seasons) > 0 else 4
    era_start_turn = int(active.get("from_turn", 0)) - pre_game
    if start_season_index is not None:
        years_now = (start_season_index + turn) // cycles
        years_at_start = (start_season_index + era_start_turn) // cycles
        year = int(active.get("start_year", 1)) + years_now - years_at_start
    else:
        elapsed = max(0, effective_turn - int(active.get("from_turn", 0)))
        year = int(active.get("start_year", 1)) + elapsed // cycles
    number_system = active.get("number_system") or config.get("number_system", "chinese")
    numeral = _chinese_number(year) if number_system == "chinese" else str(year)
    template = active.get("turn_format") or config.get("turn_format", "{era}{year}年{season}")
    return str(template).format(
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


def start_season_index(scenario_definition: dict[str, Any]) -> int | None:
    """T0 在季节表中的序号（仅 turn_unit 为 season 时有意义）。"""
    if scenario_definition.get("turn_unit") != "season":
        return None
    start = str(scenario_definition.get("start_date", ""))
    try:
        month = int(start.split("-")[1])
    except (IndexError, ValueError):
        return None
    season = ("春", "夏", "秋", "冬")[(month - 1) // 3]
    seasons = (scenario_definition.get("calendar") or {}).get("seasons") or ["春", "夏", "秋", "冬"]
    return seasons.index(season) if season in seasons else (month - 1) * len(seasons) // 12

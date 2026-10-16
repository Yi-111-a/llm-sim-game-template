from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class OpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdjustResource(OpModel):
    op: Literal["adjust_resource"]
    resource: str
    delta: float
    reason: str = ""


class AddLedger(OpModel):
    op: Literal["add_ledger"]
    name: str
    category: str
    amount: float
    kind: Literal["one_off", "recurring"]
    turns: int | None = None
    reason: str = ""
    cancellable: bool = True  # False：玩家不能以 cancel_ledger 撤销（剧本 / 事件设的固定开支）


class CancelLedger(OpModel):
    op: Literal["cancel_ledger"]
    ledger_id: str


class Appoint(OpModel):
    op: Literal["appoint"]
    character: str
    office: str
    # 有任所的官职（Office.army / seat）：True 令其启程赴任；False 为遥领。玩家层提议缺省为 True，剧本 / 事件缺省 False。
    dispatch: bool = False


class Dismiss(OpModel):
    op: Literal["dismiss"]
    character: str
    office: str


class SetCharacterStatus(OpModel):
    op: Literal["set_character_status"]
    character: str
    status: Literal["alive", "dead", "imprisoned", "exiled", "retired"]
    cause: str = ""


class SetCharacterFaction(OpModel):
    """人物改属某势力（剧本层：如兵变首领随叛军易帜）。"""

    op: Literal["set_character_faction"]
    character: str
    faction: str


class MoveCharacter(OpModel):
    op: Literal["move_character"]
    character: str
    to_region: str


class MoveArmy(OpModel):
    op: Literal["move_army"]
    army: str
    to_region: str
    with_commander: bool = True  # 统帅与军队同驻一地时随军同行（False：统帅留驻原地）


class March(OpModel):
    """率军出征：军队与其统帅作为同一行程出发（统帅须与军队同在一地）。"""

    op: Literal["march"]
    army: str
    to_region: str
    with_commander: bool = True


class Detach(OpModel):
    """分兵：从 army 中分出 strength 兵力，编为一支新军（同势力、同素质），自原驻地开拔赴 to_region（同地则就地分屯）。

    new_army_name 缺省为「原军名偏师」；commander 为新军统帅（与原军同驻者随新军同行，否则启程赴任）。
    """

    op: Literal["detach"]
    army: str
    strength: float
    to_region: str
    new_army_name: str | None = None
    commander: str | None = None


class StartProject(OpModel):
    op: Literal["start_project"]
    name: str
    region: str
    cost_per_turn: dict[str, float]
    turns: int
    on_complete: list[dict[str, Any]] = Field(default_factory=list)


class AdjustRegion(OpModel):
    op: Literal["adjust_region"]
    region: str
    stat: str
    delta: float


class AdjustRelation(OpModel):
    op: Literal["adjust_relation"]
    a: str
    b: str
    delta: float


class AddStandingOrder(OpModel):
    op: Literal["add_standing_order"]
    text: str
    ops: list[dict[str, Any]]
    until_turn: int | None = None


class CancelStandingOrder(OpModel):
    op: Literal["cancel_standing_order"]
    id: str


class SetFlag(OpModel):
    op: Literal["set_flag"]
    key: str
    value: Any


class Remember(OpModel):
    op: Literal["remember"]
    character: str
    fact: str


class AdjustArmy(OpModel):
    op: Literal["adjust_army"]
    army: str
    delta: float
    reason: str = ""


class SetArmyFaction(OpModel):
    op: Literal["set_army_faction"]
    army: str
    faction: str


class SetArmyCommander(OpModel):
    op: Literal["set_army_commander"]
    army: str
    character: str | None = None
    # True：新统帅不在军中时启程赴任（玩家层提议缺省为 True；剧本 / 事件缺省 False，即遥领，由副将暂摄）
    dispatch: bool = False


class SetArmyPosture(OpModel):
    """军令姿态：hold 坚守 / attack 进击 / scorch 坚壁清野（影响战斗与驻地，见 domain.military）。"""

    op: Literal["set_army_posture"]
    army: str
    posture: Literal["hold", "attack", "scorch"]
    reason: str = ""


class AdjustTrait(OpModel):
    op: Literal["adjust_trait"]
    character: str
    trait: str
    delta: float


class AdjustCounter(OpModel):
    op: Literal["adjust_counter"]
    key: str
    delta: int


class SetCounter(OpModel):
    op: Literal["set_counter"]
    key: str
    value: int


class SetRegionController(OpModel):
    op: Literal["set_region_controller"]
    region: str
    faction: str


class ChooseEventOption(OpModel):
    op: Literal["choose_event_option"]
    choice_id: str
    option_id: str


StateOp: TypeAlias = Annotated[
    AdjustResource
    | AddLedger
    | CancelLedger
    | Appoint
    | Dismiss
    | SetCharacterStatus
    | SetCharacterFaction
    | MoveCharacter
    | MoveArmy
    | March
    | Detach
    | StartProject
    | AdjustRegion
    | AdjustRelation
    | AddStandingOrder
    | CancelStandingOrder
    | SetFlag
    | Remember
    | AdjustArmy
    | SetArmyFaction
    | SetArmyCommander
    | SetArmyPosture
    | AdjustTrait
    | AdjustCounter
    | SetCounter
    | SetRegionController
    | ChooseEventOption,
    Field(discriminator="op"),
]

OP_NAMES: tuple[str, ...] = (
    "adjust_resource",
    "add_ledger",
    "cancel_ledger",
    "appoint",
    "dismiss",
    "set_character_status",
    "set_character_faction",
    "move_character",
    "move_army",
    "march",
    "detach",
    "start_project",
    "adjust_region",
    "adjust_relation",
    "add_standing_order",
    "cancel_standing_order",
    "set_flag",
    "remember",
    "adjust_army",
    "set_army_faction",
    "set_army_commander",
    "set_army_posture",
    "adjust_trait",
    "adjust_counter",
    "set_counter",
    "set_region_controller",
    "choose_event_option",
)

STATE_OP_ADAPTER: TypeAdapter[Any] = TypeAdapter(StateOp)
OP_MODELS: dict[str, type[OpModel]] = {
    str(model.model_fields["op"].annotation.__args__[0]): model  # type: ignore[union-attr]
    for model in OpModel.__subclasses__()
}


def op_signature(name: str) -> str:
    """一行字段说明：「character: str, office: str, reason?: str」（? 为可省略）。"""
    model = OP_MODELS.get(name)
    if model is None:
        return ""
    parts = []
    for field_name, field in model.model_fields.items():
        if field_name == "op":
            continue
        annotation = field.annotation
        args = getattr(annotation, "__args__", ())
        if getattr(annotation, "__origin__", None) is Literal:
            text = "|".join(str(item) for item in args)
        else:
            text = getattr(annotation, "__name__", None) if not args else None
            text = text or str(annotation).replace("typing.", "")
        parts.append(f"{field_name}{'' if field.is_required() else '?'}: {text}")
    return ", ".join(parts)


def op_model_schema(name: str) -> dict[str, Any]:
    """单个操作的 JSON Schema。"""
    model = OP_MODELS.get(name)
    return model.model_json_schema() if model is not None else {}


def parse_op(value: StateOp | dict[str, Any]) -> StateOp:
    if isinstance(value, BaseModel):
        return value
    return STATE_OP_ADAPTER.validate_python(value)


def op_json_schema() -> dict[str, Any]:
    """全部 StateOp 的 JSON Schema（判别字段为 op）。"""
    return STATE_OP_ADAPTER.json_schema()


def describe_op_error(exc: Exception, raw: Any) -> str:
    """把 Pydantic 校验错误写成简短中文说明（供回执与 MCP 返回）。"""
    errors = exc.errors() if hasattr(exc, "errors") else []
    name = raw.get("op") if isinstance(raw, dict) else None
    if not isinstance(raw, dict):
        return "操作必须是 JSON 对象"
    parts: list[str] = []
    for error in errors:
        kind = error.get("type", "")
        if kind == "union_tag_not_found":
            return "缺少 op 字段"
        if kind == "union_tag_invalid":
            return f"未知操作：{name}（可用操作见 op_reference）"
        location = ".".join(str(item) for item in error.get("loc", ())[1:]) or "?"
        if kind == "missing":
            parts.append(f"缺少字段 {location}")
        elif kind == "extra_forbidden":
            parts.append(f"不支持的字段 {location}")
        else:
            parts.append(f"字段 {location} 无效（{error.get('msg', '')}）")
    return f"{name or '操作'} 格式无效：" + "；".join(parts[:5]) if parts else f"{name or '操作'} 格式无效"


def memory_ops(raw: Any) -> list[dict[str, Any]]:
    """奏疏选项 / 事件选项上的 memory 字段（{character, text} 或其列表）转成 Remember op。

    格式不对的条目忽略（validate_scenario 会报告）。
    """
    rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        character, text = row.get("character"), str(row.get("text") or row.get("fact") or "").strip()
        if character and text:
            result.append({"op": "remember", "character": str(character), "fact": text})
    return result

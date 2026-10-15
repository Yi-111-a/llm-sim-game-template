from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import typer
import uvicorn
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static

from simcore.config import load_settings
from simcore.images.base import MockImageProvider
from simcore.images.manifest import (
    asset_path,
    compose_prompt,
    read_manifest,
    update_asset_entry,
    write_manifest,
)
from simcore.images.openai_images import OpenAIImageProvider
from simcore.llm.base import ToolCallingUnsupported
from simcore.llm.factory import create_provider
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.store.session import SessionStore

app = typer.Typer(no_args_is_help=True, help="本地优先模拟游戏工具")
KIND_TEMPLATES = {
    "portrait": "竖幅人物设定图，头肩构图，历史题材绘制，人物身份一目了然；避免文字、水印和现代物件。",
    "region_card": "横幅地区环境卡图，广角构图，突出地貌与聚落；无文字、无边框、无现代物件。",
    "event": "横幅历史事件插图，叙事性场景，适合作为编年纪事配图；不呈现可读文字或水印。",
    "map": "俯视历史地图插画，结构清晰、地名留白，不生成文字或图例。",
    "ui_icon": "简洁一致的历史题材界面图标，中心单一物件，背景透明或纯色，无文字。",
    "ui_background": "历史题材游戏界面装饰背景，低对比、可读性优先，不含文字或按钮。",
}


def _root() -> Path:
    return Path(os.environ.get("SIM_GAME_ROOT", Path(__file__).resolve().parents[2])).resolve()


def _scenario_path(target: str) -> Path:
    candidate = Path(target)
    if candidate.is_dir():
        return candidate.resolve()
    return (_root() / "scenarios" / target).resolve()


def _session_path(session_id: str) -> Path:
    return Path(os.environ.get("LLM_SIM_HOME", Path.home() / ".llm-sim")) / "sessions" / f"{session_id}.jsonl"


def _load_session(target: str) -> SessionStore:
    path = Path(target)
    if not path.is_file():
        path = _session_path(target)
    if not path.is_file():
        raise typer.BadParameter(f"找不到存档：{target}")
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    scenario = load_scenario(_root() / "scenarios" / header["scenario"])
    return SessionStore(path, scenario)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, min=1, max=65535, help="监听端口"),
    reload: bool = typer.Option(False, help="开发模式热重载"),
) -> None:
    """启动本地 API 服务。"""
    uvicorn.run("api.app:app", host=host, port=port, reload=reload)


@app.command("validate")
def validate(target: str) -> None:
    """检查场景结构、引用与资产清单。"""
    path = _scenario_path(target)
    errors = validate_scenario(path)
    if errors:
        for error in errors:
            typer.echo(f"错误：{error}", err=True)
        raise typer.Exit(code=1)
    data = load_scenario(path)
    typer.echo(f"场景有效：{data['scenario']['id']}")
    typer.echo(
        "实体数量："
        f"{len(data.get('regions', []))} 地区，"
        f"{len(data.get('characters', []))} 人物，"
        f"{len(data.get('events', []))} 事件，"
        f"{len(data.get('assets', {}).get('entries', []))} 项资产"
    )


@app.command("new-game")
def new_game(scenario_id: str, seed: int = typer.Option(0, help="随机种子")) -> None:
    """从场景创建新存档。"""
    scenario = load_scenario(_scenario_path(scenario_id))
    errors = validate_scenario(scenario)
    if errors:
        raise typer.BadParameter("; ".join(errors))
    session_id = os.urandom(16).hex()
    path = _session_path(session_id)
    session = SessionStore(path, scenario, build_initial_state(scenario), seed)
    typer.echo(json.dumps({"id": session_id, "scenario": scenario_id, "state": session.state.model_dump(mode="json")}, ensure_ascii=False))


@app.command("gen-assets")
def gen_assets(
    scenario_id: str,
    only: str | None = typer.Option(None, "--only", help="只处理指定状态，如 missing"),
    seed: int | None = typer.Option(None, help="可复现的图像种子"),
) -> None:
    """按资产清单生成图像并写回清单。"""
    scenario_root = _scenario_path(scenario_id)
    manifest = read_manifest(scenario_root)
    settings = load_settings()
    if settings.get("image_provider", "mock") == "mock":
        provider = MockImageProvider()
        model = "mock"
    else:
        provider = OpenAIImageProvider(
            str(settings.get("image_base_url", "https://api.openai.com/v1")),
            str(settings.get("image_api_key", "")),
            str(settings.get("image_model", "gpt-image-1")),
        )
        model = str(settings.get("image_model", "gpt-image-1"))
    style_path = scenario_root / "style.md"
    style = style_path.read_text(encoding="utf-8") if style_path.exists() else ""
    generated = 0
    for entry in manifest.get("entries", []):
        if only and entry.get("status") != only:
            continue
        destination = asset_path(scenario_root, entry["path"])
        if not only and destination.exists():
            continue
        kind = str(entry.get("kind", ""))
        prompt = compose_prompt(style, KIND_TEMPLATES.get(kind, ""), str(entry.get("prompt", "")))
        size = tuple(entry.get("size", [1024, 1024]))
        payload = asyncio.run(provider.generate(prompt, (int(size[0]), int(size[1])), seed))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        update_asset_entry(entry, payload, model, seed)
        generated += 1
        typer.echo(f"生成 {entry['id']} -> {entry['path']}")
    write_manifest(scenario_root, manifest)
    typer.echo(f"完成：{generated} 项")


@app.command()
def replay(target: str) -> None:
    """重放存档事件并输出状态哈希。"""
    session = _load_session(target)
    state, digest = session.replay()
    typer.echo(json.dumps({"turn": state.turn, "state_hash": digest}, ensure_ascii=False))


@app.command()
def doctor() -> None:
    """探测当前模型是否支持工具调用。"""

    async def probe() -> dict[str, Any]:
        provider = create_provider(load_settings(), "edict_interpreter")
        tools = [{
            "type": "function",
            "function": {
                "name": "finish_turn",
                "description": "Return immediately.",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        try:
            result = await provider.chat(
                [
                    {"role": "system", "content": "调用工具完成这次简短的诏令能力探测。"},
                    {"role": "user", "content": "请立即调用 finish_turn。"},
                ],
                tools=tools,
            )
        except ToolCallingUnsupported:
            return {"provider_reachable": True, "tool_calling": False, "json_fallback": True}
        return {
            "provider_reachable": True,
            "tool_calling": bool(result.tool_calls),
            "json_fallback": not bool(result.tool_calls),
            "model": result.model,
        }

    try:
        result = asyncio.run(probe())
    except Exception as exc:
        raise typer.BadParameter("模型连接或能力探测失败") from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


class SessionApp(App[None]):
    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__()
        self.state_data = state

    def compose(self) -> ComposeResult:
        yield Header()
        state = self.state_data
        resources = state.get("resources", {})
        lines = [
            f"场景：{state.get('scenario_id')}　回合：{state.get('turn')}",
            f"日期：{state.get('calendar', {}).get('label', '')}",
            "",
            "国势资源",
            *[f"  {row.get('name', key)}：{row.get('amount')}" for key, row in resources.items()],
            "",
            f"地区 {len(state.get('regions', {}))} · 人物 {len(state.get('characters', {}))}",
            "完整交互界面请运行前端开发服务器。",
        ]
        yield Static("\n".join(lines), id="session")
        yield Footer()


@app.command()
def play(target: str = typer.Argument("_template")) -> None:
    """在终端查看存档状态。"""
    candidate = Path(target)
    session_file = candidate if candidate.is_file() else _session_path(target)
    if session_file.is_file():
        session = _load_session(str(session_file))
    else:
        scenario = load_scenario(_scenario_path(target))
        session = SessionStore(
            _session_path(os.urandom(16).hex()),
            scenario,
            build_initial_state(scenario),
        )
    SessionApp(session.state.model_dump(mode="json")).run()


if __name__ == "__main__":
    app()

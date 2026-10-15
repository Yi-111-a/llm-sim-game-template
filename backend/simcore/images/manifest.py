from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from simcore.images.placeholder import placeholder_svg


def manifest_path(scenario_root: Path) -> Path:
    return scenario_root / "assets.yaml"


def read_manifest(scenario_root: Path) -> dict[str, Any]:
    path = manifest_path(scenario_root)
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}


def write_manifest(scenario_root: Path, manifest: dict[str, Any]) -> None:
    manifest_path(scenario_root).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def asset_path(scenario_root: Path, relative: str) -> Path:
    root = scenario_root.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Asset path escapes the scenario directory")
    return target


def compose_prompt(style: str, kind_template: str, entry_prompt: str) -> str:
    return "\n\n".join(part.strip() for part in (style, kind_template, entry_prompt) if part.strip())


def update_asset_entry(
    entry: dict[str, Any], data: bytes, provider: str, seed: int | None
) -> dict[str, Any]:
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    entry["status"] = "generated"
    entry["model"] = provider
    if seed is not None:
        entry["seed"] = seed
    return entry


def placeholder_for(scenario_root: Path, asset_id: str, size: tuple[int, int] | None = None) -> bytes:
    manifest = read_manifest(scenario_root)
    entry = next((row for row in manifest.get("entries", []) if row.get("id") == asset_id), None)
    if entry is None:
        return placeholder_svg(asset_id, "未登记资产").encode("utf-8")
    label = entry.get("used_by", asset_id).split(":")[-1]
    dimensions = size or tuple(entry.get("size", [640, 400]))
    return placeholder_svg(label, entry.get("kind", "asset"), asset_id, dimensions).encode("utf-8")

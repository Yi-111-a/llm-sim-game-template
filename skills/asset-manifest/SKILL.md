---
name: asset-manifest
description: 维护场景资产 id、用途、格式、路径与尺寸的可验证契约，包括可配置的必需槽位（asset_requirements）、事件与结局插图。
---

# 资产清单

## 何时使用

新增、移动、重命名或审核图片资源；修复界面缺图与占位图映射；为剧本改用自己的资产命名方案时使用。

## 输入

UI 与实体需要的 manifest id、图片类别、像素尺寸、格式和相对路径。

## 步骤

1. 用稳定的点分隔 id 表示实体或 UI 槽位，`used_by` 说明实际用途。
2. `path` 限定在场景目录内，格式 webp 或 png，单边 1–4096 像素。
3. 标注 `status`（missing / placeholder / generated / approved）、`prompt`、`negative_prompt`、`version`；未生成时 `missing`，不伪造 `sha256` 或模型名。
4. **必需槽位**：缺省为 `character.{id}.portrait`、`region.{id}.card` 与 `ui.topbar.background`、`ui.edict.parchment`、`ui.court.backdrop`、`ui.map.base`、`ui.memorial.banner`。剧本可在 `scenario.yaml` 改写：

   ```yaml
   asset_requirements:
     character: "portrait.{id}.default"   # null 关闭
     region: null
     ui: [map.base, ui.desk, ui.paper]
   ```

5. `scheduled` / `conditional` / `random` 事件需要 `event.<id>.illustration`（或事件 `illustration` 指定的 id）；结局、奏疏、政令卡写了 `illustration` 就必须存在。
6. 前端只用 manifest id 请求（`/api/assets/{scenario}/{id}`），缺文件时得到按尺寸生成的 SVG 占位图。

## 输出契约

`assets.yaml` 无重复 id、无路径穿越、无超尺寸；必需槽位齐全；缺图可回退到占位图。

## 涉及文件

`scenarios/<id>/assets.yaml`、`scenario.yaml`（asset_requirements）、`assets/`、`backend/simcore/images/manifest.py`、UI 组件。

## 验证

`sim validate <scenario>`；请求一个已生成与一个 missing 的资产端点；改名后确认 validate 报出缺失槽位。

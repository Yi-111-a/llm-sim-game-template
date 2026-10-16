---
name: image-asset-generation
description: 为角色、地区、事件、结局与 UI 槽位编写图像提示词、生成文件并更新 manifest；图片一律不含文字，风格由 style.md 统一。
---

# 图片资产生成

## 何时使用

创建新图片槽位、批量生成场景图片或检查图片应保存到哪个目录时使用。

## 输入

场景 `style.md`、目标 manifest ID、要表达的主体、风格、尺寸与图像提供方。

## 提示词模板与保存路径

统一将场景风格、种类提示模板和 manifest `prompt` 合成；负面提示放入 `negative_prompt`，避免文字、水印和不需要的时代物件。

| kind / manifest ID | 尺寸 | 保存路径 | 提示词模板 |
| --- | ---: | --- | --- |
| `portrait` / `character.<id>.portrait` | 768×1024 | `assets/characters/<id>/portrait.webp` | `{人物身份与衣着}，头肩肖像，背景简洁，表情与身份清楚，无文字。` |
| `bust` / `character.<id>.bust` | 512×512 | `assets/characters/<id>/bust.webp` | `{人物身份}，正面半身构图，统一光线与画风，无文字或水印。` |
| `chat_avatar` / `character.<id>.chat_avatar` | 128×128 | `assets/characters/<id>/chat.webp` | `{人物面部特征}，清晰头像，简洁背景，裁切后仍可辨认。` |
| `region_card` / `region.<id>.card` | 1024×576 | `assets/regions/<id>.webp` | `{地形、聚落与气候}，横幅广角环境图，无标签。` |
| `event` / `event.<id>.illustration` | 1344×768 | `assets/events/<id>.webp` | `{事件现场与可见行动}，纪事插画，克制构图，不渲染文字。` |
| `ui_background` / `ui.topbar.background`、`ui.edict.parchment` | 顶栏 1920×320、诏令 1024×768 | `assets/ui/` | `{面板用途}，低对比、留白充足，不能干扰正文，无文字按钮。` |
| `ui_background` / `ui.court.backdrop`、`ui.memorial.banner` | 朝堂 1600×900、纪事 1344×480 | `assets/ui/` | `{朝堂或纪事氛围}，适合作为界面背景，主体弱化并留出内容区。` |
| `map` / `ui.map.*` | 2048×1536 | `assets/ui/map.webp` | `{区域地貌与山河}，俯视底图，不写地名、不画图例。` |
| `ui_icon` / `icon.resource.*`、`icon.office.*` | 128×128 PNG | `assets/icons/{resources,offices}/<id>.png` | `{单一象征物}，透明背景，轮廓清楚，无文字。` |

## 步骤

1. 先确定 manifest ID、`used_by`、唯一相对路径、`kind`、尺寸与格式；不要让 UI 猜文件名。剧本若用自己的命名方案（`asset_requirements`，如 `portrait.{id}.default`），按它登记。
2. 编写具体、可视化的正面提示词；背景、构图和文字限制也写清楚。
3. 更新 `assets.yaml` 的 `entries`，初始 `status: missing`，不伪造 `sha256` 或模型名。
4. 运行 `sim validate <scenario>`；使用 Mock 检查离线流程。
5. 可通过 `sim gen-assets <scenario> --only missing` 生成图像。命令按 manifest 路径落盘，并更新 `sha256`、`model`、`seed`、`status`。
6. 从前端引用 manifest ID，并通过 `/api/assets/{scenario}/{asset_id}` 查看生成文件或 SVG 占位。

## 经验（《天宝》美术两轮、约 120 张）

- **图片里不要有任何文字**（含伪文字、印章字、匾额字）：界面文字一律由 DOM 渲染，字体覆盖可控；负面提示词写明「无文字、无水印、无落款」。
- 先写 `style.md` 定下统一的光线、纸本肌理与色调，再批量生成；同一批次固定种子便于重做单张。
- 事件插图画「可见的行动与场景」，不画抽象概念；结局插图可多结局共用一张（同题不同段的结局）。
- 时代细节（服饰、甲胄、建筑）在 prompt 中写具体，避免现代物件；人物肖像与人设的年龄、身份一致。
- 生成后人工审看再把 `status` 改为 `approved`；未审的保持 `generated`。

## 输出契约

一份 ID 唯一、路径安全、尺寸合规的 manifest 条目；生成的图片与登记路径和格式一致。

## 涉及文件

`scenarios/<id>/assets.yaml`、`style.md`、`assets/`、必要的 UI 组件；生成图片不是必需的提交文件。

## 验证

执行 `sim validate`；确认文件尺寸不超过 4096×4096、占位端点可返回 SVG、生成状态有哈希。

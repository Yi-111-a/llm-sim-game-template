---
name: asset-manifest
description: 维护场景资产 ID、用途、格式、路径和尺寸之间的可验证契约。
---

# 资产清单

## 何时使用

新增、移动、重命名或审核图片资源；或修复界面缺图与占位图映射时使用。

## 输入

UI/实体需要的 manifest ID、图片类别、像素尺寸、输出格式和相对路径。

## 步骤

1. 用稳定的点分隔 ID 说明实体或 UI 面板位置，`used_by` 解释实际用途。
2. 将 `path` 限定在场景目录中，格式使用 webp 或 png。
3. 标注生成状态、提示词、风格、版本；未生成时用 `missing`。
4. 保证人物肖像、地区卡和有插图的事件满足场景必需槽位。
5. 前端只用 manifest ID 请求资源，不硬编码磁盘路径。

## 输出契约

`assets.yaml` 中无重复 ID、路径穿越或超尺寸条目；缺图仍可回退至占位图。

## 涉及文件

`scenarios/<id>/assets.yaml`、`assets/`、`backend/simcore/images/manifest.py`、UI 组件。

## 验证

执行 `sim validate <scenario>`，再请求一个已生成与一个 missing 的资产端点。

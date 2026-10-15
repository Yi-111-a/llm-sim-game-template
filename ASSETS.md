# 图片资产与「图片放在哪里」

每个场景使用 `assets.yaml` 声明图片用途、路径、尺寸、格式、提示词与状态。界面与场景实体都通过 manifest ID 引用资源，不在组件内拼接文件名。

## 常用槽位

| ID 示例 | 用途 | 尺寸 | 路径约定 |
| --- | --- | ---: | --- |
| `character.<id>.portrait` | 朝臣头像与人物卡 | 768×1024 | `assets/characters/<id>/portrait.webp` |
| `character.<id>.bust` | 人物半身像 | 512×512 | `assets/characters/<id>/bust.webp` |
| `character.<id>.chat_avatar` | 对话头像 | 128×128 | `assets/characters/<id>/chat.webp` |
| `region.<id>.card` | 地区抽屉卡片 | 1024×576 | `assets/regions/<id>.webp` |
| `event.<id>.illustration` | 事件与纪事插图 | 1344×768 | `assets/events/<id>.webp` |
| `ui.topbar.*` | 顶栏纹样 | 1920×320 | `assets/ui/topbar.webp` |
| `ui.edict.*` | 诏令纸面背景 | 1024×768 | `assets/ui/edict.webp` |
| `ui.court.*` | 朝堂背景 | 1600×900 | `assets/ui/court.webp` |
| `ui.map.*` | 地图底图 | 2048×1536 | `assets/ui/map.webp` |
| `ui.memorial.*` | 纪事横幅 | 1344×480 | `assets/ui/memorial.webp` |
| `icon.resource.<id>`、`icon.office.<id>` | 资源与官职图标 | 128×128 PNG | `assets/icons/` |

`used_by` 描述实体引用或 UI 插槽；`path` 必须相对于场景目录，路径穿越会被拒绝。`format` 支持 `webp`、`png`。单边尺寸须为 1–4096 像素；不需要的槽位可省略。

## 生成与占位

```bash
cd backend
sim gen-assets chongzhen_1627 --only missing
```

命令以 `style.md`、图片种类模板和条目 `prompt` 合成提示词，生成文件后回写 `sha256`、模型、种子与状态。Mock 图像提供方用于离线流程；正式二进制图像是可选产物，不应作为模板运行条件。仓库不提交生成的二进制图片。缺失文件时，`GET /api/assets/{scenario}/{asset_id}` 按 manifest 尺寸即时生成 SVG 占位图。

资产操作步骤和逐类提示词范例见 [image-asset-generation 技能](skills/image-asset-generation/SKILL.md)。

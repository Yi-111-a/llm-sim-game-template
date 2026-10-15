# LLM 模拟游戏模板

一个本地优先、可离线运行的 LLM 模拟游戏全栈模板。规则、状态变更与存档由代码确定；LLM 负责解释自然语言、扮演角色和撰写纪事。服务可接入任意 OpenAI 兼容模型和图像模型，也可全程使用 Mock，无需密钥。

仓库包含两个可运行场景：

- `scenarios/_template`：可复制扩展的空白模板。
- `scenarios/chongzhen_1627`：独立创作的崇祯初政示例。

## 快速开始

需要 Python 3.11+、Node.js 22+ 和 npm。

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
sim validate ../scenarios/_template
sim validate ../scenarios/chongzhen_1627
sim serve
```

另开终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

浏览器打开 Vite 输出的本地地址即可。默认 LLM 与图像提供方均为 `mock`；创建存档、预览并确认诏令、推进回合、查看纪事和与人物对话都不需要 API 密钥。要调用外部模型，可在「设置」中选择兼容提供方，或在启动后端前设置 `.env.example` 中的环境变量。

## 主要能力

- 确定性状态操作与规则校验，LLM 不直接写入游戏状态。
- 事件溯源 JSONL 存档，支持回放、分叉与重现状态哈希。
- 诏令采用「草拟 → 预览 → 确认」流程；未确认的操作不会生效。
- 每个 LLM 角色可单独路由提供方与模型；调用参数不能覆盖消息、模型、工具或流式选项。
- 工具调用不可用时，诏令解释器会尝试 JSON 格式回退。
- 资产清单记录图片用途；未生成的图片由后端按请求返回 SVG 占位图，不提交二进制资产。
- 场景携带历法、可选主题 CSS 与提示词覆盖，能作为可版本化模组分发。

## 文档

- [上手指南](GETTING_STARTED.md)
- [架构与流程](ARCHITECTURE.md)
- [图片资产清单](ASSETS.md)
- [模型提供方与路由](PROVIDERS.md)
- [提示词约定](PROMPTS.md)
- [技能目录](skills/create-llm-sim-game/SKILL.md)

完整检查：

```bash
scripts/check.sh
```

MIT 许可，详见 [LICENSE](LICENSE)。

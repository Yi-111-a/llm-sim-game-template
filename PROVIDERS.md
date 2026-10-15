# 模型提供方

## 默认离线模式

新安装默认使用 `mock`，提供确定性诏令解析、模板化纪事和本地 SVG 图片占位。运行和测试无需网络、密钥或付费调用。

## 兼容提供方

`backend/simcore/llm/presets.yaml` 内置 OpenAI、DeepSeek、Qwen 兼容模式、GLM、Ollama 与 custom。每个预设记录 `base_url`、默认模型、密钥环境变量、密钥帮助链接、额外请求头和 `params_override`。可通过 `SIM_PROVIDER`、`SIM_BASE_URL`、`SIM_MODEL`、`SIM_API_KEY` 设置默认模型，或在前端设置面板为每个角色单独选择提供方与模型。

应用设置保存到 `~/.llm-sim/settings.json`（可用 `LLM_SIM_HOME` 指定目录），权限为仅当前用户读写。API 返回时密钥会被遮蔽；空白密钥输入不会覆盖已存密钥。服务端不把密钥写入场景、存档或前端构建产物。

角色路由键：`edict_interpreter`、`narrator`、`character_chat`、`court_debate`、`edict_polish`、`chronicler`。角色提供方或模型值为 `default` 时继承全局配置。

## 请求参数保护与工具回退

预设和用户的 `params_override` 可设置温度等普通参数，但不能覆盖 `messages`、`model`、`tools`、`stream`、`tool_choice` 或 `response_format`。发送工具时，兼容适配器会固定使用 `tool_choice="required"`。

`sim doctor` 与 `POST /api/settings/test` 会尝试工具调用。若提供方明确返回不支持工具的响应，诏令解释器改用 JSON 输出模式解析操作（支持 ```json 围栏）；拒绝、校验和确认仍由本地确定性代码完成。

Agent 默认最多执行 12 步；单次请求默认 30 秒、总计 120 秒。达到限制时停止循环，活动中的草拟请求可经 `POST /api/sessions/{sid}/abort` 中止。

## 图像服务

Mock 图片提供方是默认值。配置 OpenAI 兼容图像端点时，设置 `image_provider`、`image_base_url`、`image_model` 和 `image_api_key`；密钥可由本地环境或设置面板提供。图片生成由 `sim gen-assets` 显式触发，不会因打开游戏而调用外部服务。

# astrbot_plugin_ai_image

AstrBot QQ 群 AI 生图与图片二创插件。

## 命令

- `生图 生成一副随机油画`
- 引用或发送一张图片后：`改图 把脸部优化得自然一些`
- 艾特用户后发送：`改图 把头像变成油画风格`（无图片时自动使用该用户 QQ 头像）
- `生图模型`：从当前 API 获取并列出可用模型

插件会优先从当前消息对象、消息链和原始消息中寻找图片，支持图片 URL、本地路径以及常见 OneBot 图片消息结构。

## 配置

在 AstrBot WebUI 的插件配置中填写：

- `provider=openai`：调用 OpenAI 兼容的 `/images/generations` 和 `/images/edits` 接口。
- `provider=claude`：调用 Anthropic Messages API。原生 Claude API 目前只支持图片理解，不支持生成/编辑；需要使用提供图片输出能力的 Anthropic 兼容网关。
- `api_base`、`api_key`、`model`、`size` 和 `timeout_sec`。
- `generate_progress`、`edit_progress` 可自定义开始处理时的提示文案，留空可关闭提示。
- `size=auto` 时，生图会识别提示词中的 `1:1`、`16:9`、`竖版`、`横版` 等描述；改图会读取原图宽高并匹配最近比例。也可以固定选择 `1:1`、`3:2`、`2:3`、`4:3`、`3:4`、`16:9`、`9:16`。

`model` 填 `auto` 或留空时，插件会请求兼容 API 的 `/models`，优先选择名称包含 `image` 的模型；也可以发送 `生图模型` 查看并手动填写模型 ID。

多供应商请在 AstrBot 原生配置的 `api_providers` 列表中新增。由于原生 Schema 不支持根据列表动态生成“默认供应商”下拉框，插件默认使用列表第一项，失败后按列表顺序尝试后续供应商。

OpenAI 示例：

```text
provider: openai
api_base: https://api.openai.com/v1
model: gpt-image-1
```

修改代码后，在 AstrBot WebUI 中重载插件。API Key 只保存在 AstrBot 配置中，不要提交到仓库。

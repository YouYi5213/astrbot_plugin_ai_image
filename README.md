# astrbot_plugin_ai_image

AstrBot QQ 群 AI 生图与图片二创插件。

## 平台支持

- `aiocqhttp`（OneBot v11 / NapCat 等）
- `qq_official`（QQ 官方机器人 WebSocket）
- `qq_official_webhook`（QQ 官方机器人 Webhook）

图片读取与发送使用 AstrBot 统一消息组件。QQ 官方机器人的用户 ID 是 openid，插件会用 `appid + openid` 构造头像地址；OneBot 平台仍按 QQ 号获取头像。

QQ 官方机器人不会把被引用消息中的图片传给插件。可以把图片和指令放在同一条消息中发送，或者先发一张图片（群里需同时 @机器人），再发送「改图 <描述>」，插件会复用该用户在同一会话最近发送的图片。

## 命令

- `生图 生成一副随机油画`
- 引用或发送一张图片后：`改图 把脸部优化得自然一些`
- 艾特用户后发送：`改图 把头像变成油画风格`（无图片时自动使用该用户头像）
- `生图模型`：从当前 API 获取并列出可用模型
- `生图统计`：在群内显示本群每个用户的生图、改图次数（按总次数降序）；私聊显示个人统计

统计只记录接口成功返回的生图和改图请求，保存在插件目录的 `.usage_stats.json` 文本文件中，不写入 API Key、供应商地址或模型配置。

插件会优先从当前消息对象、消息链和原始消息中寻找图片，支持图片 URL、本地路径以及常见 OneBot 图片消息结构。

## 配置

在 AstrBot WebUI 的插件配置中填写：

- 每个 `api_providers` 条目填写 `provider=openai`、`provider=grok` 或 `provider=claude`、`api_base`、`api_key`、`model` 和尺寸设置。Grok 使用 OpenAI 兼容的 `/images/generations` 和 `/images/edits` 接口，默认模型为 `grok-imagine-image-2.0`；OpenAI 同样调用这两个接口；Claude 调用 Anthropic Messages API。
- `timeout_sec` 是请求超时；未填写 `api_base` 会直接报配置错误，不会自动把请求发送到其他服务。
- `recent_image_ttl_sec` 控制最近图片缓存时间，默认 `1800` 秒；改图没有附图/引用图时复用同一用户在同一会话最近发送的图片，设置为 `0` 关闭。
- `max_concurrent_requests` 控制同一供应商同时处理的生图/改图数量，默认 `1`；超过数量的请求排队等待，设置为 `2` 或 `3` 可开启并行处理。
- `cooldown_sec` 可限制同一用户连续调用的频率，设置为 `0` 关闭；`allowed_users` 和 `allowed_groups` 可填写白名单 ID，多个 ID 用逗号或空格分隔，留空表示不限制。
- `generate_progress`、`edit_progress` 可自定义开始处理时的提示文案，留空可关闭提示。
- 供应商全部失败时会按供应商逐行显示简化错误，并引用原始生图/改图消息回复，不直接转发完整上游响应体。
- `size=auto` 时，生图会识别提示词中的 `1:1`、`16:9`、`竖版`、`横版` 等描述；改图会将原图等比例放入接口画布，并保留接口返回的完整画布，避免模型输出内容在中心裁剪时被截断。也可以固定选择 `1:1`、`3:2`、`2:3`、`4:3`、`3:4`、`16:9`、`9:16`。当使用官方 `api.openai.com` 的 `gpt-image-*` 时，非官方尺寸会自动改用受支持画布，并以补边方式还原目标比例，不会裁掉生成内容。

`model` 填 `auto` 或留空时，插件会请求兼容 API 的 `/models`，优先选择名称包含 `image` 的模型；也可以发送 `生图模型` 查看并手动填写模型 ID。

多供应商请在 AstrBot 原生配置的 `api_providers` 列表中新增。每个供应商可设置 `priority`（数字越小越优先）来调整尝试顺序；相同优先级按列表中的原顺序处理。也可以通过 `default_provider` 指定某个供应商强制作为首选，失败后再按优先级尝试其他供应商。

OpenAI 示例：

```text
provider: openai
api_base: https://api.openai.com/v1
model: gpt-image-1
```

修改代码后，在 AstrBot WebUI 中重载插件。API Key 只保存在 AstrBot 配置中，不要提交到仓库。

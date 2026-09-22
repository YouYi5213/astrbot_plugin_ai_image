# Codex 项目记忆 / 交接记录

> 最后更新：2026-09-22

## 项目定位

这是一个 AstrBot QQ 群 AI 生图与改图插件，入口文件为 `main.py`，通过正则命令监听：

- `生图 <提示词>`
- `改图 <提示词>`（引用图片、消息图片或艾特用户头像）
- `生图模型`

支持 OpenAI Images 兼容接口和 Anthropic Messages 兼容网关。插件元数据在 `metadata.yaml`，配置 Schema 在 `_conf_schema.json`，用户配置由 AstrBot 管理，不应提交到仓库。

## 当前代码状态

- 基线提交：`f51b748`（`feat: add AI image generation plugin`）
- 工作区存在未提交改动：`main.py`、`_conf_schema.json`、`README.md`
- `ANALYSIS.md` 是静态分析报告，记录了已验证问题和待处理风险
- 依赖：`httpx>=0.27`、`Pillow>=10.0`
- 支持平台：`aiocqhttp`
- AstrBot 版本声明：`>=4.16`

## 已知实现重点

- 供应商通过 `api_providers` 列表配置，可用 `priority` 和 `default_provider` 控制故障转移。
- 图片解析兼容消息链、OneBot 常见结构、URL、本地路径和艾特头像。
- 改图的 auto 尺寸路径会先等比例补边，再调用接口，之后按目标比例处理返回图。
- 当前插件实例使用请求上下文和重试/超时逻辑；修改这些部分时要特别检查并发、取消和资源清理。
- 已加入 `生图统计`：成功的生图/改图按群、QQ 号累计到插件目录 `.usage_stats.json`，群内查询只读取当前群，失败请求不计数。

### 本次 Grok 适配变更

- 新增 `provider=grok` 配置模板，默认模型 `grok-imagine-image-2.0`，默认地址可填 `https://api.x.ai/v1`。
- Grok 复用 OpenAI Images 请求路径，并兼容 `url`、`image_url`、`imageUrl`、`b64_json`、`b64`、`base64` 图片返回字段。
- 修复 OneBot `image.data.url` 和 `at.data.qq` 嵌套字段提取；空 `api_base` 回退到官方 OpenAI 地址。

### 本次统计功能变更

- 新增命令：`生图统计`。
- 群聊按当前群号隔离，按总次数降序显示 QQ 号、最近网名、生图次数、改图次数和总次数。
- 私聊只读取 `private` 范围，不展示群成员数据。
- 统计文件已加入 `.gitignore`，不进入仓库，也不写入 AstrBot 的 API 配置。
- 统计只在接口成功返回图片后增加次数。

## 已知问题与优先级

优先查看 `ANALYSIS.md` 的问题清单。当前最重要的风险是：

1. Schema 曾缺少 `provider`，导致 Claude 分支无法通过界面配置。
2. OneBot `image.data.url` / `at.data.qq` 等嵌套结构需要确保能被解析。
3. 空 `api_base` 应明确报配置错误，不能退化成相对 URL。
4. 生图命令与 `生图模型` 的正则要避免重叠。
5. 下载图片需要大小限制、协议/私网校验和及时清理临时文件。
6. 上游错误信息不能把完整响应体或敏感内容直接发到群里。

不要一次性解决全部问题；按用户当前目标选择最小改动范围。

## 验证命令

在项目根目录运行：

```powershell
python -m py_compile .\main.py
python -c "import json; json.load(open('_conf_schema.json', encoding='utf-8')); print('schema ok')"
git status --short
```

如环境安装了 Ruff，可额外运行：

```powershell
ruff check .
```

## 部署约束

- 不覆盖 `/www/wwwroot/data/config/` 下的配置文件。
- 特别注意：`/www/wwwroot/data/config/astrbot_plugin_ai_image_config.json` 是不可替换文件。任何部署都必须保留原文件，并在部署前后核对 SHA-256 不变。
- 保留供应商 API 地址、API Key 和模型 ID。
- 只替换插件代码与元数据；部署前创建带时间戳的代码备份。
- 优先使用 AstrBot 插件级重载；未启用 `ASTRBOT_RELOAD=1` 时不要假设文件替换会自动重载。
- 未经用户明确授权，不重启整个 AstrBot 容器、不删除备份。

## 会话交接模板

每次阶段性完成后更新：

```text
当前目标：
已完成：
修改文件：
验证结果：
未解决/风险：
下一步：
```

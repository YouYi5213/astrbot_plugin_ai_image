# astrbot_plugin_ai_image 代码分析报告

| 项 | 内容 |
|---|---|
| 分析对象 | `astrbot_plugin_ai_image` v0.1.0 |
| 代码状态 | commit `f51b748` + 工作区未提交改动（`README.md`、`_conf_schema.json`、`main.py` 共 103 行） |
| 分析方法 | 静态阅读 + 拉取 AstrBot 上游源码核对框架 API + 桩模块加载真实 `main.py` 跑行为测试 |
| 框架基线 | AstrBot `master` 分支源码（`RegexFilter`、`star_handler.py`、`star_request.py` 等） |

> 说明：第三方的 AstrBot 行号取自 master 分支，若你的运行版本不同，行号可能有偏移，结论以代码逻辑为准。

---

## 一、项目概况

单文件插件，585 行 `main.py`，配套 `_conf_schema.json` / `metadata.yaml` / `README.md`。

| 项 | 内容 |
|---|---|
| 定位 | QQ 群「生图 / 改图」，走 OpenAI 兼容的 `/images/generations`、`/images/edits` |
| 依赖 | `httpx>=0.27`、`Pillow>=10.0` |
| 平台 | `aiocqhttp` |
| 入口 | `@filter.regex` 三个正则监听：`生图` / `改图` / `生图模型` |

---

## 二、架构

```
@filter.regex 三入口
      │
      ▼
_run_with_failover  ── 多供应商遍历 + 每供应商 retry_count 次重试
      │                （整体包在 asyncio.Lock 内）
      ▼
_run ──► _openai   (provider != claude)
      └─► _claude   (provider == claude)
      │
      ▼
_download ──► _resolve_size ──► _letterbox_edit_input ──► API
                                                          │
                                          _crop_result_to_ratio ◄┘
```

**设计亮点**（这部分做得不错）：

- `_find_image_urls` / `_find_at_ids` 用递归 + `id()` 去重，适配多种消息结构。
- 改图走「等比例补边上传 → 返回后中心裁剪」，避免兼容网关直接拉伸原图。
- `priority` 提供了不依赖 WebUI 拖拽能力的供应商排序方式。

---

## 三、已核实的正确实现

以下框架 API 用法逐条对着上游源码验证过，**没有问题，不需要改**：

| 结论 | 依据 |
|---|---|
| 传**编译好的**正则给 `filter.regex` 合法 | `RegexFilter.__init__` 是 `re.compile(regex)`；Python 对已编译 Pattern 原样返回，插件自身的 `re.S` 生效。过滤器用 `regex.search(message_str.strip())`，与 `^...$` 锚点匹配 |
| `event.stop_event()` 之后再 `yield` 结果可行 | 这是官方插件开发文档 7.3 节的标准写法；`is_stopped()` 只看 `_force_stopped`，不影响结果投递 |
| `priority=20/21` 真的生效 | `star_handler.py` L21-26 按 `-extras_configs["priority"]` 排序；`star_request.py` L36-38 遇 `is_stopped()` 即 `break` |
| 先 `event.send()` 发进度、再 `yield` 图片不会互相吞掉 | `MessageEventResult` 继承自 `MessageChain`（`message_event_result.py:224`），`send()` 类型完全匹配；且 `_has_send_oper` 在 respond stage 中未被使用 |
| `template_list` 是合法 schema 类型 | `astrbot/core/config/default.py` L4849 的 `DEFAULT_VALUE_MAP` 中存在该类型，默认值 `[]` |
| `templates` / `display_item` / `items` 写法符合惯例 | 与 AstrBot 平台配置的结构一致 |
| `priority` 作为未知 kwarg 不会导致崩溃 | `get_handler_or_create` 把多余 kwargs 收进 `md.extras_configs`，不会抛 `TypeError` |

---

## 四、问题清单

### 🔴 严重

#### 1. `provider` 字段在 Schema 中根本不存在 → Claude 分支是死代码

代码在 4 处读取 `_cfg("provider", "openai")`：`main.py:197`、`main.py:209`、`main.py:227`、`main.py:428`。

但 Schema 顶层键只有 `api_providers / default_provider / timeout_sec / generate_progress / edit_progress`，两个模板的 `items` 里**都没有** `provider`。

实测（用 claude 模板创建并填满全部字段的 profile）：

```
_cfg('provider') -> 'openai'   <-- 决定走 _claude() 还是 _openai()
_run() 实际调用  -> openai()
```

**后果**：

- WebUI 里选「Claude 兼容」和选「OpenAI 兼容」行为**完全相同**。
- `_claude()`（`main.py:406-425`）永不执行。
- README 第 19/31 行的 `provider=claude` 无法通过界面配置。

**修复**：模板里补

```json
"provider": {"type": "string", "options": ["openai", "claude"], "default": "openai"}
```

#### 2. OneBot 原生 `data.url` 嵌套结构解析失败 → 改图找不到图

`_find_image_urls` 判定为 image 组件后只查顶层 `url/file/path/src`（`main.py:73-76`），而递归时又把这几个键排除（`main.py:78`），于是嵌在 `data` 里的 URL 永远取不到。

实测：

| 消息结构 | 结果 |
|---|---|
| `{"type":"image","url":"..."}` | ✅ 命中 |
| `{"type":"image","data":{"url":"..."}}` | ❌ `[]` |
| `{"type":"reply","chain":[{"type":"image","data":{"url":"..."}}]}` | ❌ `[]` |

`{"type":"image","data":{...}}` 恰是 OneBot v11 最常见的图片结构，也是引用图片的常见结构，而 README 明确声称「支持常见 OneBot 图片消息结构」。

`_find_at_ids`（`main.py:97-103`）对 `{"type":"at","data":{"qq":"..."}}` 同样返回空。

**修复**：image 分支同时取 `data.get("url")`，或让递归允许进入 `data` 子字典。

#### 3. profile 的 `api_base` 留空 → 请求 URL 退化成 `/images/generations`

Schema 里 `api_base` 的 default 是 `""`。用户新建供应商条目若漏填，`_cfg("api_base", "https://api.openai.com/v1")` 返回 `""` 而不是默认值 —— 因为 key 存在、值是空串，`dict.get` 的默认值不触发。

实测：

```
_base()              -> ''
models endpoint      -> '/models'
generations endpoint -> '/images/generations'
```

httpx 会抛 `UnsupportedProtocol`，用户看到的是「生图失败：…」，无法定位到「没填 API 地址」。

**修复**：`_base()` 改为

```python
return str(self._cfg("api_base", "") or "https://api.openai.com/v1").rstrip("/")
```

---

### 🟠 中等

#### 4. `生图模型` 与 `生图` 正则重叠，且媒体前缀处理不一致

实测 `_GEN_RE`（`main.py:23`）也匹配 `生图模型`，把「模型」当提示词。目前靠 `priority 21 > 20` 让 `models` 先跑并 `stop_event()` 才没出错 —— 但这是「被优先级救回来的」，很脆弱。

真正的 bug 是 `_MODELS_RE`（`main.py:25`）**没有** `_MEDIA_PREFIX` 前缀处理，而 `_GEN_RE` / `_EDIT_RE` 都有：

```
[图片] 生图模型  -> GEN  prompt='模型'   ← 会真的去生成一张「模型」的图
```

**修复**：`_MODELS_RE` 加同样的前缀，并给 `_GEN_RE` 加负向断言排除「模型」。

#### 5. 官方 OpenAI 尺寸只有 3 种，7 个比例预设里 4 个会 400

`_ratio_size`（`main.py:258-264`）：

| 比例 | 映射尺寸 | 官方 `gpt-image-1` |
|---|---|---|
| 1:1 | 1024x1024 | ✅ |
| 3:2 | 1536x1024 | ✅ |
| 2:3 | 1024x1536 | ✅ |
| 4:3 | 1536x1152 | ❌ |
| 3:4 | 1152x1536 | ❌ |
| 16:9 | 1536x864 | ❌ |
| 9:16 | 864x1536 | ❌ |

而 README 的示例恰好是 `api_base: https://api.openai.com/v1` + `model: gpt-image-1`。代码显然是照「兼容网关」设计的，但文档把官方地址当示例 → 用户照 README 配完再选 16:9 必失败。

> ⚠️ 待核实：OpenAI 官方文档站被 Cloudflare 拦截，未能在分析会话内二次取证，请按自己的网关实测确认。

**修复思路**：复用已有的 `_crop_result_to_ratio` —— 非官方比例先映射到最近的支持尺寸，再裁回目标比例。

#### 6. 全局串行锁 + 超时/重试叠加，一个请求堵住所有人

`_run_with_failover` 整体包在 `async with self._request_lock`（`main.py:455`）。

原因可以理解：`_request_profile` 是实例级状态（`main.py:114`），不串行会串号。但代价是 60s 超时 × N 个供应商 × M 次重试期间，其他所有人的生图全部排队 —— 群聊场景很容易堆积。

**根治办法**：把 profile 上下文换成 `contextvars.ContextVar`，即可去掉全局锁。

#### 7. `_request_profile` 没有 `finally` 复位

`main.py:466` 设置，`main.py:471` / `main.py:476` 复位。若 `asyncio.CancelledError`（插件重载、任务取消）在循环中抛出，状态会残留并污染后续请求 —— 与第 6 点叠加时影响面更大。

#### 8. httpx client 超时被首个请求「冻结」，且默认值三处打架

`_http()`（`main.py:157-164`）惰性创建后缓存，超时只在首次创建时求值一次。默认值三处不一致：

- `_http()`：180（`main.py:159`）
- `_run_with_timeout()`：60（`main.py:480`）
- Schema：60

另外 `timeout_sec` 不在模板里，per-profile 超时实际不支持。

#### 9. `_download` 无大小上限、无 SSRF 防护

`main.py:241-256` 把 `response.content` 全量读进内存，而 URL 来自群消息。

- 恶意用户可提交 `http://127.0.0.1:port/` 让 bot 去请求内网。
- 也可用超大文件打爆内存。

**修复**：限制协议/端口/私网 IP，加 `Content-Length` 上限并流式读取。

#### 10. 临时文件生命周期 = 插件生命周期

`_temp_paths`（`main.py:112`）只在 `terminate()`（`main.py:576-584`）清理。

长期运行的 bot 上，每张图（含中间 letterbox 图、裁剪结果）都会一直留在磁盘，`set` 本身也无限增长。建议请求结束即删。

#### 11. 无权限/频率限制

任何群成员都能无限调用，直接烧 API 额度。缺少群/用户白名单、冷却时间、每日配额。

对「按张计费」的生图接口，这是风险最高的一项缺失。

---

### 🟡 轻微 / 代码质量

#### 12. README 与 Schema 漂移

README 第 20 行让人在 WebUI 填 `api_base`、`api_key`、`model`、`size` —— 这四个顶层键在 Schema 里**都不存在**，只能填在 `api_providers` 条目内；`size` 则完全不存在（仅代码里的 legacy 兜底）。

实测对照表：

| 代码读取的键 | 在顶层 Schema | 在模板 items |
|---|---|---|
| `provider` | ❌ | ❌ |
| `api_base` | ❌ | ✅ |
| `api_key` | ❌ | ✅ |
| `model` | ❌ | ✅ |
| `size` | ❌ | ❌ |
| `generate_size` | ❌ | ✅ |
| `edit_size` | ❌ | ✅ |
| `timeout_sec` | ✅ | ❌ |
| `generate_progress` | ✅ | ❌ |
| `edit_progress` | ✅ | ❌ |
| `default_provider` | ✅ | ❌ |
| `retry_count` | ❌ | ✅ |
| `priority` | ❌ | ✅ |

#### 13. 死代码

- `_save_config`（`main.py:152`）从未被调用。
- `_profile_names`（`main.py:126`）在未提交的重构中被 `list(profiles)` 取代后成了孤儿。
- `_profiles()` 里 `base_url`/`api_keys` 的归一化（`main.py:137-141`）对应的是 AstrBot **原生** provider 字段名，本插件 Schema 用的是 `api_base`/`api_key` —— 注释和代码都是误植。

#### 14. `_send_progress` 的异常兜底多余

`MessageEventResult` 就是 `MessageChain` 子类，首次调用必成功，`try/except`（`main.py:170-176`）可删。

#### 15. 固定比例改图不做 letterbox

`_edit_size_is_auto()`（`main.py:311-315`）为 False 时直接上传原图，用户可能拿到被网关拉伸的图。README 说是 auto 才补边，属有意设计，但值得在文档里显式说明。

#### 16. 上传 MIME 与实际字节不符

`main.py:384` 硬编码 `image/png`，但非 auto 路径下 `upload_path` 可能是 `_download` 下来的 `.jpg`（`main.py:248-251`）。

auto 路径因为 `_letterbox_edit_input` 总是输出 PNG 所以没问题；严格网关会拒绝非 auto 路径。

#### 17. 错误信息可能外泄

`_api_error`（`main.py:186-188`）把上游响应体前 300 字符原样发到群里，部分网关的错误体会回显 key 或内部地址。

#### 18. 补边用黑边

`main.py:333` RGB 模式填 `(0,0,0)`。黑边会被模型当成图像内容，影响边缘生成质量。建议改边缘像素延展或中灰。

#### 19. 其他

- `_cfg()` 每次调用都重建 `_profiles()` 字典（单次请求内被调十几次）。
- `ruff check` 报 17 项：8 个盲捕获 `BLE001`、5 个 `re.S`/`re.I` 别名 `FURB167`、1 个异步阻塞 `open` `ASYNC230`、1 个 `try/except/pass` `S110`、1 个导入排序 `I001`。
- `_claude` 里 `main.py:422` 一行塞了 5 条语句，可读性差。
- `_base()` 已 `rstrip("/")` 过，但 `_list_models`（`main.py:210`）里又 `rstrip` 一次。

---

## 五、建议的修复顺序

| 顺序 | 条目 | 理由 |
|---|---|---|
| 1 | **1、2、3** | 直接决定插件「能不能用」：Claude 配不了、OneBot 图片认不出、漏填地址报错看不懂 |
| 2 | **4、5** | 命令歧义和尺寸失败是用户最容易撞上的体验问题 |
| 3 | **9、10、11** | 上线到公开群之前必须处理的安全/资源问题 |
| 4 | **6、7、8** | 一起重构：用 `contextvars` 替掉 `_request_profile` + 全局锁，顺带修好超时 |
| 5 | **12-19** | 文档漂移与死代码，跑一遍 `ruff --fix` |

---

## 附录 A：待确认现象 —— 自适应改图后右上角文字被裁掉

用户实测反馈：原图经 `edit_size=auto` 改图后，输出图右上角文字显示不全。

**代码路径**（`main.py:369-404`）：

1. `_resolve_size(prompt, path, "edit_size")` —— auto 时按原图宽高比取**最近的档位比例**（`main.py:294-297`），例如 1.4:1 会落到 3:2。
2. `_letterbox_edit_input(path, size)` —— 把原图 `ImageOps.contain` 等比缩放进目标画布并**居中**，四周补黑边。
3. API 返回目标尺寸的图。
4. `_crop_result_to_ratio(result_path, edit_ratio)` —— **中心裁剪**回原图比例（`main.py:342-367`）。

**推测原因**：档位比例与原图真实比例存在偏差（第 1 步的「取最近档位」是量化误差来源），第 4 步的中心裁剪会按该偏差切掉上下或左右边缘。原图比例离档位越远，被裁掉的边缘越多 —— 靠近边缘的内容（如右上角文字）就会缺失。

**候选修复方向**：

- 让 `_resolve_size` 在 auto 模式下直接使用原图的真实宽高（不量化到档位），或在量化后仍按真实比例裁剪。
- 把中心裁剪改为「先按真实比例裁剪、再检查是否丢失内容」，或干脆不裁剪而保留补边。
- 补边颜色从黑色改为边缘像素延展，避免模型把黑边当内容。

> 状态：用户已指示暂停调查，以上为未验证推测，尚未复现取证。

---

*报告生成时间：本次分析会话*

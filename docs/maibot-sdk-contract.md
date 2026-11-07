# MaiBot 插件 API 契约摘要（com.calarence.persona / SDK 2.10~2.x）

> 研究日期：2026-11-07。来源标记：
> - **【文档】** = 官方文档明确写了（附 URL）
> - **【SDK】** = 本地 vendored 的 `maibot-plugin-sdk 2.21.0` wheel 类型定义实证（`.sdk/pkg/maibot_sdk/`，即可信源码级证据）
> - **【⚠探针】** = 文档与 SDK 都未定义，必须在运行时探测确认
>
> 文档版本提示：文档页标注「更新: 2026/11/05」。站点是 VitePress，正文 HTML 会截断；本摘要对长页面使用了 Markdown 源镜像
> `https://cdn.jsdelivr.net/gh/Mai-with-u/docs@main/zh/plugin/<page>.md`（与「在 GitHub 上编辑此页」指向同一仓库 `Mai-with-u/docs`）。

## 0. 来源清单

| 页面 | URL | 状态 |
|---|---|---|
| Hook 处理器 | https://docs.maibot.chat/plugin/hooks | ✅ HTML 末尾截断，已用 md 源补全 |
| Manifest | https://docs.maibot.chat/plugin/manifest | ✅ 完整 |
| 配置管理 | https://docs.maibot.chat/plugin/config | ✅ 完整 |
| 生命周期 | https://docs.maibot.chat/plugin/lifecycle | ✅ 完整（md） |
| WebUI 页面 | https://docs.maibot.chat/plugin/webui-pages | ✅ 完整 |
| API 参考 | https://docs.maibot.chat/plugin/api-reference | ✅ 完整（md） |
| 回复扩展 | https://docs.maibot.chat/plugin/reply-extensions | ✅ 完整（md） |
| 插件接入 | https://docs.maibot.chat/develop/plugin | ✅ 完整 |
| 补充：消息网关 | https://docs.maibot.chat/plugin/message-gateway | ✅（消息字典字段来源） |
| 补充：API 组件 | https://docs.maibot.chat/plugin/api-components | ✅（@API 声明） |
| 补充：事件处理器 | https://docs.maibot.chat/plugin/event-handlers | ✅ |
| 补充：开发指南 | https://docs.maibot.chat/plugin/ | ✅ |

**文档自身的不一致（以 hooks 页与运行时为准）**
- `develop/plugin` 说「22 个命名钩子」；`plugin/hooks` 说 **29 个**并给出完整清单 → 以 hooks 页 + `GET /plugins/runtime/hooks` 为准。
- `develop/plugin` 说「当前配套 SDK 是 2.9 系列」；而 api-reference 引用了 2.11/2.17/2.18/2.20/2.21 的能力 → 文档版本漂移，本项目按 sdk>=2.10 走。

---

## 1. Hook 载荷（最重要）

### 1.1 通用契约【文档 https://docs.maibot.chat/plugin/hooks】

```python
@HookHandler(
    hook: str,                                    # 必填，必须是 Host 已注册的 hook 名
    *, name: str = "", description: str = "",
    mode: HookMode = HookMode.BLOCKING,           # BLOCKING / OBSERVE
    order: HookOrder = HookOrder.NORMAL,          # EARLY / NORMAL / LATE
    timeout_ms: int = 0,                          # 0 = 用 Hook 默认值
    error_policy: ErrorPolicy = ErrorPolicy.SKIP, # ABORT / SKIP / LOG
    **metadata,
)
```

返回值（仅 BLOCKING 被采纳；OBSERVE 返回值一律忽略）：
```python
{"action": "continue" | "abort",
 "modified_kwargs": dict,   # 完整替换后续处理器收到的 kwargs（不是增量合并！）
 "custom_result": Any}      # abort 时用 {"reason": "原因"}
```

- abort 仅限「允许 abort ✅」的 Hook；`error_policy=ABORT` 同上，违规 **注册直接失败**【文档】。
- abort 优先于同次返回里的正文改写与 `retry`；第一个有效 abort 后不再调度后续处理器【文档】。
- 调度顺序：mode(blocking 先) → order → 内置插件先于第三方 → 插件 ID 字典序 → 处理器名【文档】。
- 运行时权威清单：`GET /plugins/runtime/hooks`（含 `allow_abort`）【文档】。
- 已注册 29 个。SDK 文档里另有 3 个**未注册**的（`emoji.register.after_build_emotion`、`jargon.query.before_search`、`jargon.query.after_search`），订阅它们 = 注册失败【文档】。

### 1.2 `maisaka.replyer.before_request`

| 项 | 结论 |
|---|---|
| abort | ✅ 允许（取消本次生成；不发模型请求、不触发重试/切换/故障惩罚） |
| 改参 | ✅ 允许 |
| OBSERVE | 未明确写；Host 校验 mode 与 Hook 能力兼容【⚠探针：实际允许集合】 |

文档明确写的字段（可改）：
```python
task_name:      str   # 本次 replyer 任务名；改为该任务的默认模型池 + 生成参数
model_name:     str   # 具体模型名，必须存在于 model_config.toml 的 [[models]]；
                      # 指定后只尝试该模型一次，不再按任务池轮换
extra_prompt:   str   # 追加到本次 replyer prompt 的额外要求
reply_reason:   str   # 本次 reply 工具传入的引用信息
reply_tool_args: dict # reply 工具额外参数；改动会传给后续 replyer hook
```
文档可读但未解释语义的字段（来自官方 abort 示例签名 `async def guard_reply(self, request_type="", reply_message_id="", **kwargs)`）：
```python
request_type:     str  # 【⚠探针】示例只用于 == "maisaka.replyer" 判断
reply_message_id: str  # 【⚠探针】示例判断其非空
```
- `session_id` / `stream_id` / `chat_info` / `chat_history` 是否出现在该 hook：**文档未写**【⚠探针】。
- `reply_tool_args` 的内部排除项：`msg_id`、`_reply_id`、`_plugin_reply_prompt`【文档】。
- `reply_tool_args` 在「表达方式选择链 → before_request → after_response」三段保持可见，`before_request` 的修改会继续向后传【文档】。
- 来源：https://docs.maibot.chat/plugin/hooks（「在 replyer 请求前切换模型或追加提示词」小节）

### 1.3 `maisaka.replyer.before_model_request`

| 项 | 结论 |
|---|---|
| abort | ✅ 允许（取消本次生成，模型请求不会发出） |
| 改参 | ✅ 允许（通过 `modified_kwargs["items"]` 整表返回新的 Items） |
| OBSERVE | 未明确【⚠探针】 |

【文档】明确的：
- 触发时机：replyer **已按当前模型能力构造完 Context Items** 之后、请求模型之前。
- `items`：是「Items / Context Items」列表，返回值格式为 `{"action": "continue", "modified_kwargs": {... , "items": [...]}}`。
- 文档推荐用途原文：「适合在 `system` 后**插入一条合成的第一条 `user` 消息**」。
- 只改写本次临时 LLM 请求：**不回写聊天历史，也不影响中期记忆插入**。

【⚠探针】文档完全未写的部分（必须实测）：
- `items` 是否 `List[dict]`；单元素真实结构（`item_type` / `meta` / `parts` 的字段名与取值）。
- 合成 item 是否必须提供 `item_id` / `timestamp` / `logical_turn_id`，以及与真实历史 item 的 id 冲突规则。
- 该 hook 除 `items` 外还有哪些 kwargs（`session_id` / `reply_tool_args` / `model_name` 等均未写）。
- `message` 提示词内容在 item 中的落点（与 `llm.generate(prompt=[{role,content}])` 的 dict 格式是否同构）。

来源：https://docs.maibot.chat/plugin/hooks（「如果需要改写 replyer 真正发给模型的消息列表…」段）

### 1.4 `chat.receive.after_process`

| 项 | 结论 |
|---|---|
| abort | ✅ 允许（停止对应消息处理） |
| 改参 | ✅ 允许 |
| OBSERVE | ✅ 合法（官方示例就用 OBSERVE 做日志），返回值被忽略 |

- kwargs 顶层：`message`（dict）【文档，示例 `kwargs.get("message", {})`】。
- `message` 内字段——文档**实际出现**的只有：`raw_message`、`user_id`【文档 hooks 示例】。
- 入站消息（`chat.receive.*` / `gateway.route_message` 同一套 MessageDict）**文档确认过**的字段【https://docs.maibot.chat/plugin/message-gateway】：
```python
message_id:   str
platform:     str            # 如 "qq"
account_id:   str            # 非空，机器人账号（不是发送者）
scope:        str | None     # 连接/租户/子通道；注意是 scope，文档没有 "route_scope"
timestamp:    str            # UNIX 秒的字符串
message_info: {
    user_info: {"user_id": str, "user_nickname": str},
    additional_config: dict,  # 平台扩展，如 platform_io_target_user_id
}
raw_message:  list[dict]      # 消息段，如 [{"type":"text","data":"..."}]
                             # 图片/表情段可带 binary_data_base64；转发在 data[].content 递归
```
- 【⚠探针】你关心的这些字段名文档**未写**：`route_scope`（文档叫 `scope`）、`chat_type`、`chat_id`、`sender_name`（文档是 `message_info.user_info.user_nickname`）、`stream_id`、`session_id`。
- 相关补充：`chat.receive.before_process` 可返回 `modified_kwargs={"message": updated_message}`，在图片后插入 `{"type":"text","data":"识别提示"}`【文档】。

### 1.5 `send_service.after_send`

| 项 | 结论 |
|---|---|
| abort | ❌ 不允许（声明 `error_policy=ABORT` 会注册失败） |
| 改参 | ❌ 不允许 |
| OBSERVE | ✅ 唯一有意义的用法（「发送完成…观察通知保持不可中止」） |

- kwargs 字段列表：**文档未列**【⚠探针】。
- 唯一可确认：kwargs 里含 `message`（出站 MessageDict），因为 SDK 文档串写明「麦麦发给主人的消息可在 `send_service.after_send` 中按 `message["platform"]` 等于 platform、`message_info.additional_config.platform_io_target_user_id` 等于 user_id 识别」【SDK `.sdk/pkg/maibot_sdk/capabilities/internal_chat.py` 注释】。
- 【⚠探针】是否成功 / `message_id` / `stream_id` / `session_id` / `metadata` 等字段名与类型；普通回复 vs 主动消息如何区分（文档未给任何标志位）。

### 1.6 `maisaka.reply.before_post_process`

| 项 | 结论 |
|---|---|
| abort | ✅ 允许（取消本次回复：后续重生成、后处理、发送停止） |
| 改参 | ✅ 允许 |
| OBSERVE | 未明确【⚠探针】 |

可读字段【文档】：`response`、`session_id`、`reply_message_id`、`reply_tool_args`。

可改字段【文档】：
```python
response:           str   # 本次最终可见回复正文
skip_post_process:  bool  # true = 完全跳过 process_llm_response
                          #（文本拆分 + 中文错别字注入 + 括号清理 + 长度限制）
enable_splitter:    bool  # 是否允许按全局配置做文本拆分
enable_chinese_typo:bool  # 是否允许按全局配置注入中文错别字
```
- 三个策略字段**必须返回布尔值**，且必须保留其余 kwargs。
- `skip_post_process` 只跳过正文文本后处理，**不跳过**图片/At/表情等富回复附件组装。
- 重写回复另用 `retry=True` + `retry_reason`（后者作为下一次生成的约束）【文档】。
- Planner 侧可见中止反馈：`error_code="plugin_aborted"`、`reason`、`hook_abort: {hook_name, handler_name, reason}`；reply 另有 `model_empty_response` / `plugin_empty_response`【文档】。

### 1.7 `expression.select.before_select` / `after_selection`

| 项 | 结论 |
|---|---|
| abort | ✅ 都允许；但 abort **只跳过表达选择，回复仍会继续**，原因保留在选择结果与生成指标中 |
| 改参 | ✅ 都允许 |
| OBSERVE | 未明确【⚠探针】 |

```python
# before_select（文档全量列出）
chat_id, session_id, chat_info, chat_history,
reply_message, reply_tool_args, target_message,
reply_reason, max_num, think_level, candidates
# after_selection = 上述 + 
selected_expression_ids, selected_expressions
```
- 该链里 `reply_tool_args` 的排除项是 `msg_id`、`set_quote`、`reference_info`（注意：与 replyer 链的排除项不同，两处文档说法不一致 ⚠）。
- 改写示例：`kwargs["selected_expression_ids"] = [item["id"] for item in candidates[:1]]`（candidate 至少含 `id`）。

### 1.8 其它有载荷线索的 Hook（便于复用）

- `send_service.before_send`：可改 `typing: bool`、`show_log: bool`【文档示例】。
- `send_service.after_build_message`：可改 `processed_plain_text: str`【文档示例 https://docs.maibot.chat/develop/plugin】。
- `maisaka.planner.before_request`：kwargs 含 `tool_definitions: list[dict]`（标准 function-calling schema，可追加 `properties`）；**不允许 abort**，只允许改参。
- `maisaka.replyer.after_response`：可改写回复或要求重生成（`retry`），允许 abort。

### 1.9 本节「文档未覆盖、必须运行时探针」清单

1. `chat.receive.after_process` / `send_service.after_send` 的完整 kwargs 键集合与类型。
2. `message` 字典在 Hook 内是否含 `stream_id` / `session_id` / `chat_type` / `chat_id` / `sender_name`（文档只在 HTTP 入站协议里给了 `scope`、`message_info.user_info.*`）。
3. `maisaka.replyer.before_model_request` 的 `items` 元素结构（`item_type`/`meta`/`parts`/`item_id`/`timestamp`/`logical_turn_id`）。
4. `maisaka.replyer.before_request` 是否有 `session_id`/`chat_info`/`chat_history`/`request_type` 的稳定语义。
5. 每个 Hook 允许的 `mode` 集合（Host 侧校验规则未公开，只看得到 `allow_abort`）。
6. 各 Hook 的默认 `timeout_ms`（文档只写「0 = 使用 Hook 默认值」；桌宠链写 5 s）。
7. 普通回复 / 主动消息在 `send_service.after_send` 中的区分方式。

探针建议（不改业务，只观测）：对每个目标 hook 注册一个 `HookMode.OBSERVE` + `ErrorPolicy.SKIP` 的处理器，`self.ctx.logger.info("%s", sorted(kwargs.keys()))`，对 dict 值再打一层 `type()`/`keys()`；确认结构后再上 BLOCKING 改参。

---

## 2. Manifest（`_manifest.json`，schema v2）

来源：https://docs.maibot.chat/plugin/manifest

### 2.1 必填

| 字段 | 类型 | 约束 |
|---|---|---|
| `manifest_version` | `2` | 固定 2 |
| `id` | string | `^[a-z0-9]+(?:[.-][a-z0-9]+)+$` → `com.calarence.persona` ✅ |
| `version` | string | 严格三段式 `X.Y.Z` |
| `name` | string | 展示名 |
| `description` | string | |
| `author` | object | `{name: str, url: str}`，url 必须 http(s) |
| `license` | string | |
| `urls` | object | `repository` **必填**（http/https）；`homepage`/`documentation`/`issues` 可选 |
| `host_application` | object | `{min_version, max_version}` |
| `sdk` | object | `{min_version, max_version}` |
| `capabilities` | string[] | 逐项能力名，不得含空值 |
| `i18n` | object | `default_locale` **必填**；`locales_path`、`supported_locales` 可选 |

### 2.2 可选

- `plugin_type`：`adapter|tool|provider|management|data|media|game|integration|other|chat|creative|automation|deskpet`；缺省/`extension` = 无类别。Host 接受任意字符串，不强制枚举。
- `display.icon`：`{type: "lucide"|"emoji"|"local", value, fallback?, background?}`；local 只能是插件目录内 `.png/.jpg/.jpeg/.webp/.svg`，禁绝对路径 / `..` / 符号链接 / 在线 URL。
- `dependencies[]`：`{type:"plugin", id, version_spec}` / `{type:"python_package", name, version_spec}`（PEP 440；禁自依赖、重复、循环）。
- `llm_providers[]`：`{client_type, name, description?, version?}`；必须与 `@LLMProvider` 双重声明且 `client_type` 完全一致，否则阻止加载；同 client_type 冲突 → 双方都禁载。
- ⚠ **manifest 中没有 `webui` 字段**：WebUI 页面由插件目录下的独立 `webui.json` 声明（见第 4 节）。

### 2.3 版本区间语义（重点，容易踩）

```json
{ "min_version": "2.10.0", "max_version": "2.21.99" }
```
- 闭区间；两端必须严格 `X.Y.Z`；`min_version <= max_version`。
- `max_version` 写法：**实测过的最高「主.次」+ .99**。禁止 `1.99.99` / `999.999.999`。
- 校验时机：Runner 加载时 `ManifestValidator`；Host 通过 `MAIBOT_HOST_VERSION` 传自身版本，SDK 版本取**运行环境实际导入**的 `maibot-plugin-sdk` 版本。
- 超范围后果**不对称**：
  - Host 高于 max 但主次相同 → warning，照常加载；其余超范围 → **error，阻止加载**。
  - SDK 超范围 → **只 warning，照常加载**（安装/更新/Runner 连接都不因 SDK 区间被阻塞）。
- 另有一道独立检查：握手 `runner.hello` 时 Host 校验 Runner 自身 SDK 版本落在固定区间 `[1.0.0, 2.99.99]`，与 manifest 声明无关。
- `[debug] force_plugin_compatibility` 可跳过两个区间（需重启，经 `MAIBOT_FORCE_PLUGIN_COMPATIBILITY` 传给 Runner）。
- 校验器是 **Pydantic 严格模式**：多余字段直接 `extra fields not permitted`。

### 2.4 capabilities 名称清单（实证）

文档只说「能力名是逐项的，Host 按名字精确比对；完整清单见 API 参考各能力组」，**但 API 参考页并未逐条列出全部能力名**。以下清单从 SDK 2.21.0 的 `call_capability(...)` 字面量提取，与文档示例（`send.text`/`send.emoji`/`config.get`/`emoji.get_random`/`chat.get_avatar`/`webui.claim_upload`）完全一致：

```
api.call api.get api.list api.replace_dynamic
chat.get_all_streams chat.get_avatar chat.get_group_streams chat.get_private_streams
chat.get_stream_by_group_id chat.get_stream_by_user_id chat.open_session
component.disable component.enable component.get_all_plugins component.get_plugin_config_schema
component.get_plugin_info component.list_loaded_plugins component.list_registered_plugins
component.load_plugin component.reload_plugin component.unload_plugin
config.get config.get_all config.get_plugin
database.count database.delete database.get database.query database.save
desktop_pet.animation_status desktop_pet.get_skin desktop_pet.hide desktop_pet.list_animations
desktop_pet.play_animation desktop_pet.play_event desktop_pet.refresh_buttons desktop_pet.say
desktop_pet.set_thinking desktop_pet.show desktop_pet.status desktop_pet.stop_animation
emoji.delete emoji.get_all emoji.get_by_description emoji.get_count emoji.get_emotions
emoji.get_info emoji.get_random emoji.register
files.download files.list files.read_text files.send files.upload
frequency.get_adjust frequency.get_current_talk_value frequency.set_adjust
internal_chat.identity internal_chat.send
knowledge.search
llm.embed llm.generate llm.generate_with_tools llm.get_available_models llm.transcribe_audio
maisaka.context.append maisaka.context.resolve_image maisaka.proactive.trigger maisaka.reply.generate
message.build_readable message.count_new message.get_by_id message.get_by_time
message.get_by_time_in_chat message.get_recent
person.get_id person.get_id_by_name person.get_value
personality.get_current personality.list personality.switch
send.command send.custom send.emoji send.forward send.hybrid send.image send.text
statistics.local.message_trend statistics.local.model_trend statistics.local.models
statistics.local.online_time_trend statistics.local.token_distribution
statistics.local.token_trend statistics.local.tool_trend
tool.get_definitions
webui.claim_upload
web.search web.fetch                       # web.py 里是独立别名调用
market.search market.get_detail market.get_readme
host.route_message host.update_message_gateway_state   # ctx.gateway 底层用这对名字
```

你点名的 8 个：`llm.generate` ✅、`message.get_recent` ✅、`chat.get_stream_by_user_id` ✅、`chat.open_session` ✅、`maisaka.proactive.trigger` ✅、`tool.get_definitions` ✅、`send.text` ✅、`webui.claim_upload` ✅ —— **全部真实存在**。

注意：`ctx.gateway.*` 底层能力名是 `host.route_message` / `host.update_message_gateway_state`（不是 `gateway.*`）；`ctx.render.html2png` 走的不是字面量能力名。若只用 `ctx.gateway` / `ctx.render` 的高层方法，manifest 里该声明哪个名字【⚠探针】。

### 2.5 i18n 声明

```json
"i18n": {"default_locale": "zh-CN", "locales_path": "i18n", "supported_locales": ["zh-CN","en-US"]}
```
- `default_locale` 必填；`supported_locales` 非空时 `default_locale` 必须在其中；不可含空值/重复项。
- 目录约定：`i18n/zh-CN.json`、`i18n/en-US.json`【文档 https://docs.maibot.chat/plugin/】。

### 2.6 本节「文档未覆盖、必须运行时探针」清单

1. Host 侧 capabilities 白名单的**完整**集合（是否包含 `host.*`、`market.*`、`web.*`，是否还有文档未列项）。
2. 未声明能力时的报错文本/错误码（文档只说「会被拒绝并报未获授权能力」）。
3. `capabilities` 与实际 `required_capabilities`（webui.json）之间是否有一致性校验。
4. `plugin_type` 各值是否影响 WebUI 展示（文档：Host 不校验枚举）。
5. SDK 超区间仅 warning 的实测确认（换版本后看日志）。

---

## 3. 配置（PluginConfigBase / Field）

来源：https://docs.maibot.chat/plugin/config

### 3.1 用法

```python
from maibot_sdk import MaiBotPlugin, PluginConfigBase, Field

class Section(PluginConfigBase):
    __ui_label__ = "基础设置"      # WebUI 分组标题
    __ui_icon__  = "settings"     # Material Icons 名称
    __ui_order__ = 0              # 越小越靠前
    enabled: bool = Field(default=True, description="是否启用")
    targets: list[str] = Field(default_factory=list, description="目标")
    mode: Literal["a", "b"] = Field(default="a", description="模式")  # 见下

class Cfg(PluginConfigBase):
    __ui_label__ = "插件配置"
    plugin: Section = Field(default_factory=Section)   # 嵌套 = 配置分节
```
- `Field` 就是 pydantic 的 `Field`（SDK 从 `.config` 导出）：`default`、`default_factory`、`description`（WebUI 标签）、`json_schema_extra`。
- `json_schema_extra` 常用键：`placeholder`、`group`；【SDK】另支持 `x-widget` 指定控件类型（`config.py` 的 `ui_type = json_extra.get("x-widget")`）。
- 可变默认值（`list`/`dict`/嵌套模型）**必须** `default_factory=`，写 `default=[]` 会被当共享默认值。
- **枚举**：文档没提 `Literal`/`Enum`；【SDK】`config.py` 有 `_extract_literal_choices()`，`Literal[...]` 会生成为 WebUI `choices` → 用 `Literal` 是安全且可用下拉框的写法。`Enum` 未见专门处理【⚠探针】。
- **i18n**：文档只提 manifest 的 `i18n`；【SDK】`PluginConfigBase` 还有 `__ui_i18n__: ClassVar[dict[str, dict[str, str]]]`（配置分节的多语言文本）——文档未写，可用但保守处理。

### 3.2 生成位置与可读性

- `config.toml` 由 **Runner 生成并维护**，位置是**插件安装目录**（`plugins/<插件名>/config.toml`）；仓库 `.gitignore` 应含 `/config.toml`。
- 首次加载生成、缺字段用模型默认值补齐、`config_version` 由 Runner 保留（不要手改）。
- 插件**能读**：`self.config`（强类型）、`self.get_plugin_config_data()`（原始 dict，无需 config_model）、`await ctx.config.get_all()` / `get_plugin()`（Runner 已加载的运行时配置）、`await ctx.config.get("key", default)`（**Host 全局 bot_config.toml**，与插件 config.toml 无关）。
- `self.config` 在未声明 `config_model` 或注入前调用会抛 `RuntimeError`。
- 插件**不应该写** config.toml（文档无写接口；WebUI / 运行时配置接口负责写入）【⚠探针：是否有写回 API】。

### 3.3 热更新

- `config.toml` 变更 → Runner 自动调 `on_config_update(scope="self", config_data, version)`；此时 `self.config` **已更新**。
- `scope` 取值：`"self"`（始终触发，无需订阅）/ `"bot"` / `"model"`（后两者需 `config_reload_subscriptions = ("bot","model")`，ClassVar，可迭代；写字符串抛 `TypeError`，写 `"self"` 抛 `ValueError`）。
- 热重载在后台**合并下发**：短时间连续保存可能只回调一次，内容是该 scope 的完整最新快照；保存请求不等待广播。
- Schema 只在**插件加载时**生成 → 改了 `config_model` 要重载插件才在 WebUI 看到新字段。

### 3.4 本节「文档未覆盖、必须运行时探针」清单

1. `Literal` 之外（`Enum`、`Annotated`、嵌套 `list[PluginConfigBase]`）在 WebUI 的渲染结果。
2. `__ui_i18n__` 的确切结构与生效范围（SDK 有字段、文档无说明）。
3. `config.toml` 写回/程序化保存是否有官方 API。
4. `config_data` 在 `scope="bot"/"model"` 时的完整键集合。
5. `x-widget` 支持的全部取值。

---

## 4. WebUI 页面（`webui.json`，schema_version 2）

来源：https://docs.maibot.chat/plugin/webui-pages

### 4.1 顶层与限制

```json
{
  "schema_version": 2,
  "workspace_title": "统计",                     // placement=workspace 时必填
  "required_capabilities": ["custom_pages", "structured_arguments", "image_upload", "declarative_basic"],
  "pages": [ /* ... */ ]
}
```
- `schema_version` 必须是 `2`；v1 明确拒绝、不自动转换。
- `required_capabilities` 可选值：`declarative_basic`、`custom_pages`、`image_upload`、`structured_arguments`。**用 HTML 页必须 `custom_pages`；用数组/对象参数必须 `structured_arguments`；上传图片必须 `image_upload`。** 你写的 `["custom_pages","structured_arguments"]` 是合法组合 ✅。
- `placement`：`"sidebar"`（麦麦工作区的插件扩展分组）/ `"workspace"`（插件自己的顶部工作区，**必须** `workspace_title`）。同插件 workspace 页面会出现在其侧边栏。
- 图标仅 `puzzle|chart|settings|database|list`；宿主生成路由 `/extensions/{plugin_id}/{page_id}`，插件不能覆盖内置路由。
- 限额：**每插件最多 20 页；声明文件最多 128 KiB**；未知字段/无效引用/类型错误直接拒绝；每页最多 200 组件、8 层深度、每容器 ≤50 子节点。
- 配置无效时仍可在宿主配置页修配置，但**不注册业务页面、不启动业务组件**。
- 完整 JSON Schema：`WebUIExtension.model_json_schema()`。
- 声明改动要**重载插件**才生效；`webui.json` 由 Runner 在线程中读、Host 独立校验并保存快照；卸载后接口立即下线。

### 4.2 页面结构与 API 绑定

```json
"pages": [{
  "id": "studio", "title": "工作台",
  "mode": "custom",                      // declarative | custom
  "entry": "webui/index.html",           // mode=custom 必填，插件内相对 .html
  "layout": "document",                  // document(默认) | viewport
  "queries": {"summary": {"api": "summary", "version": "1"}},
  "actions": {
    "save": {
      "api": "save_limit", "version": "1",
      "parameters": {"limit": {"type": "integer", "required": true, "minimum": 1, "maximum": 1000}},
      "confirmation": "确认保存？"
    }
  }
}]
```
绑定规则：
- `queries`/`actions` 的键是**本页别名**，值是 `{api, version, parameters?, confirmation?, arguments?}`。
- API 必须：**属于当前插件**、`@API` 静态声明（**动态 API 不能绑定**）、`version` **精确匹配**（默认 `"1"`）、处于启用状态。`public=True` 不是必要条件（`public` 只管跨插件调用）。
- `confirmation` 只用于 action，宿主弹确认、后端要求 `confirmed`；query 必须只读、不接受 confirmation。
- `arguments` 可把已声明参数绑定到 query/selection 引用，只投影标量；query 不接受 `arguments`。

### 4.3 API 组件（插件侧如何声明）

来源：https://docs.maibot.chat/plugin/api-components
```python
@API(name: str, description: str = "", version: str = "1", public: bool = False, **metadata)
async def handler(self, limit: int, **kwargs) -> dict: ...
```
- 静态 `@API` 自动同步；动态 `register_dynamic_api()` 需 `await sync_dynamic_apis()`，且**不能**被 WebUI 页面绑定。
- 例：`@API("receive_image", version="1")` 配合上传（见 4.6）。

### 4.4 参数声明与校验

- 标量：`string`、`integer`、`number`、`boolean`；可选 `max_length`（默认 4000，上限 65536）、`minimum`、`maximum`、`choices`；**默认全部可选**，`required: true` 才必填；缺失必填参数或未知参数都报错；**不做类型转换**，`null ≠ 未传`。
- 结构化（**仅 HTML 页**）：`array`（必填 `items`，`max_items` 默认/最多 1000）、`object`（`properties` + 各自 `required`，拒绝未知属性，不开放任意对象透传）。
```json
{"type":"object","required":true,"properties":{
  "ids":{"type":"array","required":true,"max_items":100,"items":{"type":"integer"}},
  "enabled":{"type":"boolean","required":true}}}
```
- 每绑定最多 **30 个参数**；请求总量 ≤ **64 KiB**、嵌套深度 ≤12、元素总数 ≤20000；非有限数字/文件对象/类实例不支持；大小按 UTF-8 计。
- 预置页（declarative）**只接受标量参数**。

### 4.5 返回值 / 长任务 / 分页 / 512 KiB（官方说法）

- API 返回值走同一套 JSON 规则，**总量最多 512 KiB**；HTTP 成功响应为 `{"success": true, "result": <插件返回值>}`；`maibot.action/query` 直接返回 `result`。
- **宿主不解释业务返回值**：`result.success` / `message` / `navigate_page` 都是普通数据，不触发宿主报错/提示/跳转。要表示失败就让 API **抛异常**。
- **长任务**：每插件最多 **2 个进行中的 WebUI API 调用**，单次预算 **10 s**。原文：「长任务应启动后台工作后返回任务 ID，再由 query 查询。」宿主不自动重试写操作；取消/超时不代表已提交操作回滚。
- 分页：预置页有 `pagination` 组件 `{page, pages, total}`（从 1 开始，改同名表单参数）；`table` 组件**每页 50 行**；`chart` 最多 2000 行；`progress` 0–100；`image` data URL ≤24000 字符。
- 刷新：`auto_refresh: true` 表单变化后查询；`poll_interval_seconds` 3–60 秒（默认 0 关闭）；query 顺序执行、全部成功才替换快照；写操作会取消旧查询；离页停止轮询。
- 每页最多 2 个桥接请求，突发 20 条、持续 2 条/秒；握手 10 s、确认 45 s、SDK 查询/操作 60 s、上传 150 s。

### 4.6 HTML 页与上传

- `entry`：插件内相对 `.html`（UTF-8 单文件），禁越界/符号链接/junction；**每页 ≤2 MiB，全部 HTML 合计 ≤8 MiB，注册元数据另限 512 KiB**。
- HTML 页不支持 `content` / `auto_refresh` / `poll_interval_seconds` / 组件式 `arguments`；交互自己写。
- 宿主注入 `window.maibot`：`ready`(Promise)、`query(alias,args?)`、`action(alias,args?)`、`upload(alias,file,args?,options?)`（`signal`/`onProgress`）、`navigate(pageId,state?)`、`page{id,layout,state}`、`context{theme,language,dashboardStyle,styles}`。
- 沙箱：`sandbox="allow-scripts"`、不透明来源、宿主前置 CSP（禁网络/CDN/相对资源/eval/worker/iframe）。
- 上传：`uploads: {"library": {"action":"add","image_max_edge":4000}}`；仅 JPEG/PNG/WebP、≤20 MiB & 4000 万像素（声明 `image_max_edge` 时浏览器侧可收 64 MiB/1 亿像素再缩放）；`upload_id` 64 字符、1 小时过期、只能领取一次；插件 `manifest.capabilities` 声明 `webui.claim_upload` + SDK ≥2.11.0 才能 `ctx.webui.claim_upload(upload_id)` → 领取到 `data_dir/uploads`，返回 `{path, sha256, upload_id}`。并发：单插件 2 / 全局 4，超额 429；未领取暂存：单插件 8 / 全局 32。
- 路径：`POST /api/webui/plugins/runtime/webui/{plugin}/{page}/uploads/{alias}`，字段 `file`、`args`(JSON)、`confirmed`。

### 4.7 本节「文档未覆盖、必须运行时探针」清单

1. `WebUIExtension.model_json_schema()` 的实际字段全集（文档只给了示例键）。
2. `maibot.query/action` 的参数序列化细节（`integer` 与 JS number 的边界、超大整数）。
3. API 抛异常时前端拿到的 `error` 结构（code/message 字段名）。
4. `required_capabilities` 与宿主 dashboard 版本的具体对应关系（不匹配时的表现）。
5. 超过 512 KiB 时的错误形态。
6. `webui.json` 是否支持按语言/主题的额外字段。

---

## 5. 生命周期与热重载

来源：https://docs.maibot.chat/plugin/lifecycle

### 5.1 调用顺序与约束

```python
def create_plugin():        # 顶层工厂函数，必需
    return MyPlugin()

class MyPlugin(MaiBotPlugin):
    async def on_load(self) -> None: ...
    async def on_unload(self) -> None: ...
    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None: ...
```
- Runner：导入 `plugin.py` → `create_plugin()` → 注入 `PluginContext`（`self.ctx` 可用）→ **完成 capability bootstrap 之后**才调 `on_load()`。
- **三个方法必须都被插件类覆写**，否则 Runner 拒绝加载（继承不写也算没实现）。
- `on_config_update` 固定按 `(scope, config_data, version)` **三个位置参数**调用，签名不符 → TypeError。
- `on_unload()` 中 `self.ctx` 仍可用，但必须快速完成、不做耗时操作。

### 5.2 后台任务取消要求

- 文档**没有**单独的「必须取消后台任务」条款；只要求 `on_unload()` 释放插件持有的所有资源（连接、文件句柄、网关离线、注销动态 API、保存数据）。
- 【文档】热重载/卸载会移除 WebUI iframe、关闭桥接、取消浏览器请求；「重载页面」不会中断后台任务。
- 【⚠探针/工程约定】`asyncio.create_task` 的取消、`on_unload` 的超时预算、Runner 是否强杀进程——文档未写；建议自行 `try/finally` + 保存 task 引用并在 `on_unload` 里 cancel + `await asyncio.gather(..., return_exceptions=True)`。

### 5.3 `ctx.paths`

```python
self.ctx.paths.data_dir      # 持久化：data/plugins/<plugin_id>/（默认）
self.ctx.paths.runtime_dir   # 临时：temp/plugins/<plugin_id>/（默认）
```
- SDK 2.6.0+ 提供；`runtime_dir` **不承诺长期保留**，插件需能在目录被清理后自建。
- 安全要求：不要用用户输入直接做文件名；拒绝绝对路径与 `..`；写入限定在两个目录内；不要再使用旧的 `plugins/<插件>/data`。

### 5.4 其它运行边界

- 插件跑在独立 Runner 子进程，msgpack RPC；**单次 RPC 默认 30 s 超时，组件调用默认 60 s**。
- 不能 `import src.*`，只能用 `maibot_sdk`；能力按 manifest 声明。
- **无沙箱**：无文件/网络隔离，等同主程序权限。
- 插件源码或 `config.toml` 变化会自动重载，**重载失败回滚到上一版本**。

### 5.5 本节「文档未覆盖、必须运行时探针」清单

1. `on_unload` 的硬超时与超时后的处理（强杀 / 放弃）。
2. `data_dir`/`runtime_dir` 是否保证在 `on_load` 前已创建（文档说默认路径，未说 mkdir）。
3. 热重载时 Runner 是否复用进程（影响模块级状态与全局连接）。
4. 后台任务的取消是否由 Runner 代劳。
5. `on_load` 抛异常时的表现与重试策略。

---

## 6. 能力 API 官方签名

来源：https://docs.maibot.chat/plugin/api-reference （签名以【SDK】`.sdk/pkg/maibot_sdk/capabilities/*.py` 校准）

### 6.1 `ctx.llm.generate` — 能力 `llm.generate`

```python
async def generate(
    prompt: str | list[dict],          # 字符串或 [{"role","content"}, ...]
    model: str = "",                    # 模型名或任务名；"" = 默认任务
    temperature: float | None = None,   # None = 用 Host 配置
    max_tokens: int | None = None,
    *, task_name: str | None = None, model_name: str = "", **kwargs,
) -> dict
```
返回【文档】：
```python
{"success": True, "response": str, "reasoning": str, "model": str, "model_name": str}
```
- SDK 始终补齐 `model`；Host 返回旧 `model_name` 时自动兼容。
- 同组：`generate_with_tools(prompt, tools, model="", temperature=None, max_tokens=None)`（结果另含 `tool_calls`）、`embed(text=…|texts=…, task_name=…, max_concurrent=…)`、`transcribe_audio(audio=…|audio_base64=…)`（走 `voice` 任务，返回 `{success, text}`）、`get_available_models() -> list[str]`。

### 6.2 `ctx.message.get_recent` — 能力 `message.get_recent`

```python
async def get_recent(self, chat_id: str, limit: int = 10) -> list[dict]   # 直接返回消息列表
```
同组：`get_by_id(message_id, chat_id="", stream_id="")`、`build_readable(messages, **kw)`（`replace_bot_name=True`、`timestamp_mode="relative"`、`truncate=False`；也可传 `messages=None, chat_id, start_time, end_time` 让 Host 查）、`get_by_time(start,end)`、`get_by_time_in_chat(chat_id,start,end)`、`count_new(chat_id, since)`（since 为 UNIX 秒字符串）。

### 6.3 `ctx.chat` — `chat.get_stream_by_user_id` / `chat.open_session` …

```python
async def get_stream_by_user_id(self, user_id: str, platform: str = "qq") -> Any     # 私聊流
async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> Any
async def get_all_streams(self, platform: str = "qq") -> Any
async def get_group_streams(self, platform: str = "qq") -> Any
async def get_private_streams(self, platform: str = "qq") -> Any
async def open_session(self, platform: str = "qq", chat_type: str = "private", *,
                       user_id: str = "", group_id: str = "", account_id: str = "", scope: str = "",
                       **kwargs) -> Any
async def get_avatar(self, platform, target_id, target_type="user", account_id="", scope="",
                     force_refresh=False) -> {"status","url","expires_at"}
```
- `open_session` 返回 `stream_id`、`session_id`、`chat_type`、`created` 及完整 `stream` 对象；多账号/多路由建议同时传 `account_id` + `scope`。
- `get_avatar` 需 manifest 能力 `chat.get_avatar`；`status ∈ available|unsupported|missing`。

### 6.4 `ctx.maisaka.proactive.trigger` — 能力 `maisaka.proactive.trigger`

```python
async def trigger(self, stream_id: str, intent: str, *, reason: str = "",
                  priority: str = "", metadata: dict | None = None, **kwargs) -> Any
```
- 把 `intent` 写入 Maisaka 内部上下文并唤醒 Planner；**目标聊天流必须已存在**（否则报错）。
- 同组：`maisaka.context.append(stream_id, segments, visible_text=…, source_kind=…)`、`maisaka.context.resolve_image(stream_id, source_id, index=0)`（返回 `binary_data_base64` / `binary_hash`；只读现有上下文，超 RPC 单帧上限会报错）、`maisaka.reply.generate(stream_id, reference, *, format_instruction="", model_name="", timeout_ms=None)`。

### 6.5 `ctx.tool.get_definitions` — 能力 `tool.get_definitions`

```python
async def get_definitions(self) -> list          # 元素含 name 与 definition；SDK 已解包
```

### 6.6 `ctx.send.text` — 能力 `send.text`

```python
async def text(self, text: str, stream_id: str, *, return_details: bool = False, **kwargs) -> bool | dict
```
- 默认返回 `bool`；`return_details=True` 返回 `{"success": bool, "sent": bool, "message_id": str | None}`（`message_id` 为平台侧 `platform_message_id`）。
- 同组：`image(image_data, stream_id, ...)`（base64）、`emoji`、`command`、`forward`、`hybrid(segments, stream_id)`（`[{"type":"text","content":...}]`）、`custom(custom_type, data, stream_id)`。`return_details` 对 7 个方法都生效。

### 6.7 `ctx.webui.claim_upload` — 能力 `webui.claim_upload`

```python
async def claim_upload(self, upload_id: str) -> dict   # {path, sha256, upload_id}
```
需 SDK ≥2.11.0 + Host 支持 `image_upload` + manifest `webui.claim_upload`；凭证一小时过期、只能领取一次、所属插件必须匹配；文件落 `data_dir/uploads`。

### 6.8 本节「文档未覆盖、必须运行时探针」清单

1. 上述方法失败时的错误形态：文档说「插件组件的异常以 `success: false` 写在返回载荷里」（https://docs.maibot.chat/develop/plugin），但各能力具体错误码未列。
2. `llm.generate` 的 `model` 与 `task_name` 同时给出时的优先级细节（SDK 注释说 Host 以 `task_name` 存在与否判定新旧协议）。
3. `message.get_recent` 返回消息字典的字段（与第 1 节 MessageDict 是否同构）。
4. `tool.get_definitions` 的 `definition` 具体 schema。
5. `maisaka.proactive.trigger` 是否受 `maisaka` 全局开关/频率限制影响，以及返回结构。
6. 各能力 RPC 实际超时（默认 30 s）与长任务替代方案（无官方异步任务 API）。

---

## 附：对本项目的 5 条硬约束结论

1. `id` = `com.calarence.persona` 合法；`manifest_version: 2`；`urls.repository` 必填。
2. `sdk` 区间建议写 `{"min_version": "2.10.0", "max_version": "2.21.99"}`（实测到的最高主次 + .99）；SDK 超区间只 warning，Host 超区间才阻止加载。
3. `capabilities` 必须逐项声明（如 `["llm.generate","message.get_recent","chat.get_stream_by_user_id","chat.open_session","maisaka.proactive.trigger","send.text","tool.get_definitions"]`）；文档说未声明即拒绝。
4. 不要订阅 `emoji.register.after_build_emotion`、`jargon.query.before_search`、`jargon.query.after_search`（未注册 → 插件注册失败）。
5. `send_service.after_send` 只能观察（不可 abort / 不可改参，声明 `error_policy=ABORT` 会注册失败）。
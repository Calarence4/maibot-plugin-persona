# P0 探针清单（P1 前硬门槛）

目的：在真实 Host 上确认 **Hook 载荷、Items 结构、发送关联、隐私边界、崩溃恢复** 与本实现的假设一致。
在全部通过前，`_manifest.json` 的 `host_application` / `sdk` 区间不要扩大，也不要在生产群开启记忆注入。

> 本仓库已做：SDK 2.21.0 导入/组件注册/配置 Schema/加载卸载冒烟；47 个单元测试（含模拟崩溃重放与杀进程语义的等价测试）。
> 本仓库未做：真实 Host 上的 Hook 载荷与 items 兼容性验证。下列步骤必须在部署环境执行。

## 0. 版本与注册表

1. 记录 Host 版本、Runner SDK 版本、Python 版本：

   ```bash
   /persona selfcheck
   grep -m1 "MaiCore 当前版本" logs/app_*.log*
   ```

2. 读取 Host 实际 Hook 中心表（WebUI 后端路由，需登录态）：

   ```text
   GET /plugins/runtime/hooks
   ```

   确认以下 5 个 Hook 存在，且允许的模式与声明一致：

   | Hook | 本插件 mode | allow_abort | allow_modify |
   | --- | --- | --- | --- |
   | `chat.receive.after_process` | OBSERVE / LOG | 是 | 是 |
   | `maisaka.replyer.before_request` | BLOCKING / SKIP | 是 | 是 |
   | `maisaka.replyer.before_model_request` | BLOCKING / SKIP | 是 | 是 |
   | `maisaka.reply.before_post_process` | BLOCKING / SKIP | 是 | 是 |
   | `send_service.after_send` | OBSERVE / LOG | **否** | **否** |

   若 `after_send` 被声明成 BLOCKING 或 `ABORT`，Host 会在注册阶段直接拒绝本插件。当前实现已按 OBSERVE 注册。

3. 确认插件组件注册成功（日志/WebUI）：5 Hook + 1 Command + 15 API。

## 1. 打开载荷探针

```toml
[diagnostics]
hook_payload_dump = true
max_files = 40
include_text = false
```

热更新或重载插件。探针写到：

```text
<ctx.paths.runtime_dir>/probes/<时间>-<hook>-<随机>.json
```

每份包含 `hook`、`at`、`keys` 与压缩后的 `shape`。`shape` 对 `item_type/type/role/status/kind/chat_type/platform/scope` 保留原值，其余字符串只记录长度；默认不含正文。

## 2. TurnContext 与 Items

### 场景 A：主人私聊

1. 主人私聊发送：`今天有点难过，想抱抱你`。
2. 等待 replyer 生成一次普通回复。
3. 收集三份探针：`chat.receive.after_process`、`maisaka.replyer.before_request`、`maisaka.replyer.before_model_request`、`send_service.after_send`。
4. 用 `/persona stats` 确认记录 +1。

检查项：

- [ ] `after_process` 的 `message` 含 `session_id`、`message_id`、`timestamp`、`message_info.user_info.user_id`、`raw_message`；群消息还含 `message_info.group_info.group_id`。
- [ ] `before_request` 的 `session_id` 与入站 `message.session_id` 一致；存在 `reply_tool_args`（可为空 dict）。
- [ ] `before_model_request` 的 `items` 是数组；插入后数组长度 = 原长度 + 1。
- [ ] 插入位置在 `SystemMessageItem` 之后（索引 = 第一个 system 的下标 + 1）。
- [ ] 插入项 `item_type == "UserMessageItem"`，`meta` 有 `item_id`、`logical_turn_id`（null）、`timestamp`，`parts[0].type == "text"`。
- [ ] 插入项文本以 `<persona_data nonce="…">` 开头、`</persona_data>` 结尾；模型请求未回写聊天历史。
- [ ] `after_send` 的 `sent == true` 时，`reply_message_id` 能命中入站 `message_id`；`message.session_id`、`message.message_id`、`processed_plain_text` 或 `raw_message` 存在。
- [ ] `/persona stats` 的记录 `botReplies` 增加一条，`revision` 从 1 变为 2。

**若 items 不是 dict 数组**：先把 `before_model_request` 改回只观察（不返回 `modified_kwargs`），记录真实结构后再适配 `PersonaService._synthetic_user_item`。不要在生产群强行开启。

**若 `reply_tool_args` 未向下传播**：`persona_turn_id` 只用于本插件内部关联，不影响注入；`before_model_request` 仍以 `session_id` 定位会话。

### 场景 B：普通私聊

用另一个 QQ 私聊发送 `我今天好累`。检查：

- [ ] 记录 `scope=private`。
- [ ] 主人私聊的 prompt 不包含这条正文；该私聊自己的 prompt 包含。
- [ ] 任何群里都不出现这条正文（`preview_prompt` 选群 conversationKey）。

### 场景 C/D：群 A 与群 B

1. 主人在群 A 说话：`今天开会好烦`（记录 `scope=group`）。
2. 普通成员在群 A 直接 @bot：`她在吗`（记录 `scope=group`）。
3. 在群 B 发送无关消息：`哈哈哈哈`（可能因强情绪被采集，属正常）。
4. 分别在群 A、群 B 用 `preview_prompt` 预览。

检查：

- [ ] 群 A 的记忆不出现在群 B。
- [ ] 主人私聊/普通私聊的记忆不出现在任何群。
- [ ] 群渲染里 QQ 号、群号、链接被替换为 `[数字ID]` / `[链接]`。

### 场景 E：public 空授权与撤销

1. 配置 `privacy.public_groups = ["qq:888:*:group:30001"]`。
2. 对一条记录调用 `preview_public`（targets 为该群），确认预览已跨域脱敏。
3. `grant_public(confirm=true)` 后预览群 A：可见；预览群 B：不可见。
4. `revoke_public` 后预览群 A：立即不可见；`privacyRevision` +1。

检查：

- [ ] 未授权时 `allowedConversationKeys` 为空，不隐式改为 owner，也不向所有群开放。
- [ ] 授权目标必须同时出现在配置与记录中。
- [ ] 撤销后旧 prompt 全部失效。

### 隐私快照建议扩到 20 组

方案 §12.1 要求 20 组诱导与请求快照。建议至少覆盖：主人私聊、普通私聊、群 A/B、主人在群、当事人在另一个群、跨账号同 QQ、public 空授权、public 授权后撤销、擦除后旧缓存、群内 @bot、转发消息中的隐私、图片说明文本。每组保存最终请求体（或探针 + `preview_prompt` 输出），不要只看回答。

## 3. 发送关联与边界

1. 一条回复被拆成多段发送：`botReplies` 只包含 `sent=true` 的可见分段，顺序稳定。
2. replyer Hook 被其它插件 abort：本插件只记录 candidate、不声称模型已看过；`/persona stats` 的 `candidate-exposure.json` 可增，但 `cardRevision` 不变。
3. 分段部分失败：失败段不写入 `botReplies`。
4. 无 `reply_message_id` 的发送：不拼到“最近一条主人记忆”；超过 TTL 后写独立 `qq-self`。
5. 普通回复与 P4 主动消息：当前版本未实现 P4；若后续启用，必须按 stream/时间/关联键过滤，`after_send` 无证据时记 unknown。

## 4. 崩溃恢复与跨日重放

在测试实例（非生产数据）上执行：

1. **full append 后 kill**：发送消息，等到 `full.jsonl` 增长后 `kill -9` Host；重启，检查 `full` 同修订不重复、`card` 最终包含该记录。
2. **stats/checkpoint 前 kill**：观察 `full-index.json` 大小与 `full.jsonl` 不一致后 kill；重启应触发索引重建，`archiveKey` 不重复。
3. **card/checkpoint 前 kill**：同上，重启后按 offset 重放。
4. **未提交分片跨多日**：把系统时间或目录日期改到 2 天前产生分片，保持 `checkpoint` 未提交，重启后应枚举**所有**未提交分片（不是只看昨天+今天）。
5. **半行**：手工在活动分片末尾追加半行 JSON，不应阻塞下一条完整事件；封存分片的半行会被跳过。

单元测试已覆盖等价路径：`tests/test_service.py::test_crash_replay_does_not_duplicate_full_revision`。

## 5. A_Memorix 与工具面

- [ ] `[a_memorix.plugin] enabled=false` 生效，日志无 A_Memorix 初始化。
- [ ] `tool.get_definitions` 返回的工具里没有 `query_memory` / `query_person_profile` 等记忆工具（若仍存在，说明 A_Memorix 未关闭或另有插件调用）。
- [ ] 本插件没有声明 `tool.get_definitions` 能力，也不会调用它。

## 6. 资源基线

在测试群 + 主人私聊稳定运行 30 分钟后记录：

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 插件 RSS 增量 | ≤120 MiB（p50/p95/峰值） | 待填 |
| 热路径本地耗时 | ≤30 ms p95 | 本机无 Host 基线：prompt 渲染 p50≈11 ms（600 条），compose≈0.07 ms |
| 维护 tick | ≤500 ms p95（600 条） | 本机：card 渲染≈9 ms、会话 prompt≈11 ms |
| 总磁盘 | ≤1.2 GiB | 待填 |
| LLM | 总量 ≤50/日 | 默认关闭二级识别；启用后看 `llm-ledger.json` |

`/persona stats` 的 `writer`、`lastTickAt`、`storage` 可作为运行期证据；tick 的 `elapsedMs` 目前只在日志/返回值中，未写入磁盘。

## 7. 通过后要做什么

1. 把 `_manifest.json` 的 `host_application` / `sdk` 区间改成**实测通过**的版本。
2. 在 README 的“状态”表把 P0 标为 ✅，附 Host/SDK/Python 版本与探针文件清单。
3. 若 items 结构、`after_send` 字段或 Hook 名称与本文假设不同，先改代码与测试，再重跑 P0。
4. 保留探针文件作为发布证据（已脱敏、无正文）。

## 8. 探针文件清理

```bash
rm -f "$DATA_DIR/../temp/plugins/com.calarence.persona/probes/"*.json
```

或把 `diagnostics.hook_payload_dump=false` 后重载；`runtime_dir` 整体删除也会重建。
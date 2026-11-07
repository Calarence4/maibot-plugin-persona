# 人格运行时（MaiBot 插件）

插件 ID：`com.calarence.persona` · 版本：`0.1.0`

按《方案.md》（V3.0-final）实现：**叙事型记忆卡 + 全量存档卡 + 渐进披露风格**。纯文件状态，零向量、零 Embedding、零本地模型；可选 LLM 只用于情绪/风格增强，不参与记忆评分。

> 这是可安装的插件工程，不是只有设计说明。核心记忆/隐私链路有 47 个单元测试；`maibot-plugin-sdk 2.21.0` 下已完成导入、组件注册、配置 Schema、加载/卸载冒烟。**真实 Host 的 P0 探针（Hook 载荷、items 结构、发送关联）仍需在部署环境执行**，见 [docs/P0-PROBE.md](docs/P0-PROBE.md)。

---

## 1. 当前实现范围

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| P0-Core | 🟡 工具已备，待真机 | `self_check` / `/persona selfcheck` / Hook 载荷探针（默认关闭）；本仓库未连真实 Host |
| P1 记忆与隐私 | ✅ 已实现 | 最小采集、首次脱敏、per-conversation prompt、幂等 full/checkpoint、衰减分档、按会话注入、botReplies 关联 |
| P2 治理 | 🟡 后端已实现 | 纠错/忘记/逻辑删除/物理擦除/全量尾部查询/长任务；**自定义 WebUI 页面与 FTS 未包含** |
| P3 风格 | 🟡 规则版已实现 | 目录/索引/rules/hysteresis/L1+L2/预算；**host 模式未实现**（需 P3 探针） |
| P4 动机 | ⛔ 未实现 | 只有配置骨架且默认关闭；不会发消息、不会排程 |

### 已经能做

- 采集主人私聊、普通私聊、直接对 bot 的群消息、带强情绪/事件信号的群消息；命令、纯媒体、无关群聊默认不落盘。
- 第一次写盘前统一脱敏密码、token、密钥、证件号、手机号、银行卡、邮箱。
- `card.json`（当前衰减卡，唯一真相）+ `card.md`（人看版，容量按它计算）+ `full.jsonl`（追加式修订日志）+ `checkpoint.json`（分片 offset）。
- 每日衰减：`vivid → clear → fuzzy → trace → 忘记`，每档实际缩短正文；保护记录（初次相遇、最初、pinned、主人重要）有下限。
- 按 `conversationKey` 渲染独立 prompt/manifest；owner/private 永不进群；group 只进来源群；public 必须同时命中记录授权与 `privacy.public_groups`。
- `maisaka.replyer.before_model_request` 在 system 后插入一条带随机 nonce 的合成 user Item；只影响本次请求，不回写历史。
- 普通回复经 `send_service.after_send` 确认 sent 后才追加为 `botReplies`，并递增 revision 归档。
- 管理 API：纠错、忘记、逻辑删除、物理擦除（长任务 + 轮询）、全量尾部查询、public 授权/撤销、注入预览、状态快照。
- writer generation + `flock` 跨 Runner 互斥；磁盘 reservation ledger 与 95% 停写闸门。

### 明确没有做

- **WebUI 自定义页面**：`webui.json` 未提供；后端 `@API` 已就绪，页面需要按 Host Dashboard 的 `schema_version: 2` 另做并真机验证。
- **FTS5 检索**：全量查询只扫尾部 `full_scan_bytes`（默认 5 MiB）。
- **recall 复习判定**：`recall_reinforcement_enabled` 默认 false，且 v0 不实现“再次提及”检测，`recallCount` 不会增长（符合方案“宁可关闭加成”）。
- **P3 host 模式**：`style.application_mode="host"` 只告警不生效；expression Hook 未注册，避免在未探针时接管 Host 候选。
- **P4 动机**：只保留配置节，没有 drives/goal/preview/回执。
- 图片/表情记忆、人物关系图、merge 合并不实现。

---

## 2. 目录结构

```text
maibot-plugin-persona/
├── _manifest.json          # manifest_version 2
├── plugin.py               # Runner 入口（create_plugin）
├── persona/                # 核心实现（除 config_model/plugin_impl 外只依赖标准库）
│   ├── service.py          # 编排：采集 → writer → 衰减 → 渲染 → 注入 → 回复关联
│   ├── store.py            # 路径、磁盘闸门、card/full/checkpoint/ops/erased/writer
│   ├── render.py           # 分档渲染与按会话 prompt
│   ├── privacy.py          # 隐私矩阵
│   ├── ingest.py           # 最小采集 + 首次脱敏 + inbox append
│   ├── scoring.py          # 重要度、retention、清晰度
│   ├── identity.py         # conversationKey / TurnContext
│   ├── style.py            # 风格目录、rules 选择、L1/L2
│   ├── recognition.py      # 情绪/意图一级规则 + 可选二级 LLM
│   └── ...
├── tests/                  # 47 个 pytest 用例
├── scripts/                # SDK 冒烟、备份屏障合并
└── docs/                   # 部署、P0 探针、验收对照、SDK 契约
```

---

## 3. 安装

要求：Linux（Ubuntu）、Python 3.12+、MaiBot Host 已能加载第三方插件、`maibot-plugin-sdk` 在宿主侧可用。

```bash
cd /root/my-maibot
cp -r /path/to/maibot-plugin-persona plugins/com.calarence.persona
# 目录名可以不同，Host 以 _manifest.json 的 id 为准
```

首次加载后 Runner 会在插件安装目录生成 `config.toml`（不在 `data/` 下）。用 WebUI 配置页或直接编辑：

```toml
[plugin]
config_version = "0.1.0"

[general]
enabled = true
owner_qq = "你的QQ号"        # 必须恰好一个，否则记忆写入/注入禁用
owner_platform = "qq"
owner_account = ""           # 多账号时填写，否则留空
timezone = "Asia/Shanghai"
dry_run = false
bot_names = ["花凪みお"]      # 用于判断群内是否直接对她说

[memory]
max_chars = 60000
max_records = 600
inject_max_chars = 2200
daily_maintain_hour = 4

[privacy]
public_groups = []           # 必须是完整 conversationKey，不能填裸群号

[style]
enabled = false
application_mode = "disabled"
select_mode = "rules"
default_style = "casual"

[diagnostics]
hook_payload_dump = false    # P0 探针时改 true
```

完整默认值见 `persona/config_model.py` 与 [方案 §9](../方案.md)。修改 `owner_qq` 等配置会触发热更新；`privacy.public_groups` 变化会递增 `privacyRevision` 并使全部派生 prompt 失效。

### 与 A_Memorix 的关系

方案要求关闭 A_Memorix，避免两套记忆同时注入。它不是本插件的依赖，也不会被本插件修改：

```toml
[a_memorix.plugin]
enabled = false
```

改完重启 Host。A_Memorix 关闭后其人物画像/Episode/向量检索能力不再可用，这是方案 §1 的显式取舍。

---

## 4. 数据目录

```text
<ctx.paths.data_dir>/
├── memory/
│   ├── card.json / card.md
│   ├── full.jsonl / full-stats.json / full-index.json
│   ├── erased.json
│   ├── inbox/YYYY-MM-DD/<generation>.jsonl
│   ├── inbox/checkpoint.json
│   ├── inbox/dedupe.jsonl
│   ├── operations.jsonl
│   └── backups/{daily,weekly}/<date>/
├── styles/<style_id>/{style.json,prompt.md,reference.md}
└── state/
    ├── session-map.json / recognition.json / style-state.json
    ├── candidate-exposure.json / llm-ledger.json
    ├── writer-state.json / storage-ledger.json
    └── reply-inbox.jsonl

<ctx.paths.runtime_dir>/
├── prompts/<sha256(conversationKey)>/{memory.md,manifest.json}
└── probes/                   # 只在 diagnostics.hook_payload_dump=true 时出现
```

- 新建文件权限尽量为 `0600`；备份不包含 `full.jsonl` 全量副本。
- `runtime_dir` 全是派生物，删除后可重建；缓存 miss 时**不会**回退到一份混合所有会话的全局 prompt。
- 恢复备份时必须先合并 `erased.json` 屏障（见 [docs/DEPLOY.md](docs/DEPLOY.md)），否则可能复活已擦除内容。

---

## 5. 运行观测

部署后主人可在任意会话发送：

```text
/persona stats       # 当前卡、full、磁盘、writer、配置错误
/persona selfcheck   # 版本、路径、待探针清单
/persona probe       # 最近 Hook 探针文件
```

非主人发送会被拒绝。命令只做只读查询，不发送主动消息。

日志关键字：

```text
人格运行时已加载
privacyRevision=
注入 N 字符（memory=… style=…）
跳过采集：…
```

---

## 6. 管理 API

`@API` 组件 version 固定 `1`，供 WebUI 或其它插件调用。返回值统一带 `success`；业务失败返回 `success:false` + `error`，不抛异常。

| API | 说明 |
| --- | --- |
| `stats` | 卡片/全量/磁盘/writer/配置摘要 |
| `config_summary` | 运行期配置与校验错误 |
| `list_memories` | 分页列当前卡，支持 `conversation_key` / `clarity` |
| `get_memory` | 单条记录（含 botReplies/来源事件键） |
| `preview_prompt` | 指定 `conversation_key` 的注入预览 |
| `query_full` | 尾部 5 MiB 全量查询（可 `query` 过滤） |
| `correct_memory` | 纠错，revision+1；放宽 scope 需 `confirm_widen=true` |
| `forget_memory` | 忘记正文，full 保留 |
| `delete_memory` | 逻辑删除，写 erased 屏障 |
| `erase_memory` | 物理擦除，返回 `operation_id` |
| `operation_status` | 查询长任务状态 |
| `preview_public` | public 授权前的跨域脱敏预览 |
| `grant_public` | 授权 public（`confirm=true`，写审计） |
| `revoke_public` | 撤销 public，立即失效 |
| `self_check` | P0 自检与待探针清单 |

**注意**：`grant_public` / `revoke_public` 不校验调用者是否为 WebUI 登录主人；Host 端必须在多人可访问 WebUI 时补 owner 校验（方案 §3.9）。插件内 `correct_memory` 的 scope 放宽已强制二次确认。

---

## 7. 测试与冒烟

```bash
# 建议在独立 venv 中
python -m venv .venv
.venv/Scripts/python -m pip install pydantic pytest maibot-plugin-sdk

cd maibot-plugin-persona
../.venv/Scripts/python -m pytest -q
../.venv/Scripts/python scripts/smoke_sdk.py
```

当前结果：`47 passed`；冒烟输出 21 个组件（15 API + 1 Command + 5 Hook）、10 个配置节、加载/卸载正常。

覆盖：

- 脱敏（手机号/证件号/银行卡/密码/token/私钥/角色标记）
- 重要度、初次相遇、retention、分档衰减、主人重要下限
- 分档渲染长度、跨域脱敏、事件过期、硬预算、隐私矩阵（owner/private/group/public）
- 采集过滤、eventKey 幂等、崩溃重放不重复 full 修订
- items 注入位置与结构、after_send 关联与 revision、物理擦除全副本搜索、public 授权/撤销
- 风格最短停留/冷却、索引与坏文件跳过、配置校验

---

## 8. 已知边界

1. 没有可靠的“再次提及”判定，recall 加成为关闭状态，`recallCount/lastRecalledAt` 不会自动变化。
2. 全量查询只承诺尾部 `full_scan_bytes`；更早记录需要用 grep 或后续 FTS。
3. 当前卡不是检索型记忆；未 pinned、非 initial、未再次提及的旧事可能不再自动想起。`full` 查询不会自动回注。
4. 提示词分隔与角色标记剥离不是语义级 100% 防护；真正的边界是 scope、Host 工具权限、白名单与最小出机数据。
5. `manifest` 的 Host/SDK 区间基于目标服务器实测信息（Host 1.3.19 / SDK 2.21.0），P0 探针通过前不要扩大区间。
6. 未实现 merge 合并；`mergedFrom` 字段已预留，擦除会按 id/来源键过滤，但不会主动展开不存在的血缘。
7. Windows 本地开发缺少 `tzdata` 时，`Asia/Shanghai` 会回退为固定 +08:00；Linux 部署使用 `zoneinfo`。

---

## 9. 文档

- [部署与恢复](docs/DEPLOY.md)
- [P0 探针清单](docs/P0-PROBE.md)
- [验收对照](docs/ACCEPTANCE.md)
- [MaiBot SDK 契约摘要](docs/maibot-sdk-contract.md)
- [变更记录](CHANGELOG.md)

## 10. 许可

MIT。
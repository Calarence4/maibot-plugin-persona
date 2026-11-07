# 验收对照（方案 §12）

图例：✅ 有实现与自动化测试 · 🟡 部分实现 / 仅后端 · ⛔ 未实现 · ⏳ 需真机探针

## 功能与一致性

| # | 验收项 | 状态 | 证据 / 说明 |
| --- | --- | --- | --- |
| 1 | 20 组快照：private/owner 在任何群均无正文；群 A 在群 B 无正文；跨账号不串；当事人/public 撤销不绕过 | 🟡 | 隐私矩阵与渲染测试 `test_render.py`（owner/private/group/public、跨域脱敏）、`test_service.py::test_public_grant_and_revoke`；20 组真机快照需按 [P0-PROBE.md](P0-PROBE.md) 执行 |
| 2 | 候选曝光 100 次 recall 不变；abort 不记实际曝光；一次明确提及最多 +1 | 🟡 | `recall_reinforcement_enabled` 默认 false（`test_scoring.py`）；`candidate-exposure.json` 与 `cardRevision` 解耦；**再次提及检测未实现**，`recallCount` 不会增长（方案允许 v0 关闭） |
| 3 | botReplies 只含明确 sent 的可见分段；abort/失败/无法关联不拼错 | 🟡 | `test_service.py::test_after_send_attaches_bot_reply_and_increments_revision`、`test_after_send_ignores_failed_or_empty`；分段/abort 需真机 P0 |
| 4 | inbox 超阈值/停机 7 天/三个 kill 点后不丢、full 不重复、checkpoint 可续跑 | 🟡 | `test_service.py::test_crash_replay_does_not_duplicate_full_revision`、批量 `max_inbox_batch`、全部分片枚举；真机 kill 点见 P0 §4 |
| 5 | fuzzy/trace 渲染确实更短；非保护记录达到压缩目标；保护溢出告警 | ✅ | `test_render.py::test_each_clarity_actually_shortens_text`、`test_budget_is_hard_and_never_exceeds`；容量压缩写 `stats.capacityWarn` |
| 6 | eventDate 统一状态：day 全天有效；datetime 到 validUntil/宽限后过期；过期默认不注入，pinned 例外 | ✅ | `event_state.py` 单一函数；`test_render.py::test_expired_event_not_auto_injected` |
| 7 | 纠错新 revision 可回滚；物理擦除后卡/full/inbox/operations/备份/FTS/prompt 无正文；旧备份不覆盖屏障 | 🟡 | `test_service.py::test_correction_creates_revision_and_full_query_prefers_latest`、`test_physical_erase_removes_all_copies`、`test_forgotten_record_still_queryable_in_full`；FTS 未实现；备份恢复合并见 `scripts/merge_erased.py`；**回滚 API 未提供**（旧修订在 full 中，可人工恢复） |
| 8 | full 遗忘记录可查但不进 prompt；未启用 FTS 只承诺尾部 5 MiB | ✅ | `test_forgotten_record_still_queryable_in_full`；`query_full` 只扫 `full_scan_bytes` |
| 9 | 连续 10 轮风格切换符合迟滞；host/plugin 不同时注入；新增合法风格目录无需改代码 | 🟡 | `test_style.py`（最短停留/冷却/坏 id 跳过/索引）；host 模式未实现，`application_mode=host` 只告警；"10 轮"可在真机补测 |
| 10 | 记忆评分/衰减/归档零 Embedding、零本地模型、零记忆 LLM | ✅ | 核心模块只依赖标准库；全仓库无 embedding 调用；可选 LLM 仅识别/风格，默认关闭 |

## 非功能

| 项 | 状态 | 说明 |
| --- | --- | --- |
| RSS ≤120 MiB 增量 | ⏳ | 本机 benchmark 只测耗时：600 条卡片渲染 p50≈8.9 ms、会话 prompt≈10.9 ms、注入组装≈0.07 ms（见 `scripts/bench_local.py`） |
| 热路径 ≤30 ms p95 | 🟡 | 缓存命中只读文件 + manifest，未在本机 Host 环境测 p95；miss 时 600 条约 11 ms |
| 维护 tick ≤500 ms p95 | 🟡 | 本机 600 条卡片含压缩/备份/单会话渲染约 156–161 ms；多会话同 tick 渲染会线性增长，真机需测 |
| 磁盘 1.2 GiB 预算 | ✅ | reservation ledger、85%/95% 水位、`test_storage_gate_blocks_new_writes` |
| 两个写者逼近 95% 不超卖 | 🟡 | ledger 原子登记与过期清理已实现；双 Runner 并发需 P0 验证 |
| 满盘擦除分片或 incomplete | 🟡 | 擦除失败返回 `incomplete`，不伪报；分片 compaction 未实现 |
| WebUI 长任务无同步超时、≤512 KiB | 🟡 | 管理 API 均短返回；擦除返回 `operation_id` 轮询；页面未实现 |
| 热重载无双 writer、无重复 tick、无未处理 task | 🟡 | writer flock + generation/fencing；tick 单飞；卸载取消任务并等待在途 tick；真机双 Runner 待验 |
| 日志无明文密钥/完整隐私原文 | ✅ | 首次脱敏后才落盘；探针默认只记结构；日志只记长度/会话短哈希 |

## 方案 §11.1 P0 硬门槛

| 项 | 状态 |
| --- | --- |
| 版本与注册表 | 🟡 本机 SDK 2.21.0 冒烟；Host `/plugins/runtime/hooks` 待真机 |
| TurnContext 与 Items | 🟡 按运行时转储实现；真实 items 结构待探针 |
| 隐私快照 | 🟡 单元级覆盖；20 组真机快照待执行 |
| 发送关联 | 🟡 以 `reply_message_id`/`session_id` 关联；拆分/abort/部分失败待真机 |
| 热重载/崩溃 | 🟡 等价单测；真机 kill 点/双 Runner 待执行 |
| A_Memorix | ⏳ 需部署侧关闭并核对工具面 |
| 资源基线 | ⏳ 本机仅耗时基线 |

## 结论

- 记忆与隐私的**核心算法与文件状态机**已实现并通过 47 个单元测试。
- 距离“方案全部验收”的剩余工作，按优先级：
  1. 真机 P0（items/after_send/隐私快照/kill 点）→ 收敛 manifest 区间；
  2. WebUI `schema_version: 2` 管理页面 + owner 校验；
  3. 可选 FTS5；
  4. P3 host 模式与 P4 动机（默认关闭，不阻塞 P1）。
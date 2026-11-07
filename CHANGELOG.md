# Changelog

## 0.1.0 — 2026-11-07

首个可安装版本，按《方案.md》V3.0-final 实现 P1 核心与部分 P2/P3。

### 记忆

- 最小采集：主人私聊、普通私聊、直接对 bot 的群消息、带强情绪/事件信号的群消息；命令/纯媒体/无关群聊排除。
- 首次落盘前统一脱敏：密码、token、私钥、证件号、手机号、银行卡、邮箱。
- `eventKey = conversationKey:messageId`，按 generation 分片 append；按日持久 dedupe 索引。
- 纯规则重要度（基础/主人/直接/情绪/首次/约定/矛盾/关于自己/过短），夹在 1–100。
- `card.json`（当前卡）、`card.md`（人看版）、`full.jsonl`（追加式修订）、`full-index.json`、`checkpoint.json`。
- 每日衰减 `vivid→clear→fuzzy→trace→遗忘`，每日最多一档；停机最多补算 30 天。
- 容量压缩与 `max_records` 淘汰；保护记录只告警不伪造压缩完成。
- 按会话 prompt + manifest（`cardRevision`、`privacyRevision`、`memoryIds`、`contentHash`），miss 禁止全局回退。
- 注入：system 后插入带随机 nonce 的合成 user Item；硬预算 2200/1200/3600 字符。

### 隐私与治理

- owner/private/group/public 矩阵；public 必须同时命中记录授权与配置白名单。
- `erased.json` 屏障（只存 id/key 哈希），ingest/查询/渲染/恢复/擦除过滤。
- 纠错 revision+1；忘记保留 full；逻辑删除写 delete+屏障；物理擦除清理 full/inbox/operations/备份/prompt。
- 擦除/授权等长任务返回 `operation_id`；失败保持 `incomplete`。
- 磁盘 reservation ledger、85%/95% 水位、95% 停写。

### 风格与识别

- 文件风格目录 + 选择器索引；内置 `fallback`/`casual`；坏 id/坏文件跳过。
- rules 选择、最短停留 2 轮、冷却 3 轮；L1 prompt + L2 语料按字符预算截断。
- 情绪/意图一级规则；可选二级 LLM（默认关闭，日限额、5 s 本地等待、10 min 缓存）。

### 运行时

- writer flock + generation/fencing；tick 单飞；卸载取消任务并等待在途 tick。
- after_send 只接受 `sent=true`，按 `reply_message_id` 关联入站记录；无法关联写独立 `qq-self`。
- 管理 API 12 个 + `/persona stats|selfcheck|probe` 命令（仅主人）。
- P0 探针：`diagnostics.hook_payload_dump` 写 Hook 载荷结构（默认关闭）。
- 47 个单元测试；SDK 2.21.0 导入/组件/配置/加载卸载冒烟。

### 未包含

- WebUI 自定义页面、FTS5、recall 再次提及检测、P3 host 模式、P4 动机、merge 合并、图片/表情记忆。
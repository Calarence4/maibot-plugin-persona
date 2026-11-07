# 部署与恢复

目标环境：Linux（Ubuntu）、Python 3.12+、4 GB 内存、MaiBot 统一 QQ 连接器 + NapCat/SnowLuma。

## 1. 前置检查

| 项 | 检查方式 | 本插件要求 |
| --- | --- | --- |
| Host 版本 | 启动日志 / WebUI | `_manifest.json` 声明 `1.3.19 – 1.3.99`；P0 通过前不要扩大 |
| Runner SDK | 启动日志 | 实测 `2.21.0`；声明 `2.21.0 – 2.99.99` |
| Python | `python3 --version` | 3.12+（Host 自身要求优先） |
| 插件目录 | 启动日志 `plugins/` | 第三方插件目录 |
| A_Memorix | `config/bot_config.toml` | 建议 `[a_memorix.plugin] enabled = false` |
| WebUI | Dashboard | 建议开启；当前版本未提供自定义页面 |

## 2. 关闭 A_Memorix

```toml
# /root/my-maibot/config/bot_config.toml
[a_memorix.plugin]
enabled = false
```

重启 Host。确认日志不再出现 `A_Memorix.*` 的画像/向量初始化。关闭后其人物画像、Episode、图片记忆、向量检索与记忆纠错不可用；本插件不读取 A_Memorix 的数据。

## 3. 安装

```bash
cd /root/my-maibot
# 目录名建议与插件 id 一致，便于排查；Host 实际以 _manifest.json 为准
cp -r /path/to/maibot-plugin-persona plugins/com.calarence.persona
chown -R "$(id -u):$(id -g)" plugins/com.calarence.persona
```

不要复制 `config.toml`（如果有）：首次加载由 Runner 在**插件安装目录**生成。`data/` 与 `temp/` 目录由 Host 注入的 `ctx.paths.data_dir/runtime_dir` 决定，通常在 `data/plugins/com.calarence.persona/` 与 `temp/plugins/com.calarence.persona/`。

重启 Host 或通过 WebUI 重载插件。日志应出现：

```text
人格运行时已加载 v0.1.0：data=… runtime=…
```

## 4. 配置

最小可用配置：

```toml
[plugin]
config_version = "0.1.0"

[general]
enabled = true
owner_qq = "你的QQ号"
owner_platform = "qq"
timezone = "Asia/Shanghai"
bot_names = ["你的bot昵称"]

[privacy]
public_groups = []

[style]
enabled = false
application_mode = "disabled"

[diagnostics]
hook_payload_dump = false
```

注意：

- `owner_qq` 必须是**恰好一个**非空 ID；逗号分隔、空白、多个主人都会让插件禁用记忆并给出配置错误。
- `privacy.public_groups` 只接受完整 `conversationKey`：`platform:account:route_scope:chat_type:chat_id`。不要填裸群号。
- `style.application_mode="host"` 在 P3 探针通过前不要开启；当前实现只告警、不接管。
- 修改配置会触发热更新；若 `public_groups` 变化，`privacyRevision` 递增且全部派生 prompt 立即失效。

## 5. 首次验证

主人账号在任意会话发送：

```text
/persona selfcheck
```

预期：显示版本、`记忆可用: True`、错误数 0、待探针项。若 `记忆可用: False`，先看 `/persona stats` 输出的配置错误。

再发一条普通消息，几秒后：

```text
/persona stats
```

预期：记录数 ≥1、`writer: held`、上次 tick 有值。

可选：打开 P0 探针后按 [P0-PROBE.md](P0-PROBE.md) 验证 Hook 契约。

## 6. 升级

1. 停止 Host（或卸载插件）。
2. 备份整个 data 目录：`tar czf persona-data-$(date +%F).tgz data/plugins/com.calarence.persona`。
3. 替换插件代码；保留 `config.toml`。
4. 启动 Host；Runner 会补齐新增配置字段，缺失字段用默认值。
5. 查看日志确认无配置校验错误；跑 `/persona selfcheck`。

若新版本 manifest 提高了 Host/SDK 下限，而宿主版本不足，Host 会拒绝加载；回滚代码即可，数据目录格式向后兼容（当前 `schemaVersion: 1`）。

## 7. 备份与恢复

自动备份位于 `memory/backups/daily/<date>/` 与 `weekly/<date>/`，包含 `card.json`、`erased.json`、`checkpoint.json`、`session-map.json`、`style-state.json`，**不包含** `full.jsonl`。

恢复顺序（必须遵守）：

1. 停止插件。
2. **先合并擦除屏障**：用当前 `memory/erased.json` 与备份中的 `erased.json` 求并集：

   ```bash
   python scripts/merge_erased.py \
     --current data/plugins/com.calarence.persona/memory/erased.json \
     --backup  /path/to/backup/erased.json
   ```

   干净目录里只有旧备份、没有当前屏障时，脚本默认拒绝写入并提示“屏障缺失，可能复活旧数据”。确认接受风险后才可加 `--accept-missing-risk`。
3. 再恢复 `card.json`、`session-map.json` 等其余文件。
4. 启动后执行一次 `tick`（等待一分钟或重载插件），检查 `/persona stats` 与 `full-stats.json`。

为什么不能先恢复其他文件：`erased.json` 是防复活屏障，旧备份可能早于最近一次擦除；先求并集才能保证已擦除内容不因恢复而回到当前卡或 prompt。

## 8. 磁盘与降级

- 总预算 `storage.budget_bytes` 默认约 1.2 GiB；85% 红色告警，95% 停止新摄取、FTS 重建与新增修订，保留查询/导出/小型逻辑删除。
- 达到 95% 后入站采集不再落盘（日志只报一次），`/persona stats` 的 `storage.level` 为 `stop`。
- `full_warn_bytes`（默认 512 MiB）只是 `full.jsonl` 单项告警线，不是总上限。
- 物理擦除在空间不足时保持 `incomplete`，不会伪报完成；可先删除旧备份再重试。

## 9. 卸载

1. 通过 WebUI 卸载或停止 Host。
2. 插件会在 `on_unload` 中取消 tick、等待在途维护、落盘并释放 writer 锁。
3. 代码目录可删除；`data/plugins/com.calarence.persona/` 不会自动删除，按需保留或清理。
4. 若曾关闭 A_Memorix，需要时再改回 `enabled = true`。

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 日志“记忆子系统未启用” | `owner_qq` 为空/多个/含空格 | 改成恰好一个 QQ 号后热更新 |
| 群消息不落盘 | 最小采集过滤（无关群聊/纯媒体/命令） | 正常行为；需要时让消息带情绪/事件信号或 @bot |
| `/persona` 无回复 | 命令未匹配或非主人 | 确认主人 QQ；查看 Host 命令日志 |
| 记录数一直 0 | 插件未收到 `chat.receive.after_process` 或 dry_run 开启 | 看 P0 探针；检查 Host Hook 清单 |
| 注入不出现 | 当前会话无可见记忆 / prompt miss / 预算 | 用 `preview_prompt` 指定 conversationKey 预览 |
| writer 一直 busy | 另一个 Runner 持有锁或旧进程未退出 | 重启 Host；检查 `state/writer-state.json` 心跳 |
| 磁盘 `stop` | 占用 ≥95% | 清理旧备份/压缩 full；不要直接删 `erased.json` |
| 风格不注入 | `style.enabled=false` / `application_mode=disabled` | 按 P3 验收后再启用 `plugin` 模式 |

日志默认不写明文密钥与完整隐私原文；如开启 `diagnostics.include_text`，探针也先经过脱敏，但仍建议只在 P0 期间临时打开。
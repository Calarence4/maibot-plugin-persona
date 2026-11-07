"""MaiBot SDK 入口：Hook、生命周期与管理 API。

本模块是唯一依赖 ``maibot_sdk`` 的业务文件；核心逻辑在 :mod:`persona.service`。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from persona import __version__  # noqa: E402
from persona.config_model import PersonaConfig  # noqa: E402
from persona.identity import build_conversation  # noqa: E402
from persona.service import PersonaService  # noqa: E402

LOGGER = logging.getLogger("plugin.com.calarence.persona")


class PersonaPlugin(MaiBotPlugin):
    """「人格运行时」插件。"""

    config_model = PersonaConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: PersonaService | None = None
        self._load_error = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        """初始化数据目录、加载卡片并启动 tick。"""

        paths = self.ctx.paths
        try:
            service = PersonaService(
                paths.data_dir,
                paths.runtime_dir,
                self.get_plugin_config_data(),
            )
            service._llm_generate_impl = self._llm_generate  # type: ignore[attr-defined]
            await service.start()
            self._service = service
            LOGGER.info(
                "人格运行时已加载 v%s：data=%s runtime=%s",
                __version__,
                paths.data_dir,
                paths.runtime_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 加载失败必须明确记录
            self._load_error = str(exc)
            LOGGER.exception("人格运行时加载失败：%s", exc)

    async def on_unload(self) -> None:
        """取消 tick、落盘状态并释放 writer 锁。"""

        service = self._service
        self._service = None
        if service is not None:
            try:
                await service.stop()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("卸载时落盘失败：%s", exc)
        LOGGER.info("人格运行时已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """self 配置热更新；bot/model 广播不参与本插件状态。"""

        if scope != "self":
            LOGGER.debug("忽略全局配置广播：scope=%s version=%s", scope, version)
            return
        if self._service is None:
            LOGGER.warning("配置已更新但服务尚未初始化，跳过：version=%s", version)
            return
        try:
            self._service.reconfigure(config_data)
            LOGGER.info("人格运行时配置已热更新：version=%s", version)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("配置热更新失败：%s", exc)

    async def _llm_generate(self, prompt: str) -> dict[str, Any]:
        """供识别/风格使用的可选 LLM 调用；失败返回 success=False。"""

        try:
            result = await self.ctx.llm.generate(prompt, temperature=0.2, max_tokens=256)
            return result if isinstance(result, dict) else {"success": False, "response": ""}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("LLM 调用失败：%s", exc)
            return {"success": False, "response": "", "error": str(exc)}

    # ------------------------------------------------------------------
    # Hook：采集与回复
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="persona_inbound_collector",
        description="OBSERVE：规范化、过滤、首次脱敏后 append 到本代际 inbox 分片",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.LOG,
    )
    async def handle_inbound(self, **kwargs: Any) -> None:
        service = self._service
        if service is None:
            return
        message = kwargs.get("message")
        if isinstance(message, dict):
            service.record_hook_probe("chat.receive.after_process", kwargs)
            service.handle_inbound(message)

    @HookHandler(
        "maisaka.replyer.before_request",
        name="persona_turn_context",
        description="BLOCKING：解析 TurnContext、rules 选风格、写 persona_turn_id；不注入正文",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_request(self, **kwargs: Any) -> dict[str, Any]:
        service = self._service
        if service is None:
            return {"action": "continue"}
        service.record_hook_probe("maisaka.replyer.before_request", kwargs)
        return service.before_request(kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="persona_memory_injector",
        description="BLOCKING：按当前会话只读缓存，在 system 后插入一条合成 user Item",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_model_request(self, **kwargs: Any) -> dict[str, Any]:
        service = self._service
        if service is None:
            return {"action": "continue"}
        service.record_hook_probe("maisaka.replyer.before_model_request", kwargs)
        return service.before_model_request(kwargs)

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="persona_reply_candidate",
        description="BLOCKING：只登记最终候选文本与关联键；不记 recall、不声称已送达",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_post_process(self, **kwargs: Any) -> dict[str, Any]:
        service = self._service
        if service is None:
            return {"action": "continue"}
        service.record_hook_probe("maisaka.reply.before_post_process", kwargs)
        return service.before_post_process(kwargs)

    @HookHandler(
        "send_service.after_send",
        name="persona_outbound_observer",
        description="OBSERVE：确认实际出站后登记 botReplies / P4 回执",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.LOG,
    )
    async def handle_after_send(self, **kwargs: Any) -> None:
        service = self._service
        if service is None:
            return
        service.record_hook_probe("send_service.after_send", kwargs)
        service.after_send(kwargs)

    # ------------------------------------------------------------------
    # 管理命令（仅主人可用；用于部署后快速自检）
    # ------------------------------------------------------------------

    def _command_sender(self, kwargs: dict[str, Any]) -> str:
        message = kwargs.get("message")
        if isinstance(message, dict):
            conversation = build_conversation(
                message,
                owner_qq=self._service.settings.general.owner_qq if self._service else "",
            )
            if conversation.raw.get("user_id"):
                return str(conversation.raw["user_id"])
        for key in ("user_id", "sender_id", "qq"):
            value = kwargs.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        return ""

    @Command("persona", pattern=r"^/persona(?:\s+(stats|selfcheck|probe))?$")
    async def handle_persona_command(self, **kwargs: Any):
        """``/persona stats|selfcheck|probe``：主人专用的部署自检。"""

        service = self._service
        if service is None:
            return False, "人格运行时未初始化，请检查插件日志。", 1
        owner = service.settings.general.owner_qq
        sender = self._command_sender(kwargs)
        if not owner or sender != owner:
            return False, "仅主人可以使用该命令。", 1

        raw_text = str(kwargs.get("raw_message") or kwargs.get("text") or kwargs.get("command_text") or "")
        if not raw_text and isinstance(kwargs.get("message"), dict):
            from persona.identity import extract_text

            raw_text = extract_text(kwargs["message"])
        sub = "stats"
        for candidate in ("selfcheck", "probe", "stats"):
            if candidate in raw_text:
                sub = candidate
                break

        if sub == "selfcheck":
            result = await self.api_self_check()
            lines = [
                f"人格运行时 v{__version__}",
                f"记忆可用: {result.get('config', {}).get('memory_enabled')}",
                f"错误: {len(result.get('config', {}).get('errors') or [])} 条",
                f"待探针: {len(result.get('probe_required') or [])} 项",
                "详见插件日志或 WebUI API。",
            ]
            return True, "\n".join(lines), 2
        if sub == "probe":
            files = service.probe_files()
            if not files:
                return True, "暂无 Hook 探针文件（diagnostics.hook_payload_dump 未开启或尚未触发）。", 2
            return True, "最近探针：" + "、".join(files[-3:]), 2

        snapshot = service.snapshot()
        storage = snapshot.get("storage") or {}
        lines = [
            f"卡 r{snapshot.get('cardRevision')} · 记录 {snapshot.get('records')} · 保护 {snapshot.get('protected')}",
            f"full {int((snapshot.get('full') or {}).get('bytes') or 0) / 1024 / 1024:.1f} MiB · 磁盘 {float(storage.get('ratio') or 0) * 100:.1f}%",
            f"writer: {'held' if (snapshot.get('writer') or {}).get('held') else 'busy/idle'} · 上次 tick {snapshot.get('lastTickAt') or '-'}",
        ]
        if service.settings.errors:
            lines.append("配置错误：" + "；".join(service.settings.errors))
        return True, "\n".join(lines), 2

    # ------------------------------------------------------------------
    # 管理 API（供 WebUI / 其它插件调用；version 固定 1）
    # ------------------------------------------------------------------

    def _require_service(self) -> PersonaService:
        service = self._service
        if service is None:
            raise RuntimeError(f"服务未初始化：{self._load_error or '插件未加载'}")
        return service

    @staticmethod
    def _int(kwargs: dict[str, Any], key: str, default: int) -> int:
        try:
            return int(kwargs.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bool(kwargs: dict[str, Any], key: str, default: bool = False) -> bool:
        value = kwargs.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}

    @API("stats", description="当前卡、全量卡、磁盘与 writer 状态", version="1")
    async def api_stats(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"success": True, **self._require_service().snapshot()}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("config_summary", description="运行期配置摘要与校验错误", version="1")
    async def api_config_summary(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return {"success": True, **service.settings.summary(), "version": __version__}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("list_memories", description="列出当前卡记忆（分页，按会话/清晰度过滤）", version="1")
    async def api_list_memories(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return {
                "success": True,
                **service.list_records(
                    conversation_key=str(kwargs.get("conversation_key") or ""),
                    clarity=str(kwargs.get("clarity") or ""),
                    limit=self._int(kwargs, "limit", 50),
                    offset=self._int(kwargs, "offset", 0),
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("get_memory", description="读取单条记忆（含 botReplies 与修订信息）", version="1")
    async def api_get_memory(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            record = service.get_record(str(kwargs.get("memory_id") or kwargs.get("id") or ""))
            if record is None:
                return {"success": False, "error": "记录不存在"}
            return {"success": True, "record": record}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("correct_memory", description="纠错：revision+1，旧修订保留在 full", version="1")
    async def api_correct_memory(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return service.correct_record(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                text=str(kwargs.get("text") or ""),
                title=str(kwargs.get("title") or ""),
                reason=str(kwargs.get("reason") or ""),
                operator=str(kwargs.get("operator") or "admin"),
                scope=str(kwargs.get("scope") or ""),
                allowed_conversation_keys=kwargs.get("allowed_conversation_keys")
                if isinstance(kwargs.get("allowed_conversation_keys"), list)
                else None,
                confirm_widen=self._bool(kwargs, "confirm_widen"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("preview_public", description="放宽为 public 前的跨域脱敏预览", version="1")
    async def api_preview_public(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            targets = kwargs.get("targets") if isinstance(kwargs.get("targets"), list) else []
            return service.preview_public(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                [str(item) for item in targets],
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("grant_public", description="把记录授权为 public（需 confirm=true，写审计）", version="1")
    async def api_grant_public(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            targets = kwargs.get("targets") if isinstance(kwargs.get("targets"), list) else []
            return service.grant_public(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                targets=[str(item) for item in targets],
                operator=str(kwargs.get("operator") or "admin"),
                confirm=self._bool(kwargs, "confirm"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("revoke_public", description="撤销 public 授权并立即失效派生 prompt", version="1")
    async def api_revoke_public(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return service.revoke_public(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                operator=str(kwargs.get("operator") or "admin"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("forget_memory", description="忘记正文：移出当前卡，全量历史保留", version="1")
    async def api_forget_memory(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return service.forget_record(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                operator=str(kwargs.get("operator") or "admin"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("delete_memory", description="逻辑删除：写 erased 屏障并在 full 追加 delete", version="1")
    async def api_delete_memory(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return service.logical_delete(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                operator=str(kwargs.get("operator") or "admin"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("erase_memory", description="物理擦除：先隐藏，再后台清理全副本（返回 operation_id）", version="1")
    async def api_erase_memory(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            return service.enqueue_physical_erase(
                str(kwargs.get("memory_id") or kwargs.get("id") or ""),
                operator=str(kwargs.get("operator") or "admin"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("operation_status", description="查询长任务状态", version="1")
    async def api_operation_status(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            op_id = str(kwargs.get("operation_id") or kwargs.get("op_id") or "")
            if not op_id:
                return {"success": True, "operations": service.operations.list_recent(20)}
            operation = service.operations.latest(op_id)
            if operation is None:
                return {"success": False, "error": "操作不存在"}
            return {"success": True, "operation": operation}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("query_full", description="尾部扫描全量卡（默认 5 MiB），返回最新可见修订", version="1")
    async def api_query_full(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            query = str(kwargs.get("query") or kwargs.get("q") or "").strip()
            limit = self._int(kwargs, "limit", 20)

            def predicate(record: dict[str, Any]) -> bool:
                if not query:
                    return True
                haystack = f"{record.get('title') or ''}\n{record.get('text') or ''}"
                return query in haystack

            records = service.full.query_tail(predicate=predicate, limit=limit)
            visible = [
                service._public_record(record)
                for record in records
                if not service.barrier.is_erased(str(record.get("id") or ""), record.get("sourceEventKeys"))
            ]
            return {
                "success": True,
                "items": visible,
                "scanBytes": service.settings.memory.full_scan_bytes,
                "note": "未启用 FTS 时只承诺尾部扫描范围内的命中",
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("preview_prompt", description="预览指定 conversationKey 的注入正文", version="1")
    async def api_preview_prompt(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            conversation_key = str(kwargs.get("conversation_key") or "").strip()
            if not conversation_key:
                return {"success": False, "error": "必须指定 conversation_key，禁止用全局 prompt 冒充"}
            return service.preview_prompt(
                conversation_key,
                budget_chars=self._int(kwargs, "budget_chars", service.settings.memory.inject_max_chars),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @API("self_check", description="P0 自检：版本、配置、路径、Hook 契约假设与运行期验证项", version="1")
    async def api_self_check(self, **kwargs: Any) -> dict[str, Any]:
        try:
            service = self._require_service()
            paths = self.ctx.paths
            snapshot = service.snapshot()
            return {
                "success": True,
                "version": __version__,
                "config": service.settings.summary(),
                "paths": {
                    "data_dir": str(paths.data_dir),
                    "runtime_dir": str(paths.runtime_dir),
                    "card_json": str(service.paths.card_json),
                    "full_jsonl": str(service.paths.full_jsonl),
                },
                "snapshot": snapshot,
                "probe_files": service.probe_files(),
                "hook_contract": {
                    "chat.receive.after_process": ["message"],
                    "maisaka.replyer.before_request": ["session_id", "reply_message_id", "reply_tool_args"],
                    "maisaka.replyer.before_model_request": ["items", "session_id", "reply_tool_args"],
                    "maisaka.reply.before_post_process": ["response", "session_id", "reply_message_id"],
                    "send_service.after_send": ["message", "sent", "reply_message_id", "session_id"],
                },
                "probe_required": [
                    "GET /plugins/runtime/hooks 核对实际 Hook 与 allow_abort/allow_modify",
                    "确认 before_model_request 的 items 真实结构与本插件插入的 item_type/meta/parts 兼容",
                    "确认 after_send 的 message.session_id 与 reply_message_id 能稳定关联入站回合",
                    "确认 Host 未传播 reply_tool_args 时 persona_turn_id 仅作本插件内部关联",
                    "隐私快照：群 A/群 B/主人私聊/普通私聊/跨账号/public 空授权与撤销",
                ],
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}


def create_plugin() -> PersonaPlugin:
    """Runner 顶层工厂。"""

    return PersonaPlugin()

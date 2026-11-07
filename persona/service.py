"""人格运行时服务编排。

负责把 Hook、采集、writer、衰减、渲染、注入、风格与回复关联串成一条线。
所有 card/full/管理 mutation 都走同一把跨 Runner 文件锁与 writer 代际检查；
热路径（replyer Hook）只读缓存，不做全卡扫描、不归档、不压缩、不等待 LLM。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import (
    FileLock,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    file_signature,
    iso_utc,
    iter_jsonl,
    local_day,
    local_datetime,
    new_id,
    parse_iso,
    prune_empty_dirs,
    read_json,
    resolve_timezone,
    sha256_hex,
    stable_id,
    utc_now,
)
from .event_state import sort_priority
from .identity import build_conversation, resolve_turn_context
from .ingest import Ingestor, list_inbox_shards
from .llm import LLMLedger
from .models import Event, is_protected, normalize_record, record_title
from .recognition import RecognitionEngine
from .render import card_block_lengths, render_card_markdown, render_conversation_prompt
from .sanitize import normalize_text, redact_secrets, sanitize_for_injection
from .scoring import (
    compute_initial,
    derive_title,
    score_event,
    step_clarity,
    target_clarity,
)
from .settings import Settings, settings_from_mapping
from .store import (
    CardStore,
    CheckpointStore,
    ErasedBarrier,
    FullLog,
    OperationLog,
    PluginPaths,
    StaleWriter,
    StorageGuard,
    StorageFull,
    WriterState,
)
from .style import (
    Style,
    ensure_builtin_styles,
    load_styles,
    render_index,
    render_style_block,
    select_style,
)

LOGGER = logging.getLogger("plugin.com.calarence.persona")

TICK_INTERVAL_SECONDS = 60


def _mutation_guarded(method):
    """用可重入 tick 锁串行化 admin 读写与维护 mutation。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._tick_guard:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass
class PendingTurn:
    """一次 replyer 轮的关联信息。"""

    turn_id: str
    conversation_key: str
    session_id: str
    reply_message_id: str = ""
    style_id: str = ""
    scene: str = ""
    emotion: str = ""
    intent: str = ""
    memory_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    candidate_text: str = ""


class PersonaService:
    """插件唯一运行时对象。"""

    def __init__(self, data_dir: str | Path, runtime_dir: str | Path, raw_config: dict[str, Any] | None) -> None:
        self.paths = PluginPaths.from_dirs(data_dir, runtime_dir)
        self.raw_config = dict(raw_config or {})
        self.settings: Settings = settings_from_mapping(self.raw_config)
        self.paths.ensure()

        self.storage = StorageGuard(self.paths, self.settings)
        self.barrier = ErasedBarrier(self.paths)
        self.card_store = CardStore(self.paths, self.settings)
        self.full = FullLog(self.paths, self.storage)
        self.checkpoint = CheckpointStore(self.paths)
        self.operations = OperationLog(self.paths)
        self.writer_state = WriterState(self.paths)
        self.llm_ledger = LLMLedger(self.paths.llm_ledger, self.settings)
        self.recognition = RecognitionEngine(self.paths.recognition_state, self.settings)

        self.card: dict[str, Any] = default_card_copy()
        self.styles: dict[str, Style] = {}
        self.style_state: dict[str, Any] = {}
        self.session_map: dict[str, str] = {}
        self.exposure: dict[str, dict[str, Any]] = {}

        self._ingestor = Ingestor(
            self.paths,
            self.settings,
            self.storage,
            self.barrier,
            generation=self.writer_state.token[:16],
        )
        self._index: dict[str, list[str]] = {}
        self._record_conversations: dict[str, set[str]] = {}
        self._prompt_cache: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._turn_by_id: dict[str, PendingTurn] = {}
        self._turn_by_reply: dict[str, PendingTurn] = {}
        self._pending_reply_text: dict[tuple[str, str], tuple[str, float]] = {}
        self._message_index: dict[str, str] = {}
        self._reply_offset = 0

        self._writer_lock = FileLock(self.paths.writer_lock)
        self._writer_held = False
        self._tick_guard = threading.RLock()
        self._state_lock = threading.RLock()
        self._started = False
        self._stopped = False
        self._tick_task: asyncio.Task | None = None
        self._power_tasks: set[asyncio.Task] = set()
        self._last_heavy_at = 0.0
        self._last_tick_at = ""
        self._privacy_fingerprint = self._compute_privacy_fingerprint()
        self._warned_no_owner = False
        self._last_render_errors = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """同步初始化：加载卡片、索引、风格与状态文件。"""

        if self._started:
            return
        self.paths.ensure()
        ensure_builtin_styles(self.paths.styles_dir)
        self.styles = load_styles(self.paths.styles_dir)
        try:
            atomic_write_text(self.paths.styles_index, render_index(self.styles))
        except OSError:
            pass

        self.card = self.card_store.load()
        self.session_map = self._load_mapping(self.paths.session_map)
        self.style_state = self._load_mapping(self.paths.style_state)
        self.exposure = self._load_mapping(self.paths.exposure_state)
        self._reply_offset = int(read_json(self.paths.state_dir / "reply-cursor.json", {}) .get("offset") or 0)
        self._build_index()
        self._index_messages()
        self._started = True
        if not self.settings.memory_enabled:
            LOGGER.error("记忆子系统未启用：%s", "; ".join(self.settings.errors) or "配置不完整")
        for warning in self.settings.warnings:
            LOGGER.warning("配置警告：%s", warning)

    async def start(self) -> None:
        """初始化并启动 tick（首次维护在后台，不阻塞 on_load）。"""

        self.initialize()
        self._stopped = False
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """取消 tick、落盘状态并释放锁。"""

        self._stopped = True
        task = self._tick_task
        self._tick_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._power_tasks):
            task.cancel()
        if self._power_tasks:
            await asyncio.gather(*self._power_tasks, return_exceptions=True)
        await asyncio.to_thread(self._shutdown_sync)

    def _shutdown_sync(self) -> None:
        acquired = self._tick_guard.acquire(timeout=30)
        if not acquired:
            LOGGER.warning("卸载时仍有 tick 在运行，最多等待 30 秒后继续落盘")
        try:
            self._flush_states()
            self.card_store.save(self.card)
            self.full.flush()
        except Exception as exc:  # noqa: BLE001 - 卸载期间尽力落盘
            LOGGER.warning("卸载落盘失败：%s", exc)
        finally:
            if acquired:
                self._tick_guard.release()
        if self._writer_held:
            try:
                self.writer_state.release()
            finally:
                self._writer_held = False
                self._writer_lock.release()

    async def _tick_loop(self) -> None:
        first = True
        while not self._stopped:
            try:
                await asyncio.to_thread(self.tick, first)
                first = False
                await asyncio.sleep(TICK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - tick 不得杀死循环
                LOGGER.exception("tick 异常：%s", exc)
                await asyncio.sleep(TICK_INTERVAL_SECONDS)

    @_mutation_guarded
    def reconfigure(self, raw_config: dict[str, Any]) -> None:
        """配置热更新：不改写配置文件，只重算运行期配置。"""

        old_enabled = self.settings.memory_enabled
        self.raw_config = dict(raw_config or {})
        self.settings = settings_from_mapping(self.raw_config)
        self.storage.settings = self.settings
        self.card_store.settings = self.settings
        self.llm_ledger.settings = self.settings
        self.recognition.settings = self.settings
        self._ingestor.settings = self.settings

        fingerprint = self._compute_privacy_fingerprint()
        if fingerprint != self._privacy_fingerprint:
            self._privacy_fingerprint = fingerprint
            self._bump_privacy_revision()
        self.styles = load_styles(self.paths.styles_dir)
        try:
            atomic_write_text(self.paths.styles_index, render_index(self.styles))
        except OSError:
            pass
        if old_enabled and not self.settings.memory_enabled:
            self._release_writer()
        LOGGER.info("配置已更新：%s", json.dumps(self.settings.summary(), ensure_ascii=False))

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------

    def handle_inbound(self, message: dict[str, Any]) -> bool:
        """``chat.receive.after_process`` 的 OBSERVE 入口。"""

        if not self.settings.memory_enabled or not self.settings.general.enabled:
            return False
        try:
            event, reason = self._ingestor.prepare(message)
            if event is None:
                LOGGER.debug("未采集：%s", reason)
                return False
            self._remember_session(event)
            with self._state_lock:
                self.recognition.observe(event.conversation_key, event.text)
            appended = self._ingestor.append(event)
            if appended and self.settings.recognition.llm_enabled:
                state = self.recognition.get(event.conversation_key)
                if state.get("needsLlm"):
                    self._schedule_recognition(event.conversation_key, event.text)
            return appended
        except Exception as exc:  # noqa: BLE001 - OBSERVE 失败只记日志
            LOGGER.exception("入站采集失败：%s", exc)
            return False

    def _remember_session(self, event: Event) -> None:
        with self._state_lock:
            if event.session_id:
                self.session_map[event.session_id] = event.conversation_key
            if event.message_id:
                self.session_map[f"msg:{event.message_id}"] = event.conversation_key

    def _schedule_recognition(self, conversation_key: str, text: str) -> None:
        """后台做一次可选 LLM 识别；热路径不等待。"""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self.llm_ledger.try_consume("recognition", self.settings.recognition.llm_max_per_day):
            return

        async def runner() -> None:
            try:
                await self.recognition.refine_with_llm(
                    conversation_key,
                    text,
                    lambda prompt: self._llm_generate(prompt),
                    timeout_ms=self.settings.recognition.llm_timeout_ms,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("后台识别失败：%s", exc)

        task = loop.create_task(runner())
        self._power_tasks.add(task)
        task.add_done_callback(self._power_tasks.discard)

    async def _llm_generate(self, prompt: str) -> dict[str, Any]:
        """由 plugin_impl 注入真实实现；默认返回失败，避免静默假装成功。"""

        generate = getattr(self, "_llm_generate_impl", None)
        if generate is None:
            return {"success": False, "response": ""}
        return await generate(prompt)

    # ------------------------------------------------------------------
    # P0 探针
    # ------------------------------------------------------------------

    def record_hook_probe(self, hook: str, kwargs: dict[str, Any]) -> None:
        """把真实 Hook 载荷的**结构**写入 runtime/probes（默认关闭）。"""

        if not self.settings.diagnostics.hook_payload_dump:
            return
        try:
            directory = self.paths.runtime / "probes"
            ensure_dir(directory)
            files = sorted(directory.glob("*.json"), key=lambda item: item.name)
            limit = max(1, int(self.settings.diagnostics.max_files))
            while len(files) >= limit:
                try:
                    files.pop(0).unlink()
                except OSError:
                    break
            payload = {
                "hook": hook,
                "at": iso_utc(),
                "keys": sorted(str(key) for key in kwargs),
                "shape": self._probe_shape(kwargs),
            }
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{hook.replace('.', '_')}-{uuid.uuid4().hex[:6]}.json"
            atomic_write_json(directory / name, payload)
        except Exception as exc:  # noqa: BLE001 - 探针不得影响主流程
            LOGGER.debug("Hook 探针写入失败：%s", exc)

    def _probe_shape(self, value: Any, depth: int = 0, key: str = "") -> Any:
        """把任意载荷压缩成不含正文的结构描述。"""

        if depth >= 4:
            return "<depth-limit>"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (item_key, item) in enumerate(value.items()):
                if index >= 40:
                    result["__truncated_keys__"] = len(value) - 40
                    break
                result[str(item_key)] = self._probe_shape(item, depth + 1, str(item_key))
            return result
        if isinstance(value, (list, tuple)):
            items = list(value)
            shape: dict[str, Any] = {"__list_len__": len(items)}
            if items:
                shape["first"] = self._probe_shape(items[0], depth + 1, key)
            return shape
        if isinstance(value, str):
            # 枚举/标记类短字段保留原值，便于核对 item_type、parts.type。
            if key in {"item_type", "type", "role", "status", "kind", "chat_type", "platform", "scope"} and len(value) <= 64:
                return value
            if self.settings.diagnostics.include_text:
                return {"type": "str", "len": len(value), "text": redact_secrets(value)[0][:200]}
            return {"type": "str", "len": len(value)}
        if isinstance(value, bool):
            return {"type": "bool", "value": value}
        if isinstance(value, (int, float)):
            return {"type": type(value).__name__, "value": value}
        if value is None:
            return None
        return {"type": type(value).__name__}

    def probe_files(self) -> list[str]:
        """列出当前探针文件（供 self_check）。"""

        directory = self.paths.runtime / "probes"
        if not directory.exists():
            return []
        return sorted(path.name for path in directory.glob("*.json"))

    # ------------------------------------------------------------------
    # replyer / 注入
    # ------------------------------------------------------------------

    def before_request(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """``maisaka.replyer.before_request``：只登记 TurnContext 与风格，不注入正文。"""

        modified = dict(kwargs)
        try:
            turn = resolve_turn_context(kwargs, self.session_map)
            if turn is None:
                LOGGER.info("replyer.before_request 无法定位当前会话，本次不选风格、不注入")
                return {"action": "continue"}
            state = self.recognition.get(turn.conversation_key)
            scene = self._scene_of(turn.conversation_key)
            style_id = ""
            style_reason = ""
            if self.settings.style.enabled:
                with self._state_lock:
                    style_id, style_reason = select_style(
                        styles=self.styles,
                        style_state=self.style_state,
                        conversation_key=turn.conversation_key,
                        emotion=str(state.get("emotion") or ""),
                        intent=str(state.get("intent") or ""),
                        scene=scene,
                        settings=self.settings,
                    )
            pending = PendingTurn(
                turn_id=turn.turn_id,
                conversation_key=turn.conversation_key,
                session_id=turn.session_id,
                reply_message_id=turn.reply_message_id,
                style_id=style_id,
                scene=scene,
                emotion=str(state.get("emotion") or ""),
                intent=str(state.get("intent") or ""),
            )
            self._store_turn(pending)
            reply_tool_args = modified.get("reply_tool_args")
            if not isinstance(reply_tool_args, dict):
                reply_tool_args = {}
            reply_tool_args = dict(reply_tool_args)
            reply_tool_args["persona_turn_id"] = pending.turn_id
            modified["reply_tool_args"] = reply_tool_args
            LOGGER.debug(
                "TurnContext %s：conversation=%s style=%s（%s）",
                pending.turn_id[:8],
                turn.conversation_key,
                style_id or "-",
                style_reason or "-",
            )
        except Exception as exc:  # noqa: BLE001 - SKIP 策略
            LOGGER.exception("before_request 失败：%s", exc)
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": modified}

    def before_model_request(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """``maisaka.replyer.before_model_request``：按会话只读缓存并插入一条合成 user Item。"""

        try:
            items = kwargs.get("items")
            if not isinstance(items, list) or not items:
                LOGGER.info("before_model_request 缺少 items，跳过注入")
                return {"action": "continue"}
            conversation_key = self._conversation_for_hook(kwargs)
            if not conversation_key:
                LOGGER.info("before_model_request 无法确定当前会话，跳过注入")
                return {"action": "continue"}
            memory_text = self._memory_text_for(conversation_key)
            style_text = self._style_text_for(conversation_key, kwargs)
            block = self._compose_injection(memory_text, style_text)
            if not block:
                return {"action": "continue"}
            item = self._synthetic_user_item(block)
            new_items = list(items)
            insert_at = 0
            for index, candidate in enumerate(items):
                if isinstance(candidate, dict) and candidate.get("item_type") == "SystemMessageItem":
                    insert_at = index + 1
                    break
            new_items.insert(insert_at, item)
            if memory_text:
                self._record_candidate_exposure(conversation_key)
            modified = dict(kwargs)
            modified["items"] = new_items
            LOGGER.debug(
                "注入 %d 字符（memory=%d style=%d，会话 %s）",
                len(block),
                len(memory_text),
                len(style_text),
                conversation_key,
            )
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as exc:  # noqa: BLE001 - SKIP 策略
            LOGGER.exception("before_model_request 失败：%s", exc)
            return {"action": "continue"}

    def before_post_process(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """只登记最终候选文本与关联键；不记 recall、不声称已送达。"""

        try:
            response = kwargs.get("response")
            if isinstance(response, str) and response.strip():
                session_id = str(kwargs.get("session_id") or "").strip()
                reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
                self._pending_reply_text[(session_id, reply_message_id)] = (response, time.time())
                turn = self._turn_by_reply.get(reply_message_id)
                if turn is not None:
                    turn.candidate_text = response
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("before_post_process 登记失败：%s", exc)
        return {"action": "continue"}

    def after_send(self, kwargs: dict[str, Any]) -> None:
        """``send_service.after_send``：只接受明确 sent 的可见文本。"""

        try:
            if not bool(kwargs.get("sent")):
                return
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return
            conversation = build_conversation(
                message,
                owner_qq=self.settings.general.owner_qq,
                owner_platform=self.settings.general.owner_platform,
                owner_account=self.settings.general.owner_account,
                bot_names=self.settings.general.bot_names,
            )
            session_id = str(message.get("session_id") or kwargs.get("session_id") or conversation.session_id or "")
            reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
            text = ""
            for key in ("processed_plain_text", "plain_text"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
            if not text:
                pending = self._pending_reply_text.get((session_id, reply_message_id))
                if pending and time.time() - pending[1] <= 120:
                    text = pending[0]
            if not text and isinstance(message.get("raw_message"), list):
                from .identity import component_text

                text = component_text(message.get("raw_message"))
            text = normalize_text(text)
            if not text:
                return
            entry = {
                "v": 1,
                "sessionId": session_id,
                "replyMessageId": reply_message_id,
                "messageId": str(message.get("message_id") or ""),
                "conversationKey": conversation.conversation_key,
                "chatType": conversation.chat_type,
                "text": redact_secrets(text)[0],
                "sent": True,
                "at": iso_utc(),
            }
            from .common import append_jsonl

            line_size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 2
            try:
                reservation = self.storage.reserve(line_size, "reply.append")
            except StorageFull:
                LOGGER.warning("磁盘水位过高，本次 botReply 未登记")
                return
            try:
                append_jsonl(self.paths.reply_inbox, entry)
            finally:
                self.storage.release(reservation)
        except Exception as exc:  # noqa: BLE001 - OBSERVE
            LOGGER.exception("after_send 登记失败：%s", exc)

    def _store_turn(self, turn: PendingTurn) -> None:
        self._turn_by_id[turn.turn_id] = turn
        if turn.reply_message_id:
            self._turn_by_reply[turn.reply_message_id] = turn
        self._cleanup_turns()

    def _cleanup_turns(self) -> None:
        ttl = max(60, self.settings.memory.pending_reply_ttl_minutes * 60 * 2)
        now = time.time()
        for key, turn in list(self._turn_by_id.items()):
            if now - turn.created_at > ttl:
                self._turn_by_id.pop(key, None)
                if turn.reply_message_id:
                    self._turn_by_reply.pop(turn.reply_message_id, None)
        cutoff = now - 600
        for key, value in list(self._pending_reply_text.items()):
            if value[1] < cutoff:
                self._pending_reply_text.pop(key, None)

    def _conversation_for_hook(self, kwargs: dict[str, Any]) -> str:
        session_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
        if session_id and session_id in self.session_map:
            return self.session_map[session_id]
        reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
        if reply_message_id and reply_message_id in self.session_map:
            return self.session_map[reply_message_id]
        reply_tool_args = kwargs.get("reply_tool_args")
        if isinstance(reply_tool_args, dict):
            turn_id = str(reply_tool_args.get("persona_turn_id") or "").strip()
            turn = self._turn_by_id.get(turn_id)
            if turn is not None:
                return turn.conversation_key
        if session_id:
            return f"session:{session_id}"
        return ""

    def _memory_text_for(self, conversation_key: str) -> str:
        text = self._read_prompt_cached(conversation_key)
        if text is not None:
            return text
        rendered = self._render_prompt(conversation_key)
        if rendered is None:
            LOGGER.info("prompt miss 且本地索引不可用，本轮不注入并排队重建（%s）", conversation_key)
            self._dirty.add(conversation_key)
            return ""
        return rendered

    def _style_text_for(self, conversation_key: str, kwargs: dict[str, Any]) -> str:
        if not self.settings.style.enabled or self.settings.style.application_mode != "plugin":
            return ""
        style_id = ""
        reply_tool_args = kwargs.get("reply_tool_args")
        if isinstance(reply_tool_args, dict):
            turn = self._turn_by_id.get(str(reply_tool_args.get("persona_turn_id") or ""))
            if turn is not None:
                style_id = turn.style_id
        if not style_id:
            state = self.style_state.get(conversation_key)
            if isinstance(state, dict):
                style_id = str(state.get("styleId") or "")
        style = self.styles.get(style_id)
        if style is None:
            return ""
        state = self.recognition.get(conversation_key)
        return render_style_block(
            style,
            self.settings,
            scene=self._scene_of(conversation_key),
            emotion=str(state.get("emotion") or ""),
            intent=str(state.get("intent") or ""),
            budget_chars=self.settings.style.style_inject_max_chars,
        )

    def _compose_injection(self, memory_text: str, style_text: str) -> str:
        total_budget = max(0, int(self.settings.style.total_inject_max_chars))
        nonce = uuid.uuid4().hex[:16]
        overhead = len("<persona_data nonce=\"\">\n\n</persona_data>") + len(nonce)
        memory_text = sanitize_for_injection(memory_text)
        style_text = sanitize_for_injection(style_text) if style_text else ""

        if memory_text and style_text:
            body = f"[记忆]\n{memory_text}\n\n[风格]\n{style_text}"
        elif memory_text:
            body = f"[记忆]\n{memory_text}"
        elif style_text:
            body = f"[风格]\n{style_text}"
        else:
            return ""
        limit = max(0, total_budget - overhead)
        if len(body) > limit:
            # 先裁风格，再裁记忆；硬预算优先于「至少保留一条」。
            if memory_text and style_text:
                memory_budget = min(len(memory_text), max(0, limit - 16))
                style_budget = max(0, limit - memory_budget - 16)
                memory_text = memory_text[:memory_budget]
                style_text = style_text[:style_budget]
                body = f"[记忆]\n{memory_text}\n\n[风格]\n{style_text}"
            else:
                body = body[:limit]
        if not body.strip():
            return ""
        return f'<persona_data nonce="{nonce}">\n{body}\n</persona_data>'

    @staticmethod
    def _synthetic_user_item(block: str) -> dict[str, Any]:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
        return {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": timestamp,
            },
            "parts": [{"type": "text", "text": block}],
        }

    def _record_candidate_exposure(self, conversation_key: str) -> None:
        """只记录**实际进入本次渲染**的记忆 id；不代表模型真的看过。"""

        manifest = read_json(self._prompt_dir(conversation_key) / "manifest.json", {})
        ids = manifest.get("memoryIds") if isinstance(manifest, dict) else None
        if not isinstance(ids, list):
            ids = self._index.get(conversation_key) or []
        now = iso_utc()
        with self._state_lock:
            for record_id in ids:
                entry = self.exposure.get(record_id)
                if not isinstance(entry, dict):
                    entry = {"count": 0, "lastAt": None}
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["lastAt"] = now
                self.exposure[record_id] = entry

    # ------------------------------------------------------------------
    # 状态文件与索引
    # ------------------------------------------------------------------

    @staticmethod
    def _load_mapping(path: Path) -> dict[str, Any]:
        data = read_json(path, None)
        return data if isinstance(data, dict) else {}

    def _build_index(self) -> None:
        self._index = {}
        self._record_conversations = {}
        for record in (self.card.get("records") or {}).values():
            if isinstance(record, dict):
                self._index_record(record)

    def _index_record(self, record: dict[str, Any]) -> None:
        record_id = str(record.get("id") or "")
        if not record_id:
            return
        targets = self._targets_for_record(record)
        self._record_conversations[record_id] = targets
        for conversation_key in targets:
            bucket = self._index.setdefault(conversation_key, [])
            if record_id not in bucket:
                bucket.append(record_id)

    def _unindex_record(self, record_id: str) -> None:
        for conversation_key in self._record_conversations.pop(record_id, set()):
            bucket = self._index.get(conversation_key)
            if not bucket:
                continue
            try:
                bucket.remove(record_id)
            except ValueError:
                continue
            if not bucket:
                self._index.pop(conversation_key, None)

    def _targets_for_record(self, record: dict[str, Any]) -> set[str]:
        targets: set[str] = set()
        source = str(record.get("conversationKey") or "")
        if source:
            targets.add(source)
        if str(record.get("scope") or "") == "public":
            allowed = set(self.settings.privacy.public_groups)
            for conversation_key in record.get("allowedConversationKeys") or []:
                if conversation_key in allowed:
                    targets.add(str(conversation_key))
        return targets

    def _index_messages(self) -> None:
        """建立 messageId → recordId 索引（用于 botReplies 关联）。"""

        self._message_index = {}
        for record in (self.card.get("records") or {}).values():
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("id") or "")
            conversation_key = str(record.get("conversationKey") or "")
            for event_key in record.get("sourceEventKeys") or []:
                event_key = str(event_key)
                prefix = f"{conversation_key}:"
                message_id = event_key[len(prefix) :] if event_key.startswith(prefix) else event_key.rsplit(":", 1)[-1]
                if message_id:
                    self._message_index[message_id] = record_id

    def _scene_of(self, conversation_key: str) -> str:
        parts = conversation_key.split(":")
        if len(parts) >= 4 and parts[3] in {"private", "group"}:
            return parts[3]
        return ""

    def _compute_privacy_fingerprint(self) -> str:
        payload = json.dumps(self.settings.privacy.public_groups, ensure_ascii=False, sort_keys=True)
        return sha256_hex(payload)

    def _bump_privacy_revision(self) -> None:
        self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
        self._invalidate_prompts()
        self._dirty_all_conversations()
        if self._writer_held:
            try:
                self._commit_card()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("privacyRevision 落盘失败，将在下次 tick 重试：%s", exc)
        LOGGER.info("privacyRevision=%s，全部派生 prompt 已失效", self.card["privacyRevision"])

    def _dirty_all_conversations(self) -> None:
        for conversation_key in list(self._index):
            self._dirty.add(conversation_key)
        for conversation_key in self.settings.privacy.public_groups:
            self._dirty.add(conversation_key)

    def _invalidate_prompts(self) -> None:
        self._prompt_cache.clear()
        if self.paths.prompts_dir.exists():
            for child in self.paths.prompts_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

    # ------------------------------------------------------------------
    # prompt 缓存与渲染
    # ------------------------------------------------------------------

    def _prompt_dir(self, conversation_key: str) -> Path:
        return self.paths.prompts_dir / sha256_hex(conversation_key)[:32]

    def _read_prompt_cached(self, conversation_key: str) -> str | None:
        directory = self._prompt_dir(conversation_key)
        memory_path = directory / "memory.md"
        manifest_path = directory / "manifest.json"
        signature = file_signature(memory_path)
        if signature is None:
            return None
        cached = self._prompt_cache.get(conversation_key)
        manifest = read_json(manifest_path, None)
        if not isinstance(manifest, dict):
            return None
        current_revision = int(self.card.get("cardRevision") or 0)
        current_privacy = int(self.card.get("privacyRevision") or 0)
        if int(manifest.get("cardRevision") or 0) != current_revision or int(manifest.get("privacyRevision") or 0) != current_privacy:
            self._dirty.add(conversation_key)
            return None
        if cached is not None and cached.get("signature") == signature and cached.get("manifest") == manifest:
            return str(cached.get("text") or "")
        try:
            with open(memory_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
        self._prompt_cache[conversation_key] = {"signature": signature, "manifest": manifest, "text": text}
        return text

    def _render_prompt(self, conversation_key: str, *, write: bool = True) -> str | None:
        record_ids = self._index.get(conversation_key)
        if record_ids is None:
            return None
        records = [self.card["records"][record_id] for record_id in record_ids if record_id in self.card.get("records", {})]
        result = render_conversation_prompt(
            self.card,
            conversation_key,
            self.settings,
            erased=self.barrier,
            now=utc_now(),
            budget_chars=self.settings.memory.inject_max_chars,
        )
        text = str(result["text"] or "")
        manifest = {
            "conversationKey": conversation_key,
            "cardRevision": int(self.card.get("cardRevision") or 0),
            "privacyRevision": int(self.card.get("privacyRevision") or 0),
            "memoryIds": result["memoryIds"],
            "sections": result["sections"],
            "chars": len(text),
            "truncated": bool(result["truncated"]),
            "contentHash": f"sha256:{sha256_hex(text)}",
            "renderedAt": iso_utc(),
            "recordCount": len(records),
        }
        if write:
            directory = self._prompt_dir(conversation_key)
            ensure_dir(directory)
            atomic_write_text(directory / "memory.md", text)
            atomic_write_json(directory / "manifest.json", manifest)
            signature = file_signature(directory / "memory.md")
            self._prompt_cache[conversation_key] = {"signature": signature, "manifest": manifest, "text": text}
        return text

    def _render_dirty(self) -> int:
        rendered = 0
        for conversation_key in list(self._dirty):
            try:
                if conversation_key in self._index:
                    if self._render_prompt(conversation_key) is not None:
                        rendered += 1
                elif conversation_key in self.settings.privacy.public_groups:
                    if self._render_prompt(conversation_key) is not None:
                        rendered += 1
                self._dirty.discard(conversation_key)
            except Exception as exc:  # noqa: BLE001
                self._last_render_errors += 1
                LOGGER.warning("渲染会话 prompt 失败（%s）：%s", conversation_key, exc)
        return rendered

    # ------------------------------------------------------------------
    # writer / tick
    # ------------------------------------------------------------------

    def _ensure_writer(self) -> bool:
        if self._writer_held:
            if self.writer_state.still_valid():
                return True
            self._release_writer()
        if not self._writer_lock.acquire():
            return False
        try:
            self.writer_state.take(revision=int(self.card.get("cardRevision") or 0))
            self.writer_state.still_valid()
        except Exception:
            self._writer_lock.release()
            return False
        self._writer_held = True
        self.full.load_index()
        return True

    def _release_writer(self) -> None:
        if not self._writer_held:
            return
        try:
            self.writer_state.release()
        finally:
            self._writer_held = False
            self._writer_lock.release()

    def tick(self, first: bool = False) -> dict[str, Any]:
        """单飞维护：归档、衰减、压缩、渲染、清理。"""

        if not self.settings.general.enabled or not self.settings.memory_enabled:
            return {"skipped": "disabled"}
        if not self._tick_guard.acquire(blocking=False):
            return {"skipped": "tick-in-flight"}
        started = time.monotonic()
        stats: dict[str, Any] = {"first": first}
        try:
            if self._stopped:
                return {"skipped": "stopped"}
            if not self._ensure_writer():
                stats["skipped"] = "writer-lock-busy"
                return stats
            try:
                self.writer_state.assert_valid()
            except StaleWriter:
                self._release_writer()
                return {"skipped": "stale-writer"}
            if first and self.card_store.recovered:
                LOGGER.warning("当前卡已恢复：%s", self.card_store.recovery_note)
                self.card["updatedAt"] = iso_utc()
                self.card_store.save(self.card)
            stats["events"] = self._process_inbox()
            stats["replies"] = self._process_reply_inbox()
            stats["operations"] = self._process_operations()
            stats["maintain"] = self._run_daily_maintenance()
            now_mono = time.monotonic()
            heavy_interval = max(1, int(self.settings.memory.maintain_interval_minutes)) * 60
            if first or now_mono - self._last_heavy_at >= heavy_interval:
                stats["compact"] = self._compact_if_needed()
                stats["prune"] = self._prune_inbox()
                stats["backup"] = self._backup_if_due()
                self._last_heavy_at = now_mono
            stats["rendered"] = self._render_dirty()
            self.storage.status(force=True)
            self._flush_states()
            self._last_tick_at = iso_utc()
            if self.card.get("cardRevision") is not None:
                self.writer_state.record_commit(int(self.card.get("cardRevision") or 0))
            stats["elapsedMs"] = round((time.monotonic() - started) * 1000, 1)
            return stats
        except Exception as exc:  # noqa: BLE001 - tick 失败不得杀进程
            LOGGER.exception("tick 失败：%s", exc)
            stats["error"] = str(exc)
            return stats
        finally:
            self._tick_guard.release()

    def _commit_card(self, *, bump_revision: bool = True) -> None:
        if bump_revision:
            self.card["cardRevision"] = int(self.card.get("cardRevision") or 0) + 1
        self.card["updatedAt"] = iso_utc()
        self.card_store.save(self.card)
        self.full.flush()

    # -- inbox ---------------------------------------------------------

    def _process_inbox(self) -> int:
        checkpoint = self.checkpoint.load()
        shards = checkpoint.setdefault("shards", {})
        processed = 0
        budget = max(1, int(self.settings.memory.max_inbox_batch))
        for shard in list_inbox_shards(self.paths):
            if processed >= budget:
                break
            rel = str(shard.relative_to(self.paths.inbox_dir))
            entry = shards.get(rel) if isinstance(shards.get(rel), dict) else {}
            offset = int(entry.get("offset") or 0)
            try:
                size = shard.stat().st_size
            except OSError:
                continue
            if offset >= size:
                continue
            active = shard.name.startswith(self._ingestor.generation)
            sealed = not active
            cursor = offset
            batch: list[Event] = []
            for end, payload in iter_jsonl(shard, offset, complete_lines_only=not sealed):
                if processed + len(batch) >= budget:
                    break
                cursor = end
                if isinstance(payload, dict):
                    event = Event.from_dict(payload)
                    if event.event_key:
                        batch.append(event)
            if not batch and cursor <= offset:
                continue
            changed = self._apply_events(batch)
            if changed:
                self._commit_card()
            processed += len(batch)
            shards[rel] = {
                "offset": cursor,
                "lastEventKey": batch[-1].event_key if batch else entry.get("lastEventKey"),
                "committedRevision": int(self.card.get("cardRevision") or 0),
            }
            checkpoint["committedRevision"] = int(self.card.get("cardRevision") or 0)
            self.checkpoint.save(checkpoint)
        return processed

    def _apply_events(self, events: list[Event]) -> bool:
        changed = False
        for event in events:
            if event.dry_run:
                continue
            if self.barrier.is_erased("", [event.event_key]):
                continue
            record_id = stable_id("mem_", event.event_key, 16)
            if self.barrier.is_erased(record_id, [event.event_key]):
                continue
            existing = self.card["records"].get(record_id)
            if isinstance(existing, dict) and event.event_key in (existing.get("sourceEventKeys") or []):
                continue
            if isinstance(existing, dict):
                continue  # 同一 eventKey 派生的 id 已存在，纯幂等
            record = self._new_record(event, record_id)
            if record is None:
                continue
            if not self.full.append_upsert(record):
                # full 已有该修订，继续提交 card（崩溃重放场景）。
                pass
            self.card["records"][record_id] = record
            self.card["stats"]["created"] = int(self.card["stats"].get("created") or 0) + 1
            self._index_record(record)
            self._index_message_record(record)
            self._dirty.add(event.conversation_key)
            self._remember_session(event)
            changed = True
        return changed

    def _index_message_record(self, record: dict[str, Any]) -> None:
        conversation_key = str(record.get("conversationKey") or "")
        record_id = str(record.get("id") or "")
        for event_key in record.get("sourceEventKeys") or []:
            prefix = f"{conversation_key}:"
            message_id = event_key[len(prefix) :] if event_key.startswith(prefix) else event_key.rsplit(":", 1)[-1]
            if message_id:
                self._message_index[message_id] = record_id

    def _new_record(self, event: Event, record_id: str) -> dict[str, Any] | None:
        text = event.text.strip()
        if not text:
            return None
        first_owner = False
        first_owner_at = self.card.get("firstOwnerAt")
        if event.is_owner and not event.is_group and not first_owner_at:
            self.card["firstOwnerAt"] = event.timestamp
            first_owner_at = event.timestamp
            first_owner = True

        is_first_record = not any(
            isinstance(record, dict) and str(record.get("conversationKey") or "") == event.conversation_key
            for record in self.card["records"].values()
        )
        scored = score_event(
            text=text,
            is_owner=event.is_owner,
            is_group=event.is_group,
            is_direct=event.is_direct_to_bot,
            is_first_record=is_first_record,
        )
        record = normalize_record({})
        record.update(
            {
                "id": record_id,
                "revision": 1,
                "archiveKey": f"{record_id}:1",
                "sourceEventKeys": [event.event_key],
                "platform": event.platform,
                "accountId": event.account_id,
                "routeScope": event.route_scope,
                "conversationKey": event.conversation_key,
                "source": event.source,
                "scope": self._scope_for_event(event),
                "kind": scored["kind"],
                "title": derive_title(
                    is_owner=event.is_owner,
                    is_group=event.is_group,
                    sender_name=event.sender_name,
                    text=text,
                ),
                "text": text,
                "importance": scored["importance"],
                "importanceReasons": scored["reasons"],
                "pinned": scored["pinned"],
                "clarity": "vivid",
                "createdAt": event.timestamp,
                "archivedRevision": 1,
            }
        )
        if event.is_owner and not event.is_group:
            first_at = parse_iso(first_owner_at) or parse_iso(event.timestamp) or utc_now()
            event_at = parse_iso(event.timestamp) or utc_now()
            days = max(0, int((event_at - first_at).total_seconds()) // 86400)
            record["initial"] = compute_initial(record_id, days)
            if first_owner:
                record["pinned"] = True
                record["importance"] = 100
                record["importanceReasons"] = list(record["importanceReasons"]) + ["初次相遇"]
                record["kind"] = "first"
        return record

    @staticmethod
    def _scope_for_event(event: Event) -> str:
        if event.is_owner and not event.is_group:
            return "owner"
        if event.is_group:
            return "group"
        return "private"

    # -- botReplies ----------------------------------------------------

    def _process_reply_inbox(self) -> int:
        path = self.paths.reply_inbox
        if not path.exists():
            return 0
        size = path.stat().st_size
        if self._reply_offset > size:
            self._reply_offset = 0
        entries: list[dict[str, Any]] = []
        cursor = self._reply_offset
        for end, payload in iter_jsonl(path, self._reply_offset, complete_lines_only=True):
            cursor = end
            if isinstance(payload, dict):
                entries.append(payload)
        if not entries:
            return 0
        applied = 0
        for entry in entries:
            try:
                if self._apply_bot_reply(entry):
                    applied += 1
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("处理 botReply 失败：%s", exc)
        self._reply_offset = cursor
        atomic_write_json(self.paths.state_dir / "reply-cursor.json", {"offset": cursor})
        if applied:
            self._commit_card()
        return applied

    def _apply_bot_reply(self, entry: dict[str, Any]) -> bool:
        text = normalize_text(str(entry.get("text") or ""))
        if not text:
            return False
        session_id = str(entry.get("sessionId") or "")
        reply_message_id = str(entry.get("replyMessageId") or "")
        message_id = str(entry.get("messageId") or "")
        conversation_key = str(entry.get("conversationKey") or "")
        if not conversation_key and session_id:
            conversation_key = self.session_map.get(session_id, "")
        target_id = self._message_index.get(reply_message_id) if reply_message_id else ""
        if not target_id:
            turn = self._turn_by_reply.get(reply_message_id) if reply_message_id else None
            if turn is not None:
                for candidate in turn.memory_ids:
                    if candidate in self.card["records"]:
                        target_id = candidate
                        break
        if not target_id and conversation_key:
            target_id = self._latest_record_for(conversation_key, max_age_minutes=self.settings.memory.pending_reply_ttl_minutes)
        if target_id and target_id in self.card["records"]:
            record = self.card["records"][target_id]
            replies = [item for item in record.get("botReplies") or [] if isinstance(item, dict)]
            if any(str(item.get("messageId") or "") == message_id and message_id for item in replies):
                return False
            replies.append({"text": text, "at": str(entry.get("at") or iso_utc()), "messageId": message_id, "status": "sent"})
            record["botReplies"] = replies
            record["revision"] = int(record.get("revision") or 0) + 1
            record["archiveKey"] = f"{target_id}:{record['revision']}"
            record["lastBotReplyAt"] = str(entry.get("at") or iso_utc())
            record["archivedRevision"] = int(record["revision"])
            self.full.append_upsert(record)
            self._dirty.add(str(record.get("conversationKey") or conversation_key))
            return True

        # 无法关联原入站回合：写独立 qq-self，不拼到最近一条主人记忆。
        if not conversation_key:
            return False
        record_id = stable_id("mem_", f"{conversation_key}:bot:{message_id or uuid.uuid4().hex[:8]}", 16)
        if record_id in self.card["records"]:
            return False
        record = normalize_record({})
        record.update(
            {
                "id": record_id,
                "revision": 1,
                "archiveKey": f"{record_id}:1",
                "sourceEventKeys": [f"{conversation_key}:{message_id or record_id}"],
                "conversationKey": conversation_key,
                "source": "qq-self",
                "scope": "group" if ":group:" in conversation_key else ("owner" if self._is_owner_conversation(conversation_key) else "private"),
                "kind": "qq-self",
                "title": derive_title(is_owner=False, is_group=":group:" in conversation_key, sender_name="", text=text),
                "text": text,
                "importance": 40,
                "importanceReasons": ["机器人出站", "未关联原入站回合"],
                "clarity": "vivid",
                "createdAt": str(entry.get("at") or iso_utc()),
                "archivedRevision": 1,
            }
        )
        self.full.append_upsert(record)
        self.card["records"][record_id] = record
        self._index_record(record)
        self._dirty.add(conversation_key)
        return True

    def _is_owner_conversation(self, conversation_key: str) -> bool:
        parts = conversation_key.split(":")
        return len(parts) >= 5 and parts[3] == "private" and parts[4] == self.settings.general.owner_qq

    def _latest_record_for(self, conversation_key: str, *, max_age_minutes: int) -> str:
        best_id = ""
        best_time = 0.0
        cutoff = time.time() - max_age_minutes * 60
        for record_id in self._index.get(conversation_key) or []:
            record = self.card["records"].get(record_id)
            if not isinstance(record, dict):
                continue
            created = parse_iso(record.get("createdAt"))
            if not created:
                continue
            if created.timestamp() < cutoff:
                continue
            if created.timestamp() > best_time:
                best_time = created.timestamp()
                best_id = record_id
        return best_id

    # -- operations ----------------------------------------------------

    def _process_operations(self) -> int:
        processed = 0
        for op in self.operations.list_recent(50):
            if str(op.get("status") or "") not in {"pending", "queued"}:
                continue
            op_type = str(op.get("type") or "")
            if op_type != "erase":
                continue
            op_id = str(op.get("opId") or "")
            payload = op.get("payload") or {}
            try:
                result = self.perform_erase(str(payload.get("memoryId") or ""), operator=str(payload.get("operator") or "admin"))
                self.operations.update(op_id, "complete" if result.get("success") else "incomplete", result=result)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("物理擦除失败：%s", exc)
                self.operations.update(op_id, "incomplete", result={"success": False, "error": str(exc)})
            processed += 1
        return processed

    # -- daily maintenance --------------------------------------------

    def _run_daily_maintenance(self) -> dict[str, Any]:
        tz = resolve_timezone(self.settings.general.timezone)
        now = utc_now()
        local = local_datetime(now, tz)
        if local.hour < max(0, min(23, int(self.settings.memory.daily_maintain_hour))):
            return {"skipped": "before-hour"}
        today = local_day(now, tz)
        last = str(self.card.get("lastDailyMaintainDate") or "")
        if last == today:
            return {"skipped": "done"}
        missed = 1
        if last:
            try:
                last_day = datetime.strptime(last, "%Y-%m-%d")
                missed = max(1, (local.replace(tzinfo=None) - last_day).days)
            except ValueError:
                missed = 1
        missed = min(missed, max(1, int(self.settings.memory.catchup_max_days)))
        changed = 0
        for index in range(missed):
            at = now - timedelta(days=missed - 1 - index)
            changed += self._apply_decay_step(at)
        self.card["lastDailyMaintainDate"] = today
        self.card["stats"]["lastMaintainAt"] = iso_utc()
        self._commit_card()
        return {"days": missed, "changed": changed}

    def _apply_decay_step(self, at: datetime) -> int:
        changed = 0
        for record_id, record in list(self.card["records"].items()):
            if not isinstance(record, dict):
                continue
            current = str(record.get("clarity") or "vivid")
            target = target_clarity(record, now=at, settings=self.settings)
            new = step_clarity(current, target)
            if new == current:
                continue
            conversation_key = str(record.get("conversationKey") or "")
            if new == "forgotten":
                self.card["records"].pop(record_id, None)
                self._unindex_record(record_id)
                self.card["stats"]["forgotten"] = int(self.card["stats"].get("forgotten") or 0) + 1
            else:
                record["clarity"] = new
                record["decaySteps"] = int(record.get("decaySteps") or 0) + 1
                if is_protected(record, self.settings.memory.min_owner_importance):
                    record["clarity"] = "vivid" if record.get("pinned") or record.get("initial") or record.get("kind") == "first" else record["clarity"]
            if conversation_key:
                self._dirty.add(conversation_key)
            changed += 1
        return changed

    # -- capacity ------------------------------------------------------

    def _card_markdown_chars(self) -> int:
        return len(render_card_markdown(self.card, self.settings))

    def _compact_if_needed(self) -> dict[str, Any]:
        memory = self.settings.memory
        changed = 0
        before = self._card_markdown_chars() if memory.max_chars > 0 else 0
        if memory.max_chars > 0 and memory.auto_compact and before > memory.compact_target_chars:
            target = max(1, memory.compact_target_chars)
            current = before
            candidates = [
                record
                for record in self.card["records"].values()
                if isinstance(record, dict) and not is_protected(record, memory.min_owner_importance)
            ]
            while current > target and candidates:
                candidates.sort(
                    key=lambda item: sort_priority(item, now=utc_now(), tz=resolve_timezone(self.settings.general.timezone))
                )
                record = candidates.pop(0)
                current_clarity = str(record.get("clarity") or "vivid")
                if current_clarity in {"trace", "forgotten"}:
                    continue
                lengths = card_block_lengths(record, self.settings)
                next_clarity = step_clarity(current_clarity, "trace")
                delta = lengths.get(current_clarity, 0) - lengths.get(next_clarity, 0)
                record["clarity"] = next_clarity
                current -= max(0, delta)
                changed += 1
                self._dirty.add(str(record.get("conversationKey") or ""))
            self.card["stats"]["compacted"] = int(self.card["stats"].get("compacted") or 0) + changed
            if current > target:
                self.card["stats"]["capacityWarn"] = {
                    "at": iso_utc(),
                    "chars": current,
                    "target": target,
                    "reason": "仅剩保护记录，无法继续压缩",
                }

        evicted = self._evict_overflow()
        changed += evicted
        if changed:
            self._commit_card()
        after = self._card_markdown_chars() if memory.max_chars > 0 else 0
        return {"changed": changed, "before": before, "after": after, "evicted": evicted}

    def _evict_overflow(self) -> int:
        memory = self.settings.memory
        limit = max(1, int(memory.max_records))
        non_protected = [
            record
            for record in self.card["records"].values()
            if isinstance(record, dict) and not is_protected(record, memory.min_owner_importance)
        ]
        if len(non_protected) <= limit:
            return 0
        tz = resolve_timezone(self.settings.general.timezone)
        non_protected.sort(key=lambda item: sort_priority(item, now=utc_now(), tz=tz))
        evicted = 0
        for record in non_protected[: len(non_protected) - limit]:
            record_id = str(record.get("id") or "")
            self.card["records"].pop(record_id, None)
            self._unindex_record(record_id)
            self.card["stats"]["forgotten"] = int(self.card["stats"].get("forgotten") or 0) + 1
            self._dirty.add(str(record.get("conversationKey") or ""))
            evicted += 1
        return evicted

    # -- inbox retention / backups ------------------------------------

    def _prune_inbox(self) -> int:
        checkpoint = self.checkpoint.load()
        shards = checkpoint.get("shards") or {}
        keep_days = max(1, int(self.settings.memory.inbox_retention_days))
        cutoff = utc_now() - timedelta(days=keep_days)
        removed = 0
        for rel, entry in list(shards.items()):
            path = self.paths.inbox_dir / rel
            if not path.exists():
                shards.pop(rel, None)
                continue
            try:
                if int((entry or {}).get("offset") or 0) < path.stat().st_size:
                    continue
            except OSError:
                continue
            try:
                day = datetime.strptime(path.parent.name, "%Y-%m-%d").replace(tzinfo=timezone_of(self.settings))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
                shards.pop(rel, None)
        if removed:
            prune_empty_dirs(self.paths.inbox_dir / "")
            self.checkpoint.save(checkpoint)
        return removed

    def _backup_if_due(self) -> int:
        tz = resolve_timezone(self.settings.general.timezone)
        now = utc_now()
        today = local_day(now, tz)
        daily_dir = self.paths.backups_dir / "daily" / today
        created = 0
        if not daily_dir.exists():
            ensure_dir(daily_dir)
            for name, source in (
                ("card.json", self.paths.card_json),
                ("erased.json", self.paths.erased_json),
                ("checkpoint.json", self.paths.checkpoint_json),
                ("session-map.json", self.paths.session_map),
                ("style-state.json", self.paths.style_state),
            ):
                if source.exists():
                    try:
                        shutil.copy2(source, daily_dir / name)
                        created += 1
                    except OSError:
                        continue
            self._prune_backups(self.paths.backups_dir / "daily", max(1, int(self.settings.memory.backup_daily)))
        if now.weekday() == 6:
            weekly_dir = self.paths.backups_dir / "weekly" / today
            if not weekly_dir.exists():
                ensure_dir(weekly_dir)
                for name, source in (("card.json", self.paths.card_json), ("erased.json", self.paths.erased_json)):
                    if source.exists():
                        try:
                            shutil.copy2(source, weekly_dir / name)
                            created += 1
                        except OSError:
                            continue
                self._prune_backups(self.paths.backups_dir / "weekly", max(1, int(self.settings.memory.backup_weekly)))
        return created

    def _prune_backups(self, root: Path, keep: int) -> None:
        if not root.exists():
            return
        dirs = sorted([child for child in root.iterdir() if child.is_dir()], key=lambda item: item.name)
        for old in dirs[:-keep] if len(dirs) > keep else []:
            shutil.rmtree(old, ignore_errors=True)

    def _flush_states(self) -> None:
        with self._state_lock:
            self._prune_state_maps()
            atomic_write_json(self.paths.session_map, self.session_map)
            atomic_write_json(self.paths.style_state, self.style_state)
            atomic_write_json(self.paths.exposure_state, self.exposure)
            atomic_write_json(self.paths.recognition_state, self.recognition.state)
            self.llm_ledger.save()

    def _prune_state_maps(self) -> None:
        """限制会话/识别/曝光状态增长，避免长期运行后状态文件无限膨胀。"""

        session_limit = 20000
        if len(self.session_map) > session_limit:
            for key in list(self.session_map)[: len(self.session_map) - session_limit]:
                self.session_map.pop(key, None)
        recognition_limit = 3000
        if len(self.recognition.state) > recognition_limit:
            ordered = sorted(
                self.recognition.state.items(),
                key=lambda item: str((item[1] or {}).get("updatedAt") or ""),
            )
            for key, _value in ordered[: len(ordered) - recognition_limit]:
                self.recognition.state.pop(key, None)
        live_ids = set(self.card.get("records", {}))
        if len(self.exposure) > len(live_ids) * 4 + 1000:
            for record_id in list(self.exposure):
                if record_id not in live_ids:
                    self.exposure.pop(record_id, None)

    # ------------------------------------------------------------------
    # 管理操作（供 admin API 调用）
    # ------------------------------------------------------------------

    @_mutation_guarded
    def snapshot(self) -> dict[str, Any]:
        """返回状态快照。"""

        records = [record for record in self.card.get("records", {}).values() if isinstance(record, dict)]
        clarity_counts: dict[str, int] = {}
        for record in records:
            clarity = str(record.get("clarity") or "vivid")
            clarity_counts[clarity] = clarity_counts.get(clarity, 0) + 1
        full_stats = read_json(self.paths.full_stats, {})
        return {
            "cardRevision": int(self.card.get("cardRevision") or 0),
            "privacyRevision": int(self.card.get("privacyRevision") or 0),
            "records": len(records),
            "protected": sum(1 for record in records if is_protected(record, self.settings.memory.min_owner_importance)),
            "clarity": clarity_counts,
            "firstOwnerAt": self.card.get("firstOwnerAt"),
            "lastDailyMaintainDate": self.card.get("lastDailyMaintainDate"),
            "cardChars": self._card_markdown_chars() if self.settings.memory.max_chars > 0 else None,
            "capacityWarn": self.card["stats"].get("capacityWarn"),
            "full": full_stats,
            "storage": self.storage.status(),
            "writer": {"held": self._writer_held, "valid": self.writer_state.still_valid() if self._writer_held else False},
            "lastTickAt": self._last_tick_at,
            "settings": self.settings.summary(),
            "styles": sorted(self.styles),
            "llm": self.llm_ledger.usage(),
            "operations": self.operations.list_recent(10),
        }

    @_mutation_guarded
    def preview_prompt(self, conversation_key: str, *, budget_chars: int | None = None) -> dict[str, Any]:
        """预览指定会话的注入正文；禁止用全局 prompt 冒充。"""

        if not conversation_key:
            return {"success": False, "error": "必须指定 conversation_key"}
        if conversation_key not in self._index:
            return {"success": False, "error": "该会话当前没有可见记忆"}
        result = render_conversation_prompt(
            self.card,
            conversation_key,
            self.settings,
            erased=self.barrier,
            budget_chars=budget_chars or self.settings.memory.inject_max_chars,
        )
        return {
            "success": True,
            "conversationKey": conversation_key,
            "text": result["text"],
            "memoryIds": result["memoryIds"],
            "chars": result["chars"],
            "recordCount": len(self._index.get(conversation_key, [])),
        }

    @_mutation_guarded
    def list_records(
        self,
        *,
        conversation_key: str = "",
        clarity: str = "",
        limit: int = 50,
        offset: int = 0,
        include_forgotten: bool = False,
    ) -> dict[str, Any]:
        """列当前卡记录（分页）。"""

        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        records = [
            record
            for record in self.card.get("records", {}).values()
            if isinstance(record, dict)
            and (not conversation_key or record.get("conversationKey") == conversation_key)
            and (not clarity or record.get("clarity") == clarity)
            and (include_forgotten or record.get("clarity") != "forgotten")
        ]
        records.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        page = records[offset : offset + limit]
        items = []
        for record in page:
            payload = self._public_record(record)
            text = str(payload.get("text") or "")
            if len(text) > 600:
                payload["text"] = text[:600] + "…"
                payload["textTruncated"] = True
            items.append(payload)
        return {
            "total": len(records),
            "page": offset // limit + 1,
            "pageSize": limit,
            "items": items,
        }

    @_mutation_guarded
    def get_record(self, record_id: str) -> dict[str, Any] | None:
        """读取单条记录（管理台摘要）。"""

        record = self.card.get("records", {}).get(record_id)
        if not isinstance(record, dict):
            return None
        payload = self._public_record(record)
        payload["botReplies"] = record.get("botReplies") or []
        payload["sourceEventKeys"] = record.get("sourceEventKeys") or []
        payload["importanceReasons"] = record.get("importanceReasons") or []
        payload["mergedFrom"] = record.get("mergedFrom") or []
        return payload

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"),
            "revision": record.get("revision"),
            "conversationKey": record.get("conversationKey"),
            "scope": record.get("scope"),
            "kind": record.get("kind"),
            "title": record_title(record),
            "text": record.get("text"),
            "importance": record.get("importance"),
            "clarity": record.get("clarity"),
            "pinned": record.get("pinned"),
            "initial": record.get("initial"),
            "createdAt": record.get("createdAt"),
            "eventDate": record.get("eventDate"),
            "recallCount": record.get("recallCount"),
            "lastRecalledAt": record.get("lastRecalledAt"),
        }

    @_mutation_guarded
    def correct_record(
        self,
        record_id: str,
        *,
        text: str = "",
        title: str = "",
        reason: str = "",
        operator: str = "admin",
        scope: str = "",
        allowed_conversation_keys: list[str] | None = None,
        confirm_widen: bool = False,
    ) -> dict[str, Any]:
        """纠错：revision+1，保留旧修订在 full 中。"""

        from .privacy import validate_public_grant

        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id)
            if not isinstance(record, dict):
                return {"success": False, "error": "记录不存在"}
            new_scope = scope or str(record.get("scope") or "private")
            old_scope = str(record.get("scope") or "private")
            old_allowed = [str(item) for item in record.get("allowedConversationKeys") or []]
            if new_scope in {"group", "public"} and old_scope in {"owner", "private"} and not confirm_widen:
                return {"success": False, "error": "放宽 scope 需要主人二次确认并记录审计（confirm_widen=true）"}

            new_allowed = list(old_allowed)
            if allowed_conversation_keys is not None:
                new_allowed = [
                    str(item)
                    for item in allowed_conversation_keys
                    if str(item) in set(self.settings.privacy.public_groups)
                ]
            if new_scope == "public":
                ok, error = validate_public_grant(
                    scope="public",
                    allowed_targets=new_allowed,
                    authorized_by=operator,
                    authorized_at=iso_utc(),
                    settings=self.settings,
                )
                if not ok:
                    return {"success": False, "error": error}

            sanitized = normalize_text(redact_secrets(text)[0]) if text else str(record.get("text") or "")
            if not sanitized.strip():
                return {"success": False, "error": "正文不能为空"}
            record["text"] = sanitized
            if title:
                record["title"] = normalize_text(title)
            record["changeReason"] = normalize_text(reason)[:500]
            record["correctedFrom"] = int(record.get("revision") or 0)
            record["revision"] = int(record.get("revision") or 0) + 1
            record["archiveKey"] = f"{record_id}:{record['revision']}"
            record["archivedRevision"] = int(record["revision"])
            record["scope"] = new_scope
            record["allowedConversationKeys"] = new_allowed
            if allowed_conversation_keys is not None or new_scope == "public":
                record["authorizedBy"] = operator
                record["authorizedAt"] = iso_utc()
            scope_changed = new_scope != old_scope or new_allowed != old_allowed
            self.full.append_upsert(record)
            self._unindex_record(record_id)
            self._index_record(record)
            self._dirty.add(str(record.get("conversationKey") or ""))
            if scope_changed:
                self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
                self._invalidate_prompts()
                self._dirty_all_conversations()
            self._commit_card()
            self.operations.append(
                op_type="correct",
                status="complete",
                payload={"memoryId": record_id, "operator": operator},
                result={"revision": record["revision"]},
            )
            return {"success": True, "record": self._public_record(record)}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("纠错失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def forget_record(self, record_id: str, *, operator: str = "admin") -> dict[str, Any]:
        """忘记正文：从当前卡移除并计 forgotten，全量历史保留。"""

        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id)
            if not isinstance(record, dict):
                return {"success": False, "error": "记录不存在"}
            self.card["records"].pop(record_id, None)
            self._unindex_record(record_id)
            self.card["stats"]["forgotten"] = int(self.card["stats"].get("forgotten") or 0) + 1
            self._dirty.add(str(record.get("conversationKey") or ""))
            self._commit_card()
            self.operations.append(
                op_type="forget",
                status="complete",
                payload={"memoryId": record_id, "operator": operator},
                result={"forgotten": True},
            )
            return {"success": True, "forgotten": True}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("忘记失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def preview_public(self, record_id: str, targets: list[str] | None = None) -> dict[str, Any]:
        """放宽 scope 前的跨域脱敏预览。"""

        from .privacy import validate_public_grant
        from .render import render_record_block

        record = self.card.get("records", {}).get(record_id)
        if not isinstance(record, dict):
            return {"success": False, "error": "记录不存在"}
        target_list = [str(item) for item in (targets or [])]
        ok, error = validate_public_grant(
            scope="public",
            allowed_targets=target_list,
            authorized_by="preview",
            authorized_at=iso_utc(),
            settings=self.settings,
        )
        if not ok:
            return {"success": False, "error": error}
        preview = render_record_block(
            record,
            str(record.get("clarity") or "vivid"),
            self.settings,
            cross_domain=True,
        )
        return {
            "success": True,
            "memoryId": record_id,
            "targets": target_list,
            "preview": preview,
            "previewHash": sha256_hex(preview),
            "note": "这是群/公开渲染下的脱敏预览；确认后会写审计并递增 privacyRevision",
        }

    @_mutation_guarded
    def grant_public(
        self,
        record_id: str,
        *,
        targets: list[str],
        operator: str = "admin",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """把记录放宽为 public：仅主人可执行，需二次确认并写审计。"""

        from .privacy import validate_public_grant

        if not confirm:
            return {"success": False, "error": "放宽 scope 属于高风险操作，需要 confirm=true 二次确认"}
        ok, error = validate_public_grant(
            scope="public",
            allowed_targets=list(targets),
            authorized_by=operator,
            authorized_at=iso_utc(),
            settings=self.settings,
        )
        if not ok:
            return {"success": False, "error": error}
        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id)
            if not isinstance(record, dict):
                return {"success": False, "error": "记录不存在"}
            record["scope"] = "public"
            record["allowedConversationKeys"] = [str(item) for item in targets]
            record["authorizedBy"] = operator
            record["authorizedAt"] = iso_utc()
            record["revision"] = int(record.get("revision") or 0) + 1
            record["archiveKey"] = f"{record_id}:{record['revision']}"
            record["archivedRevision"] = int(record["revision"])
            self.full.append_upsert(record)
            self._unindex_record(record_id)
            self._index_record(record)
            self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
            self._invalidate_prompts()
            for conversation_key in targets:
                self._dirty.add(str(conversation_key))
            self._dirty.add(str(record.get("conversationKey") or ""))
            self._commit_card()
            self.operations.append(
                op_type="grant_public",
                status="complete",
                payload={"memoryId": record_id, "operator": operator, "targets": list(targets)},
                result={"revision": record["revision"]},
            )
            return {"success": True, "record": self._public_record(record)}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("public 授权失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def revoke_public(self, record_id: str, *, operator: str = "admin") -> dict[str, Any]:
        """撤销 public 授权：立即失效并递增 privacyRevision。"""

        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id)
            if not isinstance(record, dict):
                return {"success": False, "error": "记录不存在"}
            source = str(record.get("conversationKey") or "")
            record["allowedConversationKeys"] = []
            record["authorizedBy"] = None
            record["authorizedAt"] = None
            record["scope"] = "group" if ":group:" in source else (
                "owner" if source.endswith(f":private:{self.settings.general.owner_qq}") else "private"
            )
            record["revision"] = int(record.get("revision") or 0) + 1
            record["archiveKey"] = f"{record_id}:{record['revision']}"
            record["archivedRevision"] = int(record["revision"])
            self.full.append_upsert(record)
            self._unindex_record(record_id)
            self._index_record(record)
            self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
            self._invalidate_prompts()
            self._dirty.add(source)
            self._commit_card()
            self.operations.append(
                op_type="revoke_public",
                status="complete",
                payload={"memoryId": record_id, "operator": operator},
                result={"revision": record["revision"]},
            )
            return {"success": True, "record": self._public_record(record)}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("public 撤销失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def logical_delete(self, record_id: str, *, operator: str = "admin") -> dict[str, Any]:
        """逻辑删除：移除并写 erased 屏障 + full delete。"""

        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id, {})
            erasure_id = new_id("era_")
            event_keys = [str(item) for item in (record.get("sourceEventKeys") or [])]
            self.barrier.add(erasure_id=erasure_id, record_id=record_id, event_keys=event_keys)
            revision = int(record.get("revision") or 0) + 1
            self.full.append_delete(record_id, revision, erasure_id, reason="logical_delete")
            self.card["records"].pop(record_id, None)
            self._unindex_record(record_id)
            self.card["stats"]["deleted"] = int(self.card["stats"].get("deleted") or 0) + 1
            self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
            self._dirty.add(str(record.get("conversationKey") or ""))
            self._commit_card()
            self._invalidate_prompts()
            self.operations.append(
                op_type="logical_delete",
                status="complete",
                payload={"memoryId": record_id, "operator": operator},
                result={"erasureId": erasure_id},
            )
            return {"success": True, "erasureId": erasure_id}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("逻辑删除失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def enqueue_physical_erase(self, record_id: str, *, operator: str = "admin") -> dict[str, Any]:
        """物理擦除：先写屏障并立即隐藏，再排队完成全副本清理。"""

        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        try:
            self.writer_state.assert_valid()
            record = self.card.get("records", {}).get(record_id, {})
            erasure_id = new_id("era_")
            event_keys = [str(item) for item in (record.get("sourceEventKeys") or [])]
            self.barrier.add(erasure_id=erasure_id, record_id=record_id, event_keys=event_keys)
            revision = int(record.get("revision") or 0) + 1
            self.full.append_delete(record_id, revision, erasure_id, reason="physical_erase")
            self.card["records"].pop(record_id, None)
            self._unindex_record(record_id)
            self.card["stats"]["deleted"] = int(self.card["stats"].get("deleted") or 0) + 1
            self.card["privacyRevision"] = int(self.card.get("privacyRevision") or 0) + 1
            self._dirty.add(str(record.get("conversationKey") or ""))
            self._commit_card()
            self._invalidate_prompts()
            op_id = self.operations.append(
                op_type="erase",
                status="pending",
                payload={"memoryId": record_id, "erasureId": erasure_id, "eventKeys": event_keys, "operator": operator},
            )
            return {"success": True, "operationId": op_id, "erasureId": erasure_id, "status": "pending"}
        except StaleWriter:
            self._release_writer()
            return {"success": False, "error": "stale writer"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("物理擦除登记失败：%s", exc)
            return {"success": False, "error": str(exc)}

    @_mutation_guarded
    def perform_erase(self, record_id: str, *, operator: str = "admin") -> dict[str, Any]:
        """执行全副本物理擦除；任何一步失败都保持 incomplete。"""

        if not record_id:
            return {"success": False, "error": "memoryId 为空"}
        if not self._ensure_writer():
            return {"success": False, "error": "writer busy"}
        steps: list[str] = []
        target_hashes: set[str] = set()
        for entry in self.barrier.entries.values():
            if isinstance(entry, dict) and entry.get("idHash") == sha256_hex(record_id)[:32]:
                target_hashes.update(str(item) for item in entry.get("keyHashes") or [])
        try:
            self.writer_state.assert_valid()
            self.full.compact(lambda payload: self._keep_after_erase(payload, record_id, target_hashes))
            steps.append("full.compact")
            self._rewrite_inbox_for_erase(record_id, target_hashes)
            steps.append("inbox.rewrite")
            self._rewrite_operations_for_erase(record_id)
            steps.append("operations.rewrite")
            self._rewrite_backups_for_erase(record_id)
            steps.append("backups.rewrite")
            self._invalidate_prompts()
            steps.append("prompts.invalidate")
            return {"success": True, "memoryId": record_id, "steps": steps, "operator": operator}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("物理擦除未完成：%s", exc)
            return {"success": False, "error": str(exc), "steps": steps, "incomplete": True}

    @staticmethod
    def _keep_after_erase(payload: dict[str, Any], record_id: str, target_hashes: set[str]) -> bool:
        if str(payload.get("id") or "") == record_id:
            return False
        record = payload.get("record")
        if isinstance(record, dict):
            if str(record.get("id") or "") == record_id:
                return False
            keys = {sha256_hex(str(key))[:32] for key in record.get("sourceEventKeys") or []}
            if keys & target_hashes:
                return False
            if record_id in {str(item) for item in record.get("mergedFrom") or []}:
                return False
        return True

    def _rewrite_inbox_for_erase(self, record_id: str, target_hashes: set[str]) -> None:
        candidates = [
            path
            for path in self.paths.inbox_dir.rglob("*.jsonl")
            if path.name != "dedupe.jsonl"
        ]
        for shard in candidates:
            rel = str(shard.relative_to(self.paths.inbox_dir))
            in_checkpoint = rel.split("/", 1)[0] != "dry"
            tmp = shard.with_name(f".erase-{uuid.uuid4().hex[:8]}.jsonl")
            with open(shard, "rb") as source, open(tmp, "wb") as target:
                for line in source:
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    event_key = str(payload.get("eventKey") or "")
                    event_key_hash = sha256_hex(event_key)[:32]
                    text = str(payload.get("text") or "")
                    if event_key_hash in target_hashes or record_id in text:
                        continue
                    target.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, shard)
            if not in_checkpoint:
                continue
            checkpoint = self.checkpoint.load()
            shards = checkpoint.get("shards") or {}
            if rel in shards:
                shards[rel]["offset"] = shard.stat().st_size
                checkpoint["shards"] = shards
                self.checkpoint.save(checkpoint)

    def _rewrite_operations_for_erase(self, record_id: str) -> None:
        def transform(payload: dict[str, Any]) -> dict[str, Any] | None:
            target = payload.get("payload")
            if not isinstance(target, dict):
                return payload
            if str(target.get("memoryId") or "") != record_id:
                return payload
            cleaned = dict(payload)
            cleaned["payload"] = {
                "memoryIdHash": sha256_hex(record_id)[:32],
                "operator": target.get("operator") or "admin",
            }
            result = dict(cleaned.get("result") or {})
            for key in list(result):
                if key not in {"success", "forgotten", "status", "revision", "erasureId"}:
                    result.pop(key, None)
            cleaned["result"] = result
            return cleaned

        self.operations.rewrite(transform)

    def _rewrite_backups_for_erase(self, record_id: str) -> None:
        if not self.paths.backups_dir.exists():
            return
        for root, _dirs, files in os.walk(self.paths.backups_dir):
            for name in files:
                path = Path(root) / name
                if not name.endswith(".json"):
                    continue
                data = read_json(path, None)
                if isinstance(data, dict) and isinstance(data.get("records"), dict):
                    if record_id in data["records"]:
                        data["records"].pop(record_id, None)
                        atomic_write_json(path, data)
                        continue
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if record_id in content:
                    try:
                        path.unlink()
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def default_card_copy() -> dict[str, Any]:
    """避免导入 store.default_card 造成循环。"""

    from .store import default_card

    return default_card()


def timezone_of(settings: Settings):
    return resolve_timezone(settings.general.timezone)


def read_jsonl_entries(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """读取 JSONL 条目并返回 ``(entries, 新 offset)``。"""

    entries: list[dict[str, Any]] = []
    cursor = offset
    for end, payload in iter_jsonl(path, offset):
        cursor = end
        if isinstance(payload, dict):
            entries.append(payload)
    return entries, cursor

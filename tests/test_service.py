from __future__ import annotations


from helpers import group_conversation, inbound, make_service, owner_conversation


def _record_ids(service) -> list[str]:
    return list(service.card["records"].keys())


def test_inbound_filtering_and_first_owner(tmp_path) -> None:
    service = make_service(tmp_path)
    assert service.handle_inbound(inbound()) is True
    assert service.handle_inbound(inbound()) is False  # 同一 eventKey 幂等
    assert service.handle_inbound(
        inbound(message_id="m2", text="今晚一起吃饭吗？我们约定七点见", timestamp="2026-11-07T10:05:00+08:00")
    ) is True
    # 不相关群消息：不采集
    assert service.handle_inbound(
        inbound(
            session="sess-group",
            message_id="g1",
            text="嗯嗯",
            user_id="20002",
            group_id="30001",
            timestamp="2026-11-07T10:06:00+08:00",
        )
    ) is False
    # 带情绪信号的群消息：采集
    assert service.handle_inbound(
        inbound(
            session="sess-group",
            message_id="g2",
            text="今天真的好难过，呜呜呜",
            user_id="20002",
            group_id="30001",
            timestamp="2026-11-07T10:07:00+08:00",
        )
    ) is True

    stats = service.tick(first=True)
    assert stats.get("events", 0) >= 3
    records = list(service.card["records"].values())
    assert len(records) == 3
    first = [record for record in records if record["kind"] == "first"][0]
    assert first["pinned"] is True
    assert first["importance"] == 100
    assert first["scope"] == "owner"
    assert first["initial"]["probability"] in {0.9, 0.55, 0.3, 0.03}
    assert first["sourceEventKeys"]

    group_record = [record for record in records if record["scope"] == "group"][0]
    assert group_record["conversationKey"] == group_conversation("30001")

    assert service.paths.card_json.exists()
    assert service.paths.full_jsonl.exists()
    assert service.paths.checkpoint_json.exists()
    assert service.paths.full_stats.exists()


def test_crash_replay_does_not_duplicate_full_revision(tmp_path) -> None:
    from persona.store import default_card

    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    assert len(service.card["records"]) == 1
    lines_before = service.paths.full_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_before) == 1

    # 模拟：full 已 append、card/checkpoint 未提交后崩溃
    service.card = default_card()
    service._index = {}
    service._message_index = {}
    service.checkpoint.save({"version": 1, "committedRevision": 0, "shards": {}})
    service.tick(first=True)

    assert len(service.card["records"]) == 1
    lines_after = service.paths.full_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_after) == 1


def test_injection_inserts_synthetic_user_item_after_system(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)

    before = service.before_request({"session_id": "sess-owner", "reply_message_id": "m1"})
    modified = before["modified_kwargs"]
    turn_id = modified["reply_tool_args"]["persona_turn_id"]
    assert turn_id

    system_item = {"item_type": "SystemMessageItem", "meta": {"item_id": "s"}, "parts": [{"type": "text", "text": "system"}]}
    user_item = {"item_type": "UserMessageItem", "meta": {"item_id": "u"}, "parts": [{"type": "text", "text": "hi"}]}
    result = service.before_model_request(
        {
            "items": [system_item, user_item],
            "session_id": "sess-owner",
            "reply_tool_args": {"persona_turn_id": turn_id},
        }
    )
    items = result["modified_kwargs"]["items"]
    assert len(items) == 3
    assert items[0]["item_type"] == "SystemMessageItem"
    assert items[1]["item_type"] == "UserMessageItem"
    assert items[2] is user_item
    text = items[1]["parts"][0]["text"]
    assert text.startswith("<persona_data nonce=")
    assert text.rstrip().endswith("</persona_data>")
    assert "今天有点难过" in text
    assert len(text) <= 3600
    assert items[1]["meta"]["item_id"]
    assert items[1]["meta"]["logical_turn_id"] is None
    record_id = _record_ids(service)[0]
    assert service.exposure[record_id]["count"] == 1
    assert service.card["cardRevision"] >= 1


def test_after_send_attaches_bot_reply_and_increments_revision(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    record_id = _record_ids(service)[0]
    assert service.card["records"][record_id]["revision"] == 1

    service.after_send(
        {
            "sent": True,
            "reply_message_id": "m1",
            "message": {
                "session_id": "sess-owner",
                "message_id": "bot-1",
                "processed_plain_text": "抱抱你，别难过",
                "timestamp": "2026-11-07T10:01:00+08:00",
            },
        }
    )
    stats = service.tick()
    assert stats.get("replies", 0) == 1
    record = service.card["records"][record_id]
    assert record["revision"] == 2
    assert record["botReplies"][0]["text"] == "抱抱你，别难过"
    assert record["botReplies"][0]["status"] == "sent"

    lines = service.paths.full_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_after_send_ignores_failed_or_empty(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    service.after_send({"sent": False, "reply_message_id": "m1", "message": {"session_id": "sess-owner"}})
    service.after_send({"sent": True, "reply_message_id": "m1", "message": {"session_id": "sess-owner"}})
    service.tick()
    path = service.paths.reply_inbox
    assert (not path.exists()) or path.read_text(encoding="utf-8").strip() == ""


def test_physical_erase_removes_all_copies(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    record_id = _record_ids(service)[0]

    response = service.enqueue_physical_erase(record_id)
    assert response["success"] is True
    assert response["status"] == "pending"
    assert record_id not in service.card["records"]
    assert service.barrier.is_erased(record_id)

    service.tick()
    operation = service.operations.latest(response["operationId"])
    assert operation is not None
    assert operation["status"] == "complete"

    assert record_id not in service.card["records"]
    assert "今天有点难过" not in service.paths.full_jsonl.read_text(encoding="utf-8")
    assert "今天有点难过" not in service.paths.erased_json.read_text(encoding="utf-8")
    assert "今天有点难过" not in service.paths.operations_jsonl.read_text(encoding="utf-8")

    inbox_text = "".join(
        path.read_text(encoding="utf-8") for path in service.paths.inbox_dir.rglob("*.jsonl")
    )
    assert "今天有点难过" not in inbox_text

    backup_text = ""
    for path in service.paths.backups_dir.rglob("*"):
        if path.is_file():
            backup_text += path.read_text(encoding="utf-8", errors="ignore")
    assert "今天有点难过" not in backup_text


def test_forget_keeps_full_history(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    record_id = _record_ids(service)[0]

    response = service.forget_record(record_id)
    assert response["success"] is True
    assert record_id not in service.card["records"]
    assert "今天有点难过" in service.paths.full_jsonl.read_text(encoding="utf-8")
    assert service.card["stats"]["forgotten"] == 1


def test_storage_gate_blocks_new_writes(tmp_path) -> None:
    import pytest

    from persona.store import StorageFull

    service = make_service(tmp_path)
    guard = service.storage
    guard.used_bytes = lambda force=False: guard.settings.storage.budget_bytes  # type: ignore[method-assign]
    with pytest.raises(StorageFull):
        guard.reserve(1024, "test")
    status = guard.status()
    assert status["ingestAllowed"] is False


def test_public_grant_and_revoke(tmp_path) -> None:
    group_key = group_conversation("30001")
    service = make_service(tmp_path, privacy={"public_groups": [group_key]})
    service.handle_inbound(inbound(text="我的手机号是13812345678，这件事可以公开"))
    service.tick(first=True)
    record_id = _record_ids(service)[0]

    denied = service.grant_public(record_id, targets=[group_key], operator="10001", confirm=False)
    assert denied["success"] is False

    preview = service.preview_public(record_id, [group_key])
    assert preview["success"] is True
    assert "13812345678" not in preview["preview"]

    granted = service.grant_public(record_id, targets=[group_key], operator="10001", confirm=True)
    assert granted["success"] is True
    assert service.card["records"][record_id]["scope"] == "public"
    revision = service.card["records"][record_id]["revision"]
    assert service.card["privacyRevision"] == 1

    from persona.render import render_conversation_prompt

    rendered = render_conversation_prompt(service.card, group_key, service.settings, erased=service.barrier)
    assert "这件事可以公开" in rendered["text"]
    assert "13812345678" not in rendered["text"]

    revoked = service.revoke_public(record_id, operator="10001")
    assert revoked["success"] is True
    assert service.card["records"][record_id]["scope"] == "owner"
    assert service.card["records"][record_id]["revision"] == revision + 1
    assert service.card["privacyRevision"] == 2
    after = render_conversation_prompt(service.card, group_key, service.settings, erased=service.barrier)
    assert after["text"] == ""


def test_missing_card_rebuilds_from_full(tmp_path) -> None:
    from helpers import make_config
    from persona.service import PersonaService

    service = make_service(tmp_path)
    service.handle_inbound(inbound())
    service.tick(first=True)
    assert len(service.card["records"]) == 1

    service.paths.card_json.unlink()
    restarted = PersonaService(service.paths.root, service.paths.runtime, make_config())
    restarted.initialize()
    assert len(restarted.card["records"]) == 1
    assert restarted.card_store.recovered is True
    assert "full.jsonl" in restarted.card_store.recovery_note


def test_correction_creates_revision_and_full_query_prefers_latest(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound(text="明天下午三点见面"))
    service.tick(first=True)
    record_id = _record_ids(service)[0]

    result = service.correct_record(record_id, text="明天下午四点见面", reason="主人更正时间", operator="10001")
    assert result["success"] is True
    record = service.card["records"][record_id]
    assert record["revision"] == 2
    assert record["correctedFrom"] == 1
    assert record["changeReason"] == "主人更正时间"
    full_text = service.paths.full_jsonl.read_text(encoding="utf-8")
    assert "明天下午三点见面" in full_text  # 旧修订保留在 full
    assert "明天下午四点见面" in full_text

    hits = service.full.query_tail(predicate=lambda item: "明天下午四点见面" in str(item.get("text") or ""), limit=10)
    assert hits and hits[0]["revision"] == 2


def test_forgotten_record_still_queryable_in_full(tmp_path) -> None:
    service = make_service(tmp_path)
    service.handle_inbound(inbound(text="这段可以被遗忘"))
    service.tick(first=True)
    record_id = _record_ids(service)[0]
    assert service.forget_record(record_id)["success"] is True
    hits = service.full.query_tail(predicate=lambda item: "这段可以被遗忘" in str(item.get("text") or ""), limit=10)
    assert hits


def test_operation_log_keeps_latest_status(tmp_path) -> None:
    service = make_service(tmp_path)
    op_id = service.operations.append(op_type="erase", status="pending", payload={"memoryId": "mem_x"})
    service.operations.update(op_id, "incomplete", result={"success": False})
    latest = service.operations.latest(op_id)
    assert latest is not None
    assert latest["status"] == "incomplete"
    assert latest["result"]["success"] is False
    assert op_id in service.paths.operations_jsonl.read_text(encoding="utf-8")


def test_owner_conversation_key_matches_identity(tmp_path) -> None:
    from persona.identity import build_conversation

    conversation = build_conversation(inbound(), owner_qq="10001", owner_platform="qq")
    assert conversation.conversation_key == owner_conversation()
    assert conversation.is_owner is True
    assert conversation.chat_type == "private"

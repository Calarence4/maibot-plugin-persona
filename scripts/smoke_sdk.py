import asyncio
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(r"G:\Projects\myQQBot\我想给你完整的一生\maibot-plugin-persona")
sys.path.insert(0, str(PLUGIN_DIR))

from maibot_sdk import PluginContext
from maibot_sdk.context import PluginPaths as SdkPaths

import plugin as entry


async def main() -> None:
    instance = entry.create_plugin()
    print("plugin class:", type(instance).__name__)
    components = instance.get_components()
    print("components:", len(components))
    for item in components:
        print(" -", item.get("type"), item.get("name"), item.get("metadata", {}).get("hook", ""))
    defaults = instance.get_default_config()
    print("default sections:", sorted(defaults))
    print("owner_qq default:", repr(defaults["general"]["owner_qq"]))
    schema = instance.get_webui_config_schema(
        plugin_id="com.calarence.persona",
        plugin_name="人格运行时",
        plugin_version="0.1.0",
        plugin_description="test",
        plugin_author="Calarence",
    )
    print("schema sections:", sorted(schema["sections"]))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        ctx = PluginContext("com.calarence.persona", None, SdkPaths(root / "data", root / "runtime"))
        instance._set_context(ctx)
        instance.set_plugin_config(
            {
                **defaults,
                "plugin": {"config_version": "0.1.0"},
                "general": {
                    **defaults["general"],
                    "owner_qq": "10001",
                },
            }
        )
        await instance.on_load()
        print("loaded:", instance._service is not None)
        if instance._service is not None:
            print("card:", instance._service.card["cardRevision"])
            print("styles:", sorted(instance._service.styles))
            result = await instance.api_self_check()
            print("self_check success:", result.get("success"))
            print("probe_required:", len(result.get("probe_required", [])))
            stats = await instance.api_stats()
            print("stats success:", stats.get("success"), "records:", stats.get("records"))

            message = {
                "platform": "qq",
                "account_id": "888",
                "scope": "*",
                "session_id": "smoke-session",
                "message_id": "smoke-msg-1",
                "timestamp": "2026-11-07T10:00:00+08:00",
                "raw_message": [{"type": "text", "data": "今天有点难过，想抱抱你"}],
                "message_info": {
                    "user_info": {"user_id": "10001", "user_nickname": "主人"},
                    "additional_config": {"self_id": "888", "account_id": "888"},
                },
            }
            await instance.handle_inbound(message=message)
            await asyncio.to_thread(instance._service.tick, True)
            print("records after inbound:", len(instance._service.card["records"]))

            before = await instance.handle_before_request(session_id="smoke-session", reply_message_id="smoke-msg-1")
            turn_args = before["modified_kwargs"]["reply_tool_args"]
            injected = await instance.handle_before_model_request(
                items=[
                    {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "system"}]},
                    {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "hi"}]},
                ],
                session_id="smoke-session",
                reply_tool_args=turn_args,
            )
            print("injected items:", len(injected["modified_kwargs"]["items"]))
            print(
                "injected marker:",
                injected["modified_kwargs"]["items"][1]["parts"][0]["text"].startswith("<persona_data"),
            )

            await instance.handle_after_send(
                sent=True,
                reply_message_id="smoke-msg-1",
                message={
                    "session_id": "smoke-session",
                    "message_id": "smoke-bot-1",
                    "processed_plain_text": "抱抱你",
                    "timestamp": "2026-11-07T10:01:00+08:00",
                },
            )
            await asyncio.to_thread(instance._service.tick, False)
            record = next(iter(instance._service.card["records"].values()))
            print("botReplies:", len(record.get("botReplies") or []), "revision:", record.get("revision"))

            reply = await instance.handle_persona_command(
                message={"message_info": {"user_info": {"user_id": "10001"}}},
                raw_message="/persona stats",
            )
            print("command result:", reply[0], reply[1].splitlines()[0])
            await instance.on_config_update(
                "self",
                {**defaults, "plugin": {"config_version": "0.1.0"}, "general": {**defaults["general"], "owner_qq": "10001"}},
                "0.1.0",
            )
            print("config hot reload ok:", instance._service.settings.general.owner_qq)
        await instance.on_unload()
        print("unloaded")


asyncio.run(main())

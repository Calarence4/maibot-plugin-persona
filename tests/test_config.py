from __future__ import annotations

from persona.config_model import PersonaConfig
from persona.settings import settings_from_mapping


def test_pydantic_defaults_cover_all_sections() -> None:
    defaults = PersonaConfig().model_dump()
    assert defaults["general"]["owner_qq"] == ""
    assert defaults["memory"]["max_chars"] == 60000
    assert defaults["style"]["application_mode"] == "disabled"
    assert defaults["purpose"]["delivery_mode"] == "preview"
    assert defaults["storage"]["budget_bytes"] == 1258291200


def test_settings_reject_multiple_owners() -> None:
    settings = settings_from_mapping({"general": {"owner_qq": "10001,10002"}})
    assert settings.errors
    assert settings.memory_enabled is False


def test_settings_require_owner() -> None:
    settings = settings_from_mapping({})
    assert any("owner_qq" in error for error in settings.errors)
    assert settings.memory_enabled is False


def test_capacity_constraints_are_validated() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "memory": {"max_chars": 1000, "warn_chars": 900, "critical_chars": 1100, "compact_target_chars": 500},
        }
    )
    assert settings.errors


def test_public_groups_require_full_conversation_key() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "privacy": {"public_groups": ["30001"]},
        }
    )
    assert settings.errors
    assert settings.privacy.public_groups == []

    valid = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "privacy": {"public_groups": ["qq:888:*:group:30001"]},
        }
    )
    assert valid.errors == []
    assert valid.privacy.public_groups == ["qq:888:*:group:30001"]


def test_storage_water_levels_are_validated() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "storage": {"warn_ratio": 0.97, "stop_ingest_ratio": 0.9},
        }
    )
    assert settings.errors

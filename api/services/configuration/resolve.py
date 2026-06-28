"""Resolve effective config by merging per-workflow model overrides onto global config."""

from __future__ import annotations

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    REGISTRY,
    ServiceType,
)

# Maps override key → (UserConfiguration field, ServiceType for registry lookup)
_SECTION_MAP: dict[str, ServiceType] = {
    "llm": ServiceType.LLM,
    "tts": ServiceType.TTS,
    "stt": ServiceType.STT,
    "realtime": ServiceType.REALTIME,
}


def _is_blank_api_key(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not any(str(key).strip() for key in value)
    return False


def _normalize_override(override: dict) -> dict:
    normalized = dict(override)
    if "api_key" in normalized and _is_blank_api_key(normalized["api_key"]):
        normalized.pop("api_key")
    return normalized


def _has_non_blank_api_key(override: dict) -> bool:
    return "api_key" in override and not _is_blank_api_key(override.get("api_key"))


def _prune_inactive_incomplete_sections(
    model_overrides: dict, is_realtime: bool | None
) -> dict:
    if is_realtime is None:
        return model_overrides

    inactive_sections = ("stt", "tts") if is_realtime else ("realtime",)
    pruned = dict(model_overrides)
    for section_key in inactive_sections:
        section = pruned.get(section_key)
        if isinstance(section, dict) and not _has_non_blank_api_key(section):
            pruned.pop(section_key, None)
    return pruned


def normalize_model_overrides(
    model_overrides: dict | None, is_realtime: bool | None = None
) -> dict | None:
    """Return model overrides with blank API keys and inactive incomplete services removed."""
    if not model_overrides:
        return model_overrides

    normalized = dict(model_overrides)
    for section_key in _SECTION_MAP:
        section = normalized.get(section_key)
        if isinstance(section, dict):
            normalized[section_key] = _normalize_override(section)

    active_realtime = is_realtime
    if active_realtime is None and isinstance(normalized.get("is_realtime"), bool):
        active_realtime = normalized["is_realtime"]
    normalized = _prune_inactive_incomplete_sections(normalized, active_realtime)
    return normalized


def _build_section_from_override(service_type: ServiceType, override: dict):
    """Construct a typed config object from a raw override dict using the registry."""
    provider = override.get("provider")
    if not provider:
        return None
    registry = REGISTRY.get(service_type, {})
    config_cls = registry.get(provider)
    if config_cls is None:
        return None
    return config_cls(**override)


def resolve_effective_config(
    user_config: UserConfiguration,
    model_overrides: dict | None,
) -> UserConfiguration:
    """Deep-merge workflow model_overrides onto global user config.

    - If model_overrides is None or empty, returns a copy of user_config unchanged.
    - For each section (llm, tts, stt, realtime), if the override contains that key:
      - If the global section is None, construct a new config from the override.
      - If the provider changes, construct a new config from the override.
      - Otherwise, merge override fields onto the existing config (model_copy).
    - is_realtime is a simple boolean override.
    - Sections not in the override are inherited from global unchanged.
    - The original user_config is never mutated.
    """
    if not model_overrides:
        return user_config.model_copy(deep=True)

    effective = user_config.model_copy(deep=True)

    # Effective realtime mode for this resolution: an explicit override wins.
    active_realtime = model_overrides.get("is_realtime", user_config.is_realtime)
    if "is_realtime" in model_overrides:
        effective.is_realtime = model_overrides["is_realtime"]

    # Sections the active mode does not use. A partial override of one of these
    # still merges onto an existing global section (so an explicit tweak is not
    # silently dropped), but an override that would have to build a brand-new
    # section without an API key is skipped: the active mode ignores it anyway,
    # and constructing a keyless section would be incomplete.
    inactive_sections = ("stt", "tts") if active_realtime else ("realtime",)

    for section_key, service_type in _SECTION_MAP.items():
        if section_key not in model_overrides:
            continue

        override = _normalize_override(model_overrides[section_key] or {})
        base = getattr(effective, section_key)
        builds_new_section = base is None or (
            "provider" in override and override["provider"] != base.provider
        )

        if builds_new_section:
            if section_key in inactive_sections and not _has_non_blank_api_key(
                override
            ):
                # Incomplete inactive section with no global base to merge onto.
                continue
            setattr(
                effective,
                section_key,
                _build_section_from_override(service_type, override),
            )
        else:
            # Same provider as the global section — merge override fields onto it.
            setattr(effective, section_key, base.model_copy(update=override))

    return effective

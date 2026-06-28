from datetime import datetime, timedelta
from typing import List, Literal, Optional, TypedDict, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, ValidationError

from api.db import db_client
from api.db.models import (
    UserModel,
)
from api.services.auth.depends import get_user
from api.services.configuration.check_validity import (
    APIKeyStatusResponse,
    UserConfigurationValidator,
)
from api.services.configuration.defaults import DEFAULT_SERVICE_PROVIDERS
from api.services.configuration.defaults import DEFAULT_IS_REALTIME
from api.services.configuration.masking import check_for_masked_keys, mask_user_config
from api.services.configuration.merge import SERVICE_FIELDS, merge_user_configurations
from api.services.configuration.registry import REGISTRY, ServiceType
from api.services.dograh_cloud import dograh_cloud_enabled, require_dograh_cloud
from api.services.livekit.runtime_config import (
    LIVEKIT_SUPPORTED_PROVIDERS,
    is_livekit_runtime,
    livekit_supports_provider,
)
from api.services.mps_service_key_client import mps_service_key_client

router = APIRouter(prefix="/user")


class AuthUserResponse(TypedDict):
    id: int
    is_superuser: bool


class DefaultConfigurationsResponse(TypedDict):
    llm: dict[str, dict]
    tts: dict[str, dict]
    stt: dict[str, dict]
    embeddings: dict[str, dict]
    realtime: dict[str, dict]
    default_providers: dict[str, str]
    default_is_realtime: bool


@router.get("/configurations/defaults")
async def get_default_configurations() -> DefaultConfigurationsResponse:
    def filter_dograh_provider(items: dict[str, dict]) -> dict[str, dict]:
        if dograh_cloud_enabled():
            return items
        return {
            provider: schema
            for provider, schema in items.items()
            if provider != "dograh"
        }

    livekit_runtime = is_livekit_runtime()

    def filter_livekit_providers(
        service: str, items: dict[str, dict]
    ) -> dict[str, dict]:
        """Drop providers the LiveKit worker cannot run, so the UI only offers
        choices that actually work. Falls back to the unfiltered set if nothing
        is supported (should not happen) to avoid an empty dropdown."""
        if not livekit_runtime:
            return items
        supported = LIVEKIT_SUPPORTED_PROVIDERS.get(service)
        if supported is None:
            return items
        filtered = {
            provider: schema
            for provider, schema in items.items()
            if provider in supported
        }
        return filtered or items

    configurations = {
        "llm": filter_livekit_providers("llm", filter_dograh_provider({
            provider: model_cls.model_json_schema()
            for provider, model_cls in REGISTRY[ServiceType.LLM].items()
        })),
        "tts": filter_livekit_providers("tts", filter_dograh_provider({
            provider: model_cls.model_json_schema()
            for provider, model_cls in REGISTRY[ServiceType.TTS].items()
        })),
        "stt": filter_livekit_providers("stt", filter_dograh_provider({
            provider: model_cls.model_json_schema()
            for provider, model_cls in REGISTRY[ServiceType.STT].items()
        })),
        "embeddings": {
            provider: model_cls.model_json_schema()
            for provider, model_cls in REGISTRY[ServiceType.EMBEDDINGS].items()
        },
        "realtime": filter_livekit_providers("realtime", {
            provider: model_cls.model_json_schema()
            for provider, model_cls in REGISTRY[ServiceType.REALTIME].items()
        }),
        "default_providers": DEFAULT_SERVICE_PROVIDERS,
        "default_is_realtime": DEFAULT_IS_REALTIME,
    }
    return configurations


@router.get("/auth/user")
async def get_auth_user(
    user: UserModel = Depends(get_user),
) -> AuthUserResponse:
    return {
        "id": user.id,
        "is_superuser": user.is_superuser,
    }


class UserConfigurationRequestResponseSchema(BaseModel):
    llm: dict[str, Union[str, float, list[str], None]] | None = None
    tts: dict[str, Union[str, float, list[str], None]] | None = None
    stt: dict[str, Union[str, float, list[str], None]] | None = None
    embeddings: dict[str, Union[str, float, list[str], None]] | None = None
    realtime: dict[str, Union[str, float, list[str], None]] | None = None
    is_realtime: bool | None = None
    test_phone_number: str | None = None
    timezone: str | None = None


@router.get("/configurations/user")
async def get_user_configurations(
    user: UserModel = Depends(get_user),
) -> UserConfigurationRequestResponseSchema:
    user_configurations = await db_client.get_user_configurations(user.id)
    masked_config = mask_user_config(user_configurations)

    return masked_config


def _provider_value(section) -> str:
    provider = getattr(section, "provider", None)
    if provider is None:
        return ""
    return getattr(provider, "value", str(provider))


def _ensure_livekit_runtime_supported(config, incoming: dict) -> None:
    """Reject configurations the LiveKit worker cannot run, at save time.

    Only validates the section(s) the user is actually changing so that
    incremental, tab-scoped saves of unrelated sections keep working. No-op when
    LiveKit is not the active runtime.
    """

    if not is_livekit_runtime():
        return

    # Turning on realtime mode without a realtime model produces a split-brain
    # config (is_realtime=True, realtime=None) that fails only at call time.
    if incoming.get("is_realtime") and config.realtime is None:
        raise ValueError(
            "Realtime mode is enabled but no realtime model is configured. "
            "Configure a realtime model (e.g. Google Gemini realtime) before "
            "turning on realtime mode."
        )

    changed = set(incoming).intersection(SERVICE_FIELDS)
    for service in changed:
        section = getattr(config, service, None)
        if section is None:
            continue
        provider = _provider_value(section)
        if not livekit_supports_provider(service, provider):
            supported = ", ".join(
                sorted(LIVEKIT_SUPPORTED_PROVIDERS.get(service, frozenset()))
            )
            label = {"stt": "transcriber", "tts": "voice"}.get(service, service)
            raise ValueError(
                f"The LiveKit runtime cannot run the '{provider}' {label} "
                f"provider. Supported {label} providers: {supported or 'none'}."
            )


@router.put("/configurations/user")
async def update_user_configurations(
    request: UserConfigurationRequestResponseSchema,
    user: UserModel = Depends(get_user),
) -> UserConfigurationRequestResponseSchema:
    existing_config = await db_client.get_user_configurations(user.id)

    incoming_dict = request.model_dump(exclude_none=True)

    # Merge via helper
    try:
        user_configurations = merge_user_configurations(existing_config, incoming_dict)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        check_for_masked_keys(user_configurations)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        _ensure_livekit_runtime_supported(user_configurations, incoming_dict)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        validator = UserConfigurationValidator()
        services_to_validate = set(incoming_dict).intersection(SERVICE_FIELDS)
        if "is_realtime" in incoming_dict:
            if user_configurations.is_realtime:
                services_to_validate.add("realtime")
            else:
                services_to_validate.update({"llm", "stt", "tts"})
        await validator.validate(
            user_configurations,
            organization_id=user.selected_organization_id,
            created_by=user.provider_id,
            only_services=services_to_validate,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=e.args[0])

    user_configurations = await db_client.update_user_configuration(
        user.id, user_configurations
    )

    # Return masked version of updated config
    masked_config = mask_user_config(user_configurations)

    return masked_config


@router.get("/configurations/user/validate")
async def validate_user_configurations(
    validity_ttl_seconds: int = Query(default=60, ge=0, le=86400),
    user: UserModel = Depends(get_user),
) -> APIKeyStatusResponse:
    configurations = await db_client.get_user_configurations(user.id)

    if (
        configurations.last_validated_at
        and configurations.last_validated_at
        < datetime.now() - timedelta(seconds=validity_ttl_seconds)
    ):
        validator = UserConfigurationValidator()
        try:
            status = await validator.validate(
                configurations,
                organization_id=user.selected_organization_id,
                created_by=user.provider_id,
            )
            await db_client.update_user_configuration_last_validated_at(user.id)
            return status
        except ValueError as e:
            raise HTTPException(status_code=422, detail=e.args[0])
    else:
        return {"status": []}


# API Key Management Endpoints
class APIKeyResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class CreateAPIKeyRequest(BaseModel):
    name: str


class CreateAPIKeyResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    api_key: str  # Only returned when creating a new key
    created_at: datetime


@router.get("/api-keys")
async def get_api_keys(
    include_archived: bool = Query(default=False),
    user: UserModel = Depends(get_user),
) -> List[APIKeyResponse]:
    """Get all API keys for the user's selected organization."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    api_keys = await db_client.get_api_keys_by_organization(
        user.selected_organization_id, include_archived=include_archived
    )

    return [
        APIKeyResponse(
            id=key.id,
            name=key.name,
            key_prefix=key.key_prefix,
            is_active=key.is_active,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            archived_at=key.archived_at,
        )
        for key in api_keys
    ]


@router.post("/api-keys")
async def create_api_key(
    request: CreateAPIKeyRequest,
    user: UserModel = Depends(get_user),
) -> CreateAPIKeyResponse:
    """Create a new API key for the user's selected organization."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    api_key, raw_key = await db_client.create_api_key(
        organization_id=user.selected_organization_id,
        name=request.name,
        created_by=user.id,
    )

    return CreateAPIKeyResponse(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        api_key=raw_key,
        created_at=api_key.created_at,
    )


@router.delete("/api-keys/{api_key_id}")
async def archive_api_key(
    api_key_id: int,
    user: UserModel = Depends(get_user),
) -> dict:
    """Archive an API key (soft delete)."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Verify the API key belongs to the user's organization
    api_keys = await db_client.get_api_keys_by_organization(
        user.selected_organization_id, include_archived=True
    )
    if not any(key.id == api_key_id for key in api_keys):
        raise HTTPException(status_code=404, detail="API key not found")

    success = await db_client.archive_api_key(api_key_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to archive API key")

    return {"success": True, "message": "API key archived successfully"}


@router.put("/api-keys/{api_key_id}/reactivate")
async def reactivate_api_key(
    api_key_id: int,
    user: UserModel = Depends(get_user),
) -> dict:
    """Reactivate an archived API key."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Verify the API key belongs to the user's organization
    api_keys = await db_client.get_api_keys_by_organization(
        user.selected_organization_id, include_archived=True
    )
    if not any(key.id == api_key_id for key in api_keys):
        raise HTTPException(status_code=404, detail="API key not found")

    success = await db_client.reactivate_api_key(api_key_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to reactivate API key")

    return {"success": True, "message": "API key reactivated successfully"}


# Voice Configuration Endpoints
TTSProvider = Literal["elevenlabs", "deepgram", "sarvam", "cartesia", "dograh", "rime"]


class VoiceInfo(BaseModel):
    voice_id: str
    name: str
    description: Optional[str] = None
    accent: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    preview_url: Optional[str] = None


class VoicesResponse(BaseModel):
    provider: str
    voices: List[VoiceInfo]


@router.get("/configurations/voices/{provider}")
async def get_voices(
    provider: TTSProvider,
    model: Optional[str] = None,
    language: Optional[str] = None,
    user: UserModel = Depends(get_user),
) -> VoicesResponse:
    """Get available voices for a TTS provider."""
    require_dograh_cloud("MPS voice catalog")

    try:
        result = await mps_service_key_client.get_voices(
            provider=provider,
            model=model,
            language=language,
            organization_id=user.selected_organization_id,
            created_by=user.provider_id,
        )
        return VoicesResponse(
            provider=result.get("provider", provider),
            voices=[VoiceInfo(**voice) for voice in result.get("voices", [])],
        )
    except Exception as e:
        logger.error(f"Failed to fetch voices for {provider}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch voices for {provider}",
        )

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import UserModel
from api.enums import CallType
from api.services.auth.depends import get_user
from api.services.livekit.client import (
    LiveKitConfigurationError,
    create_room_session,
    create_sip_dispatch_rule,
    is_livekit_runtime,
    livekit_configured,
)
from api.services.livekit.runtime_config import (
    LiveKitRuntimeSettings,
    effective_livekit_settings,
    save_livekit_settings,
)
from api.services.livekit.vobiz import (
    VobizLiveKitSyncResult,
    import_vobiz_phone_numbers,
    preserve_vobiz_livekit_credentials,
    sync_vobiz_livekit_config,
    verify_vobiz_credentials,
)
from api.services.livekit.worker_process import (
    apply_livekit_worker_settings,
    get_worker_status,
)
from api.services.workflow.defaults import (
    DEFAULT_WORKFLOW_NAME,
    ensure_default_workflow_for_organization,
)

router = APIRouter(prefix="/livekit")


class LiveKitRuntimeResponse(BaseModel):
    voice_runtime: str
    livekit_enabled: bool


class LiveKitSessionRequest(BaseModel):
    initial_context: dict[str, Any] = Field(default_factory=dict)


class LiveKitSessionResponse(BaseModel):
    livekit_url: str
    room_name: str
    participant_identity: str
    participant_token: str
    dispatch_id: str | None = None


class LiveKitSIPDispatchRuleRequest(BaseModel):
    trunk_ids: list[str] = Field(default_factory=list)
    inbound_numbers: list[str] = Field(default_factory=list)
    name: str | None = None
    room_prefix: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveKitSettingsRequest(BaseModel):
    voice_runtime: str = "livekit"
    livekit_url: str = ""
    livekit_client_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str | None = None
    livekit_agent_name: str = "spx-voice"
    livekit_room_prefix: str = "spx-voice"
    livekit_token_ttl_seconds: int = 3600
    livekit_sip_inbound_host: str = ""
    livekit_sip_max_call_duration_seconds: int = 1800


class LiveKitSettingsResponse(BaseModel):
    voice_runtime: str
    livekit_url: str
    livekit_client_url: str
    livekit_api_key: str
    livekit_api_secret_configured: bool
    livekit_agent_name: str
    livekit_room_prefix: str
    livekit_token_ttl_seconds: int
    livekit_sip_inbound_host: str
    livekit_sip_max_call_duration_seconds: int
    livekit_enabled: bool
    source: str
    worker_managed_by_api: bool
    worker_running: bool
    worker_pid: int | None = None
    worker_message: str | None = None
    vobiz_sync_message: str | None = None


class VobizLiveKitSetupRequest(BaseModel):
    livekit_url: str = ""
    livekit_client_url: str | None = None
    livekit_api_key: str = ""
    livekit_api_secret: str | None = None
    livekit_agent_name: str = Field(default="spx-voice", min_length=1, max_length=64)
    livekit_room_prefix: str = Field(default="spx-voice", min_length=1, max_length=64)
    livekit_sip_inbound_host: str = ""
    livekit_token_ttl_seconds: int = Field(default=3600, ge=60)
    livekit_sip_max_call_duration_seconds: int = Field(default=1800, ge=60)
    provision_livekit_sip: bool = True

    config_id: int | None = None
    config_name: str = Field(default="Vobiz LiveKit", min_length=1, max_length=64)
    set_default_outbound: bool = True
    vobiz_auth_id: str = Field(..., min_length=1)
    vobiz_auth_token: str = Field(..., min_length=1)
    vobiz_application_id: str | None = None
    phone_numbers: list[str] = Field(default_factory=list)
    inbound_workflow_id: int | None = None


class VobizLiveKitSetupResponse(BaseModel):
    livekit: LiveKitSettingsResponse
    telephony_config_id: int
    telephony_config_name: str
    telephony_config_created: bool
    imported_phone_numbers: int
    active_phone_numbers: int
    inbound_workflow_id: int | None = None
    sync_ok: bool
    sync_message: str | None = None


class VobizTestConnectionRequest(BaseModel):
    vobiz_auth_id: str = Field(..., min_length=1)
    vobiz_auth_token: str = Field(..., min_length=1)


class VobizTestConnectionResponse(BaseModel):
    ok: bool
    detail: str
    number_count: int | None = None


@router.post("/vobiz/test-connection", response_model=VobizTestConnectionResponse)
async def test_vobiz_connection(
    request: VobizTestConnectionRequest,
    user: UserModel = Depends(get_user),
) -> VobizTestConnectionResponse:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    result = await verify_vobiz_credentials(
        request.vobiz_auth_id, request.vobiz_auth_token
    )
    return VobizTestConnectionResponse(
        ok=bool(result.get("ok")),
        detail=str(result.get("detail", "")),
        number_count=result.get("number_count"),
    )


@router.get("/runtime", response_model=LiveKitRuntimeResponse)
async def get_livekit_runtime() -> LiveKitRuntimeResponse:
    return LiveKitRuntimeResponse(
        voice_runtime="livekit",
        livekit_enabled=is_livekit_runtime() and livekit_configured(),
    )


@router.get("/settings", response_model=LiveKitSettingsResponse)
async def get_livekit_settings(user: UserModel = Depends(get_user)):
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return _settings_response()


@router.put("/settings", response_model=LiveKitSettingsResponse)
async def update_livekit_settings(
    request: LiveKitSettingsRequest,
    user: UserModel = Depends(get_user),
):
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    existing = effective_livekit_settings()
    secret = (
        request.livekit_api_secret
        if request.livekit_api_secret not in (None, "")
        else existing.livekit_api_secret
    )
    settings = save_livekit_settings(
        {
            "voice_runtime": request.voice_runtime,
            "livekit_url": request.livekit_url,
            "livekit_client_url": request.livekit_client_url,
            "livekit_api_key": request.livekit_api_key,
            "livekit_api_secret": secret,
            "livekit_agent_name": request.livekit_agent_name,
            "livekit_room_prefix": request.livekit_room_prefix,
            "livekit_token_ttl_seconds": request.livekit_token_ttl_seconds,
            "livekit_sip_inbound_host": request.livekit_sip_inbound_host,
            "livekit_sip_max_call_duration_seconds": (
                request.livekit_sip_max_call_duration_seconds
            ),
        }
    )
    apply_livekit_worker_settings(settings)
    vobiz_sync_message = await _sync_vobiz_configs_for_org(
        user.selected_organization_id,
        settings=settings,
    )
    return _settings_response(vobiz_sync_message=vobiz_sync_message)


@router.post("/vobiz/setup", response_model=VobizLiveKitSetupResponse)
async def setup_vobiz_livekit(
    request: VobizLiveKitSetupRequest,
    user: UserModel = Depends(get_user),
):
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    organization_id = user.selected_organization_id
    inbound_workflow_id = await _resolve_vobiz_setup_inbound_workflow_id(
        request.inbound_workflow_id,
        organization_id=organization_id,
        user_id=getattr(user, "id", None),
    )

    if request.provision_livekit_sip:
        existing_settings = effective_livekit_settings()
        livekit_url = request.livekit_url.strip() or existing_settings.livekit_url
        livekit_api_key = (
            request.livekit_api_key.strip() or existing_settings.livekit_api_key
        )
        livekit_api_secret = (
            request.livekit_api_secret.strip()
            if request.livekit_api_secret not in (None, "")
            else existing_settings.livekit_api_secret
        )
        livekit_sip_inbound_host = (
            request.livekit_sip_inbound_host.strip()
            or existing_settings.livekit_sip_inbound_host
        )
        if not (livekit_url and livekit_api_key and livekit_api_secret):
            raise HTTPException(
                status_code=400,
                detail="LiveKit URL, API key, and API secret are required.",
            )
        if not livekit_sip_inbound_host:
            raise HTTPException(
                status_code=400,
                detail="LiveKit SIP inbound host is required for Vobiz setup.",
            )

        settings = save_livekit_settings(
            {
                "voice_runtime": "livekit",
                "livekit_url": livekit_url,
                "livekit_client_url": (
                    request.livekit_client_url.strip()
                    if request.livekit_client_url
                    else livekit_url
                ),
                "livekit_api_key": livekit_api_key,
                "livekit_api_secret": livekit_api_secret,
                "livekit_agent_name": request.livekit_agent_name,
                "livekit_room_prefix": request.livekit_room_prefix,
                "livekit_token_ttl_seconds": request.livekit_token_ttl_seconds,
                "livekit_sip_inbound_host": livekit_sip_inbound_host,
                "livekit_sip_max_call_duration_seconds": (
                    request.livekit_sip_max_call_duration_seconds
                ),
            }
        )
        apply_livekit_worker_settings(settings)

    row, created = await _upsert_vobiz_setup_config(
        organization_id=organization_id,
        request=request,
    )
    if request.set_default_outbound:
        row = (
            await db_client.set_default_telephony_configuration(row.id, organization_id)
        ) or row

    if request.provision_livekit_sip:
        sync_result = await _sync_vobiz_livekit_config_safely(
            config_id=row.id,
            organization_id=organization_id,
            import_phone_numbers=True,
            requested_phone_numbers=request.phone_numbers,
        )
        imported_phone_numbers = sync_result.imported_phone_numbers
    else:
        imported_phone_numbers = await import_vobiz_phone_numbers(
            config_id=row.id,
            organization_id=organization_id,
            credentials=row.credentials or {},
            requested_phone_numbers=request.phone_numbers,
        )
        sync_result = VobizLiveKitSyncResult(
            ok=True,
            message=("Vobiz config saved locally; LiveKit SIP provisioning skipped."),
            imported_phone_numbers=imported_phone_numbers,
        )

    assigned = 0
    if inbound_workflow_id is not None:
        numbers = await db_client.list_phone_numbers_for_config(row.id)
        for number in numbers:
            if not getattr(number, "is_active", False):
                continue
            if getattr(number, "inbound_workflow_id", None) == inbound_workflow_id:
                continue
            await db_client.update_phone_number(
                number.id,
                row.id,
                inbound_workflow_id=inbound_workflow_id,
            )
            assigned += 1
        if assigned and request.provision_livekit_sip:
            sync_result = await _sync_vobiz_livekit_config_safely(
                config_id=row.id,
                organization_id=organization_id,
            )

    numbers = await db_client.list_phone_numbers_for_config(row.id)
    active_phone_numbers = len([n for n in numbers if getattr(n, "is_active", False)])

    message = sync_result.message
    if assigned:
        suffix = "number" if assigned == 1 else "numbers"
        route_message = f"Attached inbound workflow to {assigned} {suffix}."
        message = f"{message} {route_message}".strip() if message else route_message

    return VobizLiveKitSetupResponse(
        livekit=_settings_response(vobiz_sync_message=message),
        telephony_config_id=row.id,
        telephony_config_name=row.name,
        telephony_config_created=created,
        imported_phone_numbers=imported_phone_numbers,
        active_phone_numbers=active_phone_numbers,
        inbound_workflow_id=inbound_workflow_id,
        sync_ok=sync_result.ok,
        sync_message=message,
    )


async def _resolve_vobiz_setup_inbound_workflow_id(
    requested_workflow_id: int | None,
    *,
    organization_id: int,
    user_id: int | None,
) -> int | None:
    if requested_workflow_id is not None:
        workflow = await db_client.get_workflow(
            requested_workflow_id,
            organization_id=organization_id,
        )
        if not workflow:
            raise HTTPException(status_code=404, detail="Inbound workflow not found")
        return requested_workflow_id

    if user_id is None:
        return None

    await ensure_default_workflow_for_organization(
        user_id=user_id,
        organization_id=organization_id,
    )
    workflows = await db_client.get_all_workflows_for_listing(
        organization_id=organization_id,
        status="active",
    )
    if not workflows:
        return None

    default_workflow = next(
        (
            workflow
            for workflow in workflows
            if getattr(workflow, "name", None) == DEFAULT_WORKFLOW_NAME
        ),
        None,
    )
    if default_workflow:
        return default_workflow.id

    if len(workflows) == 1:
        return workflows[0].id

    return None


async def _sync_vobiz_livekit_config_safely(**kwargs) -> VobizLiveKitSyncResult:
    try:
        return await sync_vobiz_livekit_config(**kwargs)
    except Exception as exc:
        logger.warning(f"Vobiz LiveKit SIP sync failed: {exc}")
        return VobizLiveKitSyncResult(
            ok=False,
            message=_livekit_sync_error_message(exc),
            imported_phone_numbers=0,
        )


def _livekit_sync_error_message(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    prefix = "Vobiz config was saved locally, but SIP provisioning failed: "
    if "401" in text and "livekit" in low:
        return (
            f"{prefix}LiveKit rejected the API key/secret (401 Unauthorized). "
            "Update LiveKit settings or rerun setup with SIP provisioning disabled."
        )
    if ("401" in text or "403" in text) and "vobiz" in low:
        return (
            f"{prefix}Vobiz rejected the account ID / auth token. Re-check the "
            "Vobiz credentials and rerun setup."
        )
    if "failed to reach vobiz" in low or "timeout" in low:
        return (
            f"{prefix}the Vobiz API could not be reached (timeout or network "
            "error). Check connectivity and try again."
        )
    return f"{prefix}{text}"


@router.post(
    "/session/{workflow_id}/{workflow_run_id}",
    response_model=LiveKitSessionResponse,
)
async def create_livekit_session(
    workflow_id: int,
    workflow_run_id: int,
    request: LiveKitSessionRequest,
    user: UserModel = Depends(get_user),
) -> LiveKitSessionResponse:
    if not is_livekit_runtime():
        raise HTTPException(status_code=409, detail="voice_runtime_not_livekit")

    workflow_run = await db_client.get_workflow_run(
        workflow_run_id, organization_id=user.selected_organization_id
    )
    if not workflow_run or workflow_run.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="workflow_run_not_found")
    if workflow_run.is_completed:
        raise HTTPException(status_code=400, detail="workflow_run_already_completed")

    try:
        session = await create_room_session(
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            user_id=user.id,
            organization_id=user.selected_organization_id,
            call_type=CallType.OUTBOUND.value,
            participant_identity=f"user-{user.id}-run-{workflow_run_id}",
            participant_name=user.email,
            initial_context={
                **(workflow_run.initial_context or {}),
                **(request.initial_context or {}),
            },
        )
    except LiveKitConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    await db_client.update_workflow_run(
        workflow_run_id,
        gathered_context={
            "livekit_room": session.room_name,
            "livekit_dispatch_id": session.dispatch_id,
        },
    )
    return LiveKitSessionResponse(**session.__dict__)


@router.post("/sip/dispatch-rules/{workflow_id}")
async def create_inbound_sip_dispatch_rule(
    workflow_id: int,
    request: LiveKitSIPDispatchRuleRequest,
    user: UserModel = Depends(get_user),
):
    if not is_livekit_runtime():
        raise HTTPException(status_code=409, detail="voice_runtime_not_livekit")

    workflow = await db_client.get_workflow(
        workflow_id, organization_id=user.selected_organization_id
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="workflow_not_found")

    try:
        return await create_sip_dispatch_rule(
            workflow_id=workflow_id,
            user_id=user.id,
            organization_id=user.selected_organization_id,
            trunk_ids=request.trunk_ids,
            inbound_numbers=request.inbound_numbers,
            name=request.name,
            room_prefix=request.room_prefix,
            metadata=request.metadata,
        )
    except LiveKitConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _sync_vobiz_configs_for_org(
    organization_id: int,
    *,
    settings: LiveKitRuntimeSettings,
) -> str | None:
    if not (settings.is_livekit and settings.configured):
        return None

    rows = await db_client.list_telephony_configurations_by_provider(
        organization_id,
        "vobiz",
    )
    if not rows:
        return None

    synced = 0
    failed: list[str] = []
    for row in rows:
        try:
            result = await sync_vobiz_livekit_config(
                config_id=row.id,
                organization_id=organization_id,
                import_phone_numbers=True,
            )
            if result.ok:
                synced += 1
            elif result.message:
                failed.append(f"{row.name}: {result.message}")
        except Exception as exc:
            logger.warning(f"LiveKit Vobiz sync failed for config {row.id}: {exc}")
            failed.append(f"{row.name}: {exc}")

    if failed:
        return "LiveKit saved, but Vobiz sync needs attention: " + "; ".join(failed)
    if synced:
        suffix = "configuration" if synced == 1 else "configurations"
        return f"LiveKit saved and {synced} Vobiz {suffix} synced."
    return None


async def _upsert_vobiz_setup_config(
    *,
    organization_id: int,
    request: VobizLiveKitSetupRequest,
):
    row = None
    if request.config_id is not None:
        row = await db_client.get_telephony_configuration_for_org(
            request.config_id,
            organization_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Vobiz configuration not found")
        if row.provider != "vobiz":
            raise HTTPException(
                status_code=400,
                detail="Selected telephony configuration is not a Vobiz config.",
            )
    else:
        auth_id = request.vobiz_auth_id.strip()
        configs = await db_client.list_telephony_configurations_by_provider(
            organization_id,
            "vobiz",
        )
        for candidate in configs:
            if (candidate.credentials or {}).get("auth_id") == auth_id:
                row = candidate
                break

    base_credentials = dict((row.credentials if row else {}) or {})
    auth_id = request.vobiz_auth_id.strip()
    auth_token = request.vobiz_auth_token.strip()
    same_account = base_credentials.get("auth_id") == auth_id
    credentials = dict(base_credentials if same_account else {})
    credentials.update(
        {
            "auth_id": auth_id,
            "auth_token": auth_token,
        }
    )
    if request.vobiz_application_id is not None:
        credentials["application_id"] = request.vobiz_application_id.strip() or None
    elif base_credentials.get("application_id"):
        credentials["application_id"] = base_credentials["application_id"]
    if row and same_account:
        credentials = preserve_vobiz_livekit_credentials(
            credentials,
            base_credentials,
        )

    if row:
        updated = await db_client.update_telephony_configuration(
            config_id=row.id,
            organization_id=organization_id,
            name=request.config_name,
            credentials=credentials,
        )
        return updated or row, False

    try:
        created = await db_client.create_telephony_configuration(
            organization_id=organization_id,
            name=request.config_name,
            provider="vobiz",
            credentials=credentials,
            is_default_outbound=request.set_default_outbound,
        )
    except IntegrityError as exc:
        if "uq_telephony_configurations_org_name" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A telephony configuration named '{request.config_name}' already "
                    "exists in this organization. Pick a different name."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail="Telephony configuration violates a uniqueness constraint.",
        )
    return created, True


def _settings_response(
    *, vobiz_sync_message: str | None = None
) -> LiveKitSettingsResponse:
    settings = effective_livekit_settings()
    worker = get_worker_status()
    return LiveKitSettingsResponse(
        voice_runtime=settings.voice_runtime,
        livekit_url=settings.livekit_url,
        livekit_client_url=settings.livekit_client_url,
        livekit_api_key=settings.livekit_api_key,
        livekit_api_secret_configured=bool(settings.livekit_api_secret),
        livekit_agent_name=settings.livekit_agent_name,
        livekit_room_prefix=settings.livekit_room_prefix,
        livekit_token_ttl_seconds=settings.livekit_token_ttl_seconds,
        livekit_sip_inbound_host=settings.livekit_sip_inbound_host,
        livekit_sip_max_call_duration_seconds=(
            settings.livekit_sip_max_call_duration_seconds
        ),
        livekit_enabled=settings.is_livekit and settings.configured,
        source=settings.source,
        worker_managed_by_api=worker.managed_by_api,
        worker_running=worker.running,
        worker_pid=worker.pid,
        worker_message=worker.message,
        vobiz_sync_message=vobiz_sync_message,
    )

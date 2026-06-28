from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from google.protobuf.duration_pb2 import Duration
from livekit import api as livekit_api
from livekit.api import TwirpError, TwirpErrorCode
from loguru import logger

from api.services.livekit.runtime_config import (
    effective_livekit_settings,
    is_livekit_runtime as _is_livekit_runtime,
    is_pipecat_runtime as _is_pipecat_runtime,
    livekit_configured as _livekit_configured,
)


LIVEKIT_RUNTIME = "livekit"
PIPECAT_RUNTIME = "pipecat"


class LiveKitConfigurationError(RuntimeError):
    """Raised when the LiveKit runtime is selected but not configured."""


@dataclass(frozen=True)
class LiveKitRoomSession:
    livekit_url: str
    room_name: str
    participant_identity: str
    participant_token: str
    dispatch_id: str | None = None


@dataclass(frozen=True)
class LiveKitSIPCall:
    room_name: str
    dispatch_id: str | None
    sip_participant_id: str | None
    call_id: str | None
    raw_response: dict[str, Any]


def is_livekit_runtime() -> bool:
    return _is_livekit_runtime()


def is_pipecat_runtime() -> bool:
    return _is_pipecat_runtime()


def livekit_configured() -> bool:
    return _livekit_configured()


def require_livekit_configured() -> None:
    settings = effective_livekit_settings()
    missing = []
    if not settings.livekit_url:
        missing.append("LIVEKIT_URL")
    if not settings.livekit_api_key:
        missing.append("LIVEKIT_API_KEY")
    if not settings.livekit_api_secret:
        missing.append("LIVEKIT_API_SECRET")
    if missing:
        raise LiveKitConfigurationError(
            "LiveKit runtime is selected but missing: " + ", ".join(missing)
        )


def require_livekit_sip_configured(sip_trunk_id: str | None = None) -> None:
    require_livekit_configured()
    settings = effective_livekit_settings()
    if not (sip_trunk_id or settings.livekit_sip_outbound_trunk_id):
        raise LiveKitConfigurationError(
            "LiveKit SIP outbound calls require a configured outbound trunk"
        )


def room_name_for_run(workflow_id: int, workflow_run_id: int) -> str:
    settings = effective_livekit_settings()
    return f"{settings.livekit_room_prefix}-wf-{workflow_id}-run-{workflow_run_id}"


def room_prefix_for_workflow(workflow_id: int) -> str:
    settings = effective_livekit_settings()
    return f"{settings.livekit_room_prefix}-wf-{workflow_id}-"


def browser_livekit_url() -> str:
    return effective_livekit_settings().browser_url


def build_run_metadata(
    *,
    workflow_id: int,
    workflow_run_id: int | None,
    user_id: int,
    organization_id: int,
    call_type: str,
    initial_context: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "runtime": LIVEKIT_RUNTIME,
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "user_id": user_id,
        "organization_id": organization_id,
        "call_type": call_type,
        "initial_context": initial_context or {},
    }
    if extra:
        metadata.update(extra)
    return metadata


def _json_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, separators=(",", ":"), default=str)


def _new_access_token(identity: str, *, name: str | None = None) -> livekit_api.AccessToken:
    require_livekit_configured()
    settings = effective_livekit_settings()
    token = livekit_api.AccessToken(
        settings.livekit_api_key, settings.livekit_api_secret
    ).with_identity(
        identity
    )
    if name:
        token = token.with_name(name)
    return token.with_ttl(timedelta(seconds=settings.livekit_token_ttl_seconds))


def create_join_token(
    *,
    room_name: str,
    identity: str,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    token = _new_access_token(identity, name=name)
    if metadata:
        token = token.with_metadata(_json_metadata(metadata))
    token = token.with_grants(
        livekit_api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
    )
    return token.to_jwt()


async def _ensure_room(
    lkapi: livekit_api.LiveKitAPI,
    *,
    room_name: str,
    metadata: dict[str, Any],
    agents: list[livekit_api.RoomAgentDispatch] | None = None,
) -> None:
    try:
        await lkapi.room.create_room(
            livekit_api.CreateRoomRequest(
                name=room_name,
                empty_timeout=60,
                departure_timeout=30,
                max_participants=8,
                metadata=_json_metadata(metadata),
                agents=agents or [],
            )
        )
    except TwirpError as exc:
        if exc.code == TwirpErrorCode.ALREADY_EXISTS:
            logger.info(f"LiveKit room already exists: {room_name}")
            return
        raise


async def create_agent_dispatch(
    *,
    room_name: str,
    metadata: dict[str, Any],
    agent_name: str | None = None,
) -> str | None:
    require_livekit_configured()
    settings = effective_livekit_settings()
    async with livekit_api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lkapi:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            livekit_api.CreateAgentDispatchRequest(
                agent_name=agent_name or settings.livekit_agent_name,
                room=room_name,
                metadata=_json_metadata(metadata),
            )
        )
        return dispatch.id or None


async def create_room_session(
    *,
    workflow_id: int,
    workflow_run_id: int,
    user_id: int,
    organization_id: int,
    call_type: str,
    participant_identity: str,
    participant_name: str,
    initial_context: dict[str, Any] | None = None,
) -> LiveKitRoomSession:
    require_livekit_configured()
    settings = effective_livekit_settings()
    room_name = room_name_for_run(workflow_id, workflow_run_id)
    metadata = build_run_metadata(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        user_id=user_id,
        organization_id=organization_id,
        call_type=call_type,
        initial_context=initial_context,
    )

    async with livekit_api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lkapi:
        await _ensure_room(lkapi, room_name=room_name, metadata=metadata)
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            livekit_api.CreateAgentDispatchRequest(
                agent_name=settings.livekit_agent_name,
                room=room_name,
                metadata=_json_metadata(metadata),
            )
        )

    participant_token = create_join_token(
        room_name=room_name,
        identity=participant_identity,
        name=participant_name,
        metadata=metadata,
    )
    return LiveKitRoomSession(
        livekit_url=browser_livekit_url(),
        room_name=room_name,
        participant_identity=participant_identity,
        participant_token=participant_token,
        dispatch_id=dispatch.id or None,
    )


async def create_outbound_sip_call(
    *,
    workflow_id: int,
    workflow_run_id: int,
    user_id: int,
    organization_id: int,
    to_number: str,
    from_number: str | None = None,
    sip_trunk_id: str | None = None,
    initial_context: dict[str, Any] | None = None,
) -> LiveKitSIPCall:
    require_livekit_sip_configured(sip_trunk_id)
    settings = effective_livekit_settings()
    resolved_trunk_id = sip_trunk_id or settings.livekit_sip_outbound_trunk_id
    room_name = room_name_for_run(workflow_id, workflow_run_id)
    metadata = build_run_metadata(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        user_id=user_id,
        organization_id=organization_id,
        call_type="outbound",
        initial_context=initial_context,
        extra={"to_number": to_number, "from_number": from_number},
    )

    async with livekit_api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lkapi:
        await _ensure_room(lkapi, room_name=room_name, metadata=metadata)
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            livekit_api.CreateAgentDispatchRequest(
                agent_name=settings.livekit_agent_name,
                room=room_name,
                metadata=_json_metadata(metadata),
            )
        )
        participant = await lkapi.sip.create_sip_participant(
            livekit_api.CreateSIPParticipantRequest(
                sip_trunk_id=resolved_trunk_id,
                sip_call_to=to_number,
                sip_number=from_number or settings.livekit_sip_default_from_number,
                room_name=room_name,
                participant_identity=f"sip-{workflow_run_id}",
                participant_name=to_number,
                participant_metadata=_json_metadata(metadata),
                wait_until_answered=False,
                max_call_duration=Duration(
                    seconds=settings.livekit_sip_max_call_duration_seconds
                ),
            )
        )

    raw_response = {
        field.name: getattr(participant, field.name)
        for field in participant.DESCRIPTOR.fields
    }
    return LiveKitSIPCall(
        room_name=room_name,
        dispatch_id=dispatch.id or None,
        sip_participant_id=getattr(participant, "participant_id", None) or None,
        call_id=getattr(participant, "sip_call_id", None) or None,
        raw_response=raw_response,
    )


async def create_sip_dispatch_rule(
    *,
    workflow_id: int,
    user_id: int,
    organization_id: int,
    trunk_ids: list[str],
    inbound_numbers: list[str],
    name: str | None = None,
    room_prefix: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_livekit_configured()
    settings = effective_livekit_settings()
    rule_metadata = build_run_metadata(
        workflow_id=workflow_id,
        workflow_run_id=None,
        user_id=user_id,
        organization_id=organization_id,
        call_type="inbound",
        initial_context={},
        extra=metadata,
    )
    prefix = room_prefix or room_prefix_for_workflow(workflow_id)

    room_config = livekit_api.RoomConfiguration(
        agents=[
            livekit_api.RoomAgentDispatch(
                agent_name=settings.livekit_agent_name,
                metadata=_json_metadata(rule_metadata),
            )
        ],
        metadata=_json_metadata(rule_metadata),
    )

    async with livekit_api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lkapi:
        created = await lkapi.sip.create_sip_dispatch_rule(
            livekit_api.CreateSIPDispatchRuleRequest(
                rule=livekit_api.SIPDispatchRule(
                    dispatch_rule_individual=livekit_api.SIPDispatchRuleIndividual(
                        room_prefix=prefix,
                    )
                ),
                trunk_ids=trunk_ids,
                inbound_numbers=inbound_numbers,
                name=name or f"workflow-{workflow_id}",
                metadata=_json_metadata(rule_metadata),
                room_config=room_config,
            )
        )

    return {
        "sip_dispatch_rule_id": created.sip_dispatch_rule_id,
        "room_prefix": prefix,
        "agent_name": settings.livekit_agent_name,
        "metadata": rule_metadata,
    }

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
from fastapi import HTTPException
from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.services.livekit.client import (
    build_run_metadata,
    is_livekit_runtime,
    livekit_configured,
    room_prefix_for_workflow,
)
from api.services.livekit.runtime_config import effective_livekit_settings
from livekit import api as livekit_api
from livekit.api import TwirpError, TwirpErrorCode

VOBIZ_API_BASE_URL = "https://api.vobiz.ai/api"

# Bound every Vobiz HTTP call so a hung Vobiz API surfaces as a clear timeout
# error instead of a setup wizard that spins indefinitely.
VOBIZ_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

VOBIZ_LIVEKIT_DERIVED_CREDENTIAL_FIELDS = {
    "livekit_sip_outbound_trunk_id",
    "livekit_sip_inbound_trunk_id",
    "livekit_sip_dispatch_rule_id",
    "livekit_sip_dispatch_rules",
    "vobiz_sip_credential_id",
    "vobiz_sip_username",
    "vobiz_sip_password",
    "vobiz_sip_realm",
    "vobiz_sip_trunk_id",
    "vobiz_sip_domain",
    "vobiz_sip_inbound_destination",
}

VOBIZ_LIVEKIT_SENSITIVE_CREDENTIAL_FIELDS = {
    "vobiz_sip_password",
}


@dataclass(frozen=True)
class VobizLiveKitSyncResult:
    ok: bool
    message: str | None = None
    imported_phone_numbers: int = 0


def should_auto_provision_vobiz_livekit() -> bool:
    return is_livekit_runtime() and livekit_configured()


def livekit_sip_inbound_destination() -> str:
    return effective_livekit_settings().sip_inbound_destination


def preserve_vobiz_livekit_credentials(credentials: dict, existing: dict) -> dict:
    """Keep auto-provisioned SIP fields when a client submits only visible fields."""
    out = dict(credentials)
    for field in VOBIZ_LIVEKIT_DERIVED_CREDENTIAL_FIELDS:
        if not out.get(field) and existing.get(field):
            out[field] = existing[field]
    return out


async def ensure_vobiz_livekit_credentials(
    credentials: dict[str, Any],
    *,
    phone_numbers: list[str] | None = None,
    organization_id: int | None = None,
    telephony_configuration_id: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Provision the Vobiz and LiveKit SIP assets stored on a Vobiz config.

    The only user-entered Vobiz fields should remain auth_id/auth_token. In
    LiveKit mode this creates the Vobiz SIP trunk credential/trunk, then creates
    or updates matching LiveKit inbound/outbound trunks and stores their IDs.
    """
    if not should_auto_provision_vobiz_livekit():
        return credentials

    auth_id = credentials.get("auth_id")
    auth_token = credentials.get("auth_token")
    if not auth_id or not auth_token:
        return credentials

    updated = dict(credentials)
    label = name or f"SPX Voice {telephony_configuration_id or auth_id}"
    numbers = _clean_phone_numbers(phone_numbers or [])
    livekit_inbound_numbers = _livekit_inbound_number_aliases(numbers)

    async with aiohttp.ClientSession(timeout=VOBIZ_HTTP_TIMEOUT) as session:
        if not (
            updated.get("vobiz_sip_username")
            and updated.get("vobiz_sip_password")
            and updated.get("vobiz_sip_credential_id")
        ):
            credential = await _create_vobiz_sip_credential(
                session,
                auth_id=auth_id,
                auth_token=auth_token,
                username=updated.get("vobiz_sip_username"),
                password=updated.get("vobiz_sip_password"),
            )
            updated.update(credential)

        if not (updated.get("vobiz_sip_trunk_id") and updated.get("vobiz_sip_domain")):
            trunk = await _create_vobiz_sip_trunk(
                session,
                auth_id=auth_id,
                auth_token=auth_token,
                name=label,
                credential_id=updated.get("vobiz_sip_credential_id"),
            )
            updated.update(trunk)

        destination = livekit_sip_inbound_destination()
        if destination and updated.get("vobiz_sip_trunk_id"):
            await _update_vobiz_sip_trunk_destination(
                session,
                auth_id=auth_id,
                auth_token=auth_token,
                trunk_id=updated["vobiz_sip_trunk_id"],
                inbound_destination=destination,
            )
            updated["vobiz_sip_inbound_destination"] = destination
        if numbers and updated.get("vobiz_sip_trunk_id"):
            await _ensure_vobiz_phone_numbers_assigned(
                session,
                auth_id=auth_id,
                auth_token=auth_token,
                trunk_id=updated["vobiz_sip_trunk_id"],
                phone_numbers=numbers,
            )

    vobiz_domain = updated.get("vobiz_sip_domain")
    username = updated.get("vobiz_sip_username")
    password = updated.get("vobiz_sip_password")
    if not (vobiz_domain and username and password):
        raise HTTPException(
            status_code=502,
            detail="Vobiz SIP trunk response did not include domain/credentials.",
        )

    metadata = _metadata(
        auth_id=auth_id,
        organization_id=organization_id,
        telephony_configuration_id=telephony_configuration_id,
    )

    settings = effective_livekit_settings()
    async with livekit_api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lkapi:
        outbound_id = await _ensure_livekit_outbound_trunk(
            lkapi,
            trunk_id=updated.get("livekit_sip_outbound_trunk_id"),
            name=f"{label} outbound",
            address=vobiz_domain,
            username=username,
            password=password,
            numbers=numbers,
            metadata=metadata,
        )
        updated["livekit_sip_outbound_trunk_id"] = outbound_id

        inbound_id = await _ensure_livekit_inbound_trunk(
            lkapi,
            trunk_id=updated.get("livekit_sip_inbound_trunk_id"),
            name=f"{label} inbound",
            numbers=livekit_inbound_numbers,
            metadata=metadata,
        )
        updated["livekit_sip_inbound_trunk_id"] = inbound_id

    logger.info(
        "[Vobiz/LiveKit] linked account "
        f"{auth_id} to outbound trunk {updated.get('livekit_sip_outbound_trunk_id')}"
    )
    return updated


async def list_vobiz_account_phone_numbers(credentials: dict[str, Any]) -> list[str]:
    auth_id = credentials.get("auth_id")
    auth_token = credentials.get("auth_token")
    if not auth_id or not auth_token:
        return []

    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/numbers"
    headers = _vobiz_headers(auth_id, auth_token)
    try:
        async with aiohttp.ClientSession(timeout=VOBIZ_HTTP_TIMEOUT) as session:
            data = await _vobiz_request_json(
                session, "GET", endpoint, headers=headers, expected_statuses=(200,)
            )
    except HTTPException as exc:
        logger.warning(f"[Vobiz/LiveKit] could not list Vobiz numbers: {exc.detail}")
        return []

    items: list[Any]
    if isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data.get("data"), list):
        items = data["data"]
    elif isinstance(data.get("objects"), list):
        items = data["objects"]
    else:
        items = []

    numbers: list[str] = []
    for item in items:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = _first_string_value(
                item,
                [
                    "phone_number",
                    "phoneNumber",
                    "number",
                    "msisdn",
                    "cli",
                    "did",
                    "e164",
                ],
            )
        else:
            candidate = None
        if candidate:
            normalized = _phone_number_for_storage(candidate)
            if normalized:
                numbers.append(normalized)
    return _dedupe(numbers)


async def verify_vobiz_credentials(auth_id: str, auth_token: str) -> dict[str, Any]:
    """Probe Vobiz with the given credentials before running the full setup.

    Returns ``{"ok": bool, "detail": str, "number_count": int | None}``. Auth and
    network failures are reported in ``detail`` rather than raised, so the wizard
    can show immediate, actionable feedback on the Vobiz step.
    """

    auth_id = (auth_id or "").strip()
    auth_token = (auth_token or "").strip()
    if not auth_id or not auth_token:
        return {
            "ok": False,
            "detail": "Vobiz account ID and auth token are required.",
            "number_count": None,
        }

    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/numbers"
    headers = _vobiz_headers(auth_id, auth_token)
    try:
        async with aiohttp.ClientSession(timeout=VOBIZ_HTTP_TIMEOUT) as session:
            data = await _vobiz_request_json(
                session, "GET", endpoint, headers=headers, expected_statuses=(200,)
            )
    except HTTPException as exc:
        return {
            "ok": False,
            "detail": _vobiz_probe_error_detail(exc),
            "number_count": None,
        }

    count = len(_vobiz_response_items(data))
    return {
        "ok": True,
        "detail": f"Connected to Vobiz. {count} number(s) on the account.",
        "number_count": count,
    }


def _vobiz_response_items(data: dict[str, Any]) -> list[Any]:
    for key in ("items", "data", "objects"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _vobiz_probe_error_detail(exc: HTTPException) -> str:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return (
            "Vobiz rejected the account ID / auth token (HTTP "
            f"{status}). Double-check the credentials in the Vobiz console."
        )
    if status == 404:
        return (
            "Vobiz account not found (HTTP 404). Check that the account ID is correct."
        )
    detail = str(getattr(exc, "detail", exc))
    if "Failed to reach Vobiz API" in detail:
        return "Could not reach the Vobiz API (timeout or network error)."
    # Truncate long upstream bodies so the message stays readable in the UI.
    return f"Vobiz returned an error: {detail[:300]}"


async def sync_vobiz_livekit_config(
    *,
    config_id: int,
    organization_id: int,
    import_phone_numbers: bool = False,
    requested_phone_numbers: list[str] | None = None,
) -> VobizLiveKitSyncResult:
    row = await db_client.get_telephony_configuration_for_org(
        config_id, organization_id
    )
    if not row or row.provider != "vobiz":
        return VobizLiveKitSyncResult(ok=True)

    if is_livekit_runtime() and not livekit_configured():
        return VobizLiveKitSyncResult(
            ok=False,
            message=(
                "LiveKit runtime is selected, but LIVEKIT_URL, LIVEKIT_API_KEY, "
                "or LIVEKIT_API_SECRET is missing."
            ),
        )
    if not should_auto_provision_vobiz_livekit():
        return VobizLiveKitSyncResult(ok=True)

    imported = 0
    if import_phone_numbers:
        imported = await _import_vobiz_phone_numbers(
            config_id=config_id,
            organization_id=organization_id,
            credentials=row.credentials or {},
            requested_phone_numbers=requested_phone_numbers or [],
        )

    phone_rows = await db_client.list_phone_numbers_for_config(config_id)
    active_numbers = [
        p.address_normalized for p in phone_rows if p.is_active and p.address_normalized
    ]

    credentials = await ensure_vobiz_livekit_credentials(
        row.credentials or {},
        phone_numbers=active_numbers,
        organization_id=organization_id,
        telephony_configuration_id=config_id,
        name=row.name,
    )

    dispatch_rules = await _sync_livekit_dispatch_rules(
        credentials=credentials,
        phone_rows=phone_rows,
        organization_id=organization_id,
        telephony_configuration_id=config_id,
        config_name=row.name,
    )
    if dispatch_rules is not None:
        credentials["livekit_sip_dispatch_rules"] = dispatch_rules
        first_rule = next(iter(dispatch_rules.values()), None)
        if first_rule:
            credentials["livekit_sip_dispatch_rule_id"] = first_rule.get(
                "sip_dispatch_rule_id"
            )
        else:
            credentials.pop("livekit_sip_dispatch_rule_id", None)

    if credentials != (row.credentials or {}):
        await db_client.update_telephony_configuration(
            config_id=config_id,
            organization_id=organization_id,
            credentials=credentials,
        )

    message = None
    if not livekit_sip_inbound_destination():
        message = (
            "LiveKit SIP trunks were linked. Set LIVEKIT_SIP_INBOUND_HOST to let "
            "Vobiz route inbound SIP calls to this LiveKit SIP server."
        )
    return VobizLiveKitSyncResult(
        ok=True,
        message=message,
        imported_phone_numbers=imported,
    )


async def import_vobiz_phone_numbers(
    *,
    config_id: int,
    organization_id: int,
    credentials: dict[str, Any],
    requested_phone_numbers: list[str] | None = None,
) -> int:
    """Import Vobiz numbers into local telephony-phone-number storage only."""
    return await _import_vobiz_phone_numbers(
        config_id=config_id,
        organization_id=organization_id,
        credentials=credentials,
        requested_phone_numbers=requested_phone_numbers or [],
    )


async def _import_vobiz_phone_numbers(
    *,
    config_id: int,
    organization_id: int,
    credentials: dict[str, Any],
    requested_phone_numbers: list[str],
) -> int:
    existing = await db_client.list_phone_numbers_for_config(config_id)
    existing_numbers = {
        _phone_number_for_storage(
            getattr(row, "address_normalized", None)
            or getattr(row, "address", None)
            or ""
        )
        for row in existing
    }
    existing_numbers.discard("")

    numbers = _dedupe(
        [_phone_number_for_storage(n) for n in requested_phone_numbers if n]
    )
    numbers = [n for n in numbers if n]
    if not numbers:
        numbers = await list_vobiz_account_phone_numbers(credentials)
    numbers = [n for n in numbers if n not in existing_numbers]

    imported = 0
    for number in numbers:
        try:
            await db_client.create_phone_number(
                organization_id=organization_id,
                telephony_configuration_id=config_id,
                address=number,
                country_code=None if number.startswith("+") else "IN",
                is_default_caller_id=not existing and imported == 0,
            )
            imported += 1
        except (IntegrityError, ValueError) as exc:
            logger.warning(
                f"[Vobiz/LiveKit] skipping auto-imported number {number}: {exc}"
            )
    return imported


async def _sync_livekit_dispatch_rules(
    *,
    credentials: dict[str, Any],
    phone_rows: list[Any],
    organization_id: int,
    telephony_configuration_id: int,
    config_name: str,
) -> dict[str, dict[str, Any]] | None:
    inbound_trunk_id = credentials.get("livekit_sip_inbound_trunk_id")
    if not inbound_trunk_id:
        return None

    grouped: dict[int, list[str]] = {}
    workflow_users: dict[int, int] = {}
    for phone in phone_rows:
        workflow_id = getattr(phone, "inbound_workflow_id", None)
        if not (getattr(phone, "is_active", False) and workflow_id):
            continue
        workflow = await db_client.get_workflow(
            workflow_id, organization_id=organization_id
        )
        if not workflow:
            continue
        grouped.setdefault(workflow_id, []).append(phone.address_normalized)
        workflow_users[workflow_id] = workflow.user_id

    stored = credentials.get("livekit_sip_dispatch_rules")
    if not isinstance(stored, dict):
        stored = {}

    next_rules: dict[str, dict[str, Any]] = {}
    settings = effective_livekit_settings()
    async with livekit_api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as lkapi:
        grouped_keys = {str(workflow_id) for workflow_id in grouped}
        for key, value in stored.items():
            if key in grouped_keys or not isinstance(value, dict):
                continue
            rule_id = value.get("sip_dispatch_rule_id")
            if not rule_id:
                continue
            try:
                await lkapi.sip.delete_sip_dispatch_rule(
                    livekit_api.DeleteSIPDispatchRuleRequest(
                        sip_dispatch_rule_id=rule_id
                    )
                )
            except TwirpError as exc:
                if exc.code != TwirpErrorCode.NOT_FOUND:
                    logger.warning(
                        f"[Vobiz/LiveKit] failed to delete dispatch rule {rule_id}: {exc}"
                    )

        for workflow_id, numbers in grouped.items():
            key = str(workflow_id)
            existing_id = None
            if isinstance(stored.get(key), dict):
                existing_id = stored[key].get("sip_dispatch_rule_id")
            target_numbers = _vobiz_sip_destination_numbers(_dedupe(numbers))
            created_or_updated = await _ensure_livekit_dispatch_rule(
                lkapi,
                rule_id=existing_id,
                workflow_id=workflow_id,
                user_id=workflow_users[workflow_id],
                organization_id=organization_id,
                telephony_configuration_id=telephony_configuration_id,
                trunk_id=inbound_trunk_id,
                inbound_numbers=target_numbers,
                target_numbers=target_numbers,
                name=f"{config_name} workflow {workflow_id}",
            )
            next_rules[key] = created_or_updated

    return next_rules


async def _ensure_livekit_outbound_trunk(
    lkapi: livekit_api.LiveKitAPI,
    *,
    trunk_id: str | None,
    name: str,
    address: str,
    username: str,
    password: str,
    numbers: list[str],
    metadata: str,
) -> str:
    if trunk_id:
        try:
            updated = await lkapi.sip.update_sip_outbound_trunk_fields(
                trunk_id,
                address=address,
                transport=livekit_api.SIP_TRANSPORT_AUTO,
                numbers=numbers,
                auth_username=username,
                auth_password=password,
                name=name,
                metadata=metadata,
            )
            return updated.sip_trunk_id
        except TwirpError as exc:
            if exc.code != TwirpErrorCode.NOT_FOUND:
                raise

    existing_id = await _find_livekit_outbound_trunk(
        lkapi,
        address=address,
        numbers=numbers,
        metadata=metadata,
    )
    if existing_id:
        return await _ensure_livekit_outbound_trunk(
            lkapi,
            trunk_id=existing_id,
            name=name,
            address=address,
            username=username,
            password=password,
            numbers=numbers,
            metadata=metadata,
        )

    created = await lkapi.sip.create_sip_outbound_trunk(
        livekit_api.CreateSIPOutboundTrunkRequest(
            trunk=livekit_api.SIPOutboundTrunkInfo(
                name=name,
                metadata=metadata,
                address=address,
                transport=livekit_api.SIP_TRANSPORT_AUTO,
                numbers=numbers,
                auth_username=username,
                auth_password=password,
            )
        )
    )
    return created.sip_trunk_id


async def _ensure_livekit_inbound_trunk(
    lkapi: livekit_api.LiveKitAPI,
    *,
    trunk_id: str | None,
    name: str,
    numbers: list[str],
    metadata: str,
) -> str:
    if trunk_id:
        try:
            updated = await lkapi.sip.update_sip_inbound_trunk_fields(
                trunk_id,
                numbers=numbers,
                name=name,
                metadata=metadata,
            )
            return updated.sip_trunk_id
        except TwirpError as exc:
            if exc.code != TwirpErrorCode.NOT_FOUND:
                raise

    existing_id = await _find_livekit_inbound_trunk(
        lkapi,
        numbers=numbers,
        metadata=metadata,
    )
    if existing_id:
        return await _ensure_livekit_inbound_trunk(
            lkapi,
            trunk_id=existing_id,
            name=name,
            numbers=numbers,
            metadata=metadata,
        )

    created = await lkapi.sip.create_sip_inbound_trunk(
        livekit_api.CreateSIPInboundTrunkRequest(
            trunk=livekit_api.SIPInboundTrunkInfo(
                name=name,
                metadata=metadata,
                numbers=numbers,
            )
        )
    )
    return created.sip_trunk_id


async def _find_livekit_outbound_trunk(
    lkapi: livekit_api.LiveKitAPI,
    *,
    address: str,
    numbers: list[str],
    metadata: str,
) -> str | None:
    response = await lkapi.sip.list_sip_outbound_trunk(
        livekit_api.ListSIPOutboundTrunkRequest()
    )
    for trunk in response.items:
        if _same_vobiz_config_metadata(trunk.metadata, metadata):
            return trunk.sip_trunk_id
    for trunk in response.items:
        if trunk.address == address and _numbers_overlap(list(trunk.numbers), numbers):
            return trunk.sip_trunk_id
    return None


async def _find_livekit_inbound_trunk(
    lkapi: livekit_api.LiveKitAPI,
    *,
    numbers: list[str],
    metadata: str,
) -> str | None:
    response = await lkapi.sip.list_sip_inbound_trunk(
        livekit_api.ListSIPInboundTrunkRequest()
    )
    for trunk in response.items:
        if _same_vobiz_config_metadata(trunk.metadata, metadata):
            return trunk.sip_trunk_id
    for trunk in response.items:
        if _numbers_overlap(list(trunk.numbers), numbers):
            return trunk.sip_trunk_id
    return None


async def _ensure_livekit_dispatch_rule(
    lkapi: livekit_api.LiveKitAPI,
    *,
    rule_id: str | None,
    workflow_id: int,
    user_id: int,
    organization_id: int,
    telephony_configuration_id: int,
    trunk_id: str,
    inbound_numbers: list[str],
    target_numbers: list[str],
    name: str,
) -> dict[str, Any]:
    metadata_dict = build_run_metadata(
        workflow_id=workflow_id,
        workflow_run_id=None,
        user_id=user_id,
        organization_id=organization_id,
        call_type="inbound",
        initial_context={
            "provider": "vobiz",
            "telephony_configuration_id": telephony_configuration_id,
        },
        extra={
            "provider": "vobiz",
            "telephony_configuration_id": telephony_configuration_id,
            "inbound_numbers": inbound_numbers,
            "target_numbers": target_numbers,
        },
    )
    metadata = _json_metadata(metadata_dict)
    settings = effective_livekit_settings()
    room_config = livekit_api.RoomConfiguration(
        agents=[
            livekit_api.RoomAgentDispatch(
                agent_name=settings.livekit_agent_name,
                metadata=metadata,
            )
        ],
        metadata=metadata,
    )
    rule = livekit_api.SIPDispatchRule(
        dispatch_rule_individual=livekit_api.SIPDispatchRuleIndividual(
            room_prefix=room_prefix_for_workflow(workflow_id),
        )
    )

    if rule_id:
        try:
            updated = await lkapi.sip.update_sip_dispatch_rule(
                rule_id,
                livekit_api.SIPDispatchRuleInfo(
                    sip_dispatch_rule_id=rule_id,
                    rule=rule,
                    trunk_ids=[trunk_id],
                    inbound_numbers=inbound_numbers,
                    name=name,
                    metadata=metadata,
                    room_config=room_config,
                ),
            )
            return {
                "sip_dispatch_rule_id": updated.sip_dispatch_rule_id,
                "workflow_id": workflow_id,
                "inbound_numbers": inbound_numbers,
                "target_numbers": target_numbers,
                "room_prefix": room_prefix_for_workflow(workflow_id),
            }
        except TwirpError as exc:
            if exc.code == TwirpErrorCode.NOT_FOUND:
                pass
            elif exc.code == TwirpErrorCode.INVALID_ARGUMENT and (
                "already exists" in str(exc).lower()
            ):
                await lkapi.sip.delete_sip_dispatch_rule(
                    livekit_api.DeleteSIPDispatchRuleRequest(
                        sip_dispatch_rule_id=rule_id
                    )
                )
            else:
                raise

    created = await lkapi.sip.create_sip_dispatch_rule(
        livekit_api.CreateSIPDispatchRuleRequest(
            rule=rule,
            trunk_ids=[trunk_id],
            inbound_numbers=inbound_numbers,
            name=name,
            metadata=metadata,
            room_config=room_config,
        )
    )
    return {
        "sip_dispatch_rule_id": created.sip_dispatch_rule_id,
        "workflow_id": workflow_id,
        "inbound_numbers": inbound_numbers,
        "target_numbers": target_numbers,
        "room_prefix": room_prefix_for_workflow(workflow_id),
    }


async def _create_vobiz_sip_credential(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    auth_token: str,
    username: str | None,
    password: str | None,
) -> dict[str, str]:
    username = username or f"spx_voice_lk_{uuid.uuid4().hex[:12]}"
    password = password or secrets.token_urlsafe(24)
    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/credentials"
    data = await _vobiz_request_json(
        session,
        "POST",
        endpoint,
        headers=_vobiz_headers(auth_id, auth_token),
        json_body={
            "username": username,
            "password": password,
        },
        expected_statuses=(200, 201),
    )
    credential_id = _first_string_value(
        data,
        ["credential_id", "credential_uuid", "id", "uuid"],
    )
    if not credential_id:
        raise HTTPException(
            status_code=502,
            detail=f"Vobiz credential response missing credential_id: {data}",
        )
    return {
        "vobiz_sip_credential_id": credential_id,
        "vobiz_sip_username": str(data.get("username") or username),
        "vobiz_sip_password": password,
        "vobiz_sip_realm": str(data.get("realm") or ""),
    }


async def _create_vobiz_sip_trunk(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    auth_token: str,
    name: str,
    credential_id: str | None,
) -> dict[str, str]:
    if not credential_id:
        raise HTTPException(
            status_code=502,
            detail="Vobiz SIP credential id is required before creating a trunk.",
        )

    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/trunks"
    body: dict[str, Any] = {
        "name": name[:64],
        "trunk_direction": "both",
        "transport": "udp",
        "credential_uuid": credential_id,
    }
    destination = livekit_sip_inbound_destination()
    if destination:
        body["inbound_destination"] = destination

    data = await _vobiz_request_json(
        session,
        "POST",
        endpoint,
        headers=_vobiz_headers(auth_id, auth_token),
        json_body=body,
        expected_statuses=(200, 201),
    )
    trunk_id = _first_string_value(data, ["trunk_id", "trunk_uuid", "id", "uuid"])
    domain = _first_string_value(
        data,
        ["trunk_domain", "sip_domain", "domain", "address"],
    )
    if not trunk_id or not domain:
        raise HTTPException(
            status_code=502,
            detail=f"Vobiz trunk response missing trunk_id/domain: {data}",
        )
    return {
        "vobiz_sip_trunk_id": trunk_id,
        "vobiz_sip_domain": domain,
    }


async def _update_vobiz_sip_trunk_destination(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    auth_token: str,
    trunk_id: str,
    inbound_destination: str,
) -> None:
    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/trunks/{trunk_id}"
    headers = _vobiz_headers(auth_id, auth_token)
    body = {"inbound_destination": inbound_destination}

    try:
        await _vobiz_request_json(
            session,
            "PUT",
            endpoint,
            headers=headers,
            json_body=body,
            expected_statuses=(200, 202),
        )
        return
    except HTTPException as exc:
        if exc.status_code not in (404, 405):
            logger.warning(
                f"[Vobiz/LiveKit] PUT trunk destination failed; trying PATCH: "
                f"{exc.detail}"
            )

    await _vobiz_request_json(
        session,
        "PATCH",
        endpoint,
        headers=headers,
        json_body=body,
        expected_statuses=(200, 202),
    )


async def _ensure_vobiz_phone_numbers_assigned(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    auth_token: str,
    trunk_id: str,
    phone_numbers: list[str],
) -> None:
    records = await _list_vobiz_phone_number_records(
        session,
        auth_id=auth_id,
        auth_token=auth_token,
    )
    headers = _vobiz_headers(auth_id, auth_token)
    for phone_number in _clean_phone_numbers(phone_numbers):
        record = records.get(phone_number) or {}
        assigned_trunk_id = _first_string_value(
            record,
            ["trunk_group_id", "trunk_id", "sip_trunk_id"],
        )
        assigned_application_id = _first_string_value(
            record,
            ["application_id", "app_id", "application_uuid"],
        )
        if assigned_trunk_id == trunk_id:
            continue
        if assigned_trunk_id:
            await _unassign_vobiz_phone_number(
                session,
                auth_id=auth_id,
                headers=headers,
                phone_number=phone_number,
            )
        if assigned_application_id:
            await _unassign_vobiz_phone_number_application(
                session,
                auth_id=auth_id,
                headers=headers,
                phone_number=phone_number,
            )
        await _assign_vobiz_phone_number(
            session,
            auth_id=auth_id,
            headers=headers,
            phone_number=phone_number,
            trunk_id=trunk_id,
        )
        logger.info(
            f"[Vobiz/LiveKit] assigned number {phone_number} to trunk {trunk_id}"
        )


async def _list_vobiz_phone_number_records(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    auth_token: str,
) -> dict[str, dict[str, Any]]:
    endpoint = f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/numbers"
    try:
        data = await _vobiz_request_json(
            session,
            "GET",
            endpoint,
            headers=_vobiz_headers(auth_id, auth_token),
            expected_statuses=(200,),
        )
    except HTTPException as exc:
        logger.warning(
            f"[Vobiz/LiveKit] could not inspect Vobiz number trunk assignments: "
            f"{exc.detail}"
        )
        return {}

    if isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data.get("data"), list):
        items = data["data"]
    elif isinstance(data.get("objects"), list):
        items = data["objects"]
    else:
        items = []

    records: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        number = _first_string_value(
            item,
            ["phone_number", "phoneNumber", "number", "msisdn", "cli", "did", "e164"],
        )
        if not number:
            continue
        normalized = _phone_number_for_storage(number)
        if normalized:
            records[normalized] = item
    return records


async def _unassign_vobiz_phone_number(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    headers: dict[str, str],
    phone_number: str,
) -> None:
    endpoint = _vobiz_number_assignment_endpoint(auth_id, phone_number)
    try:
        await _vobiz_request_json(
            session,
            "DELETE",
            endpoint,
            headers=headers,
            expected_statuses=(200, 202, 204),
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise


async def _unassign_vobiz_phone_number_application(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    headers: dict[str, str],
    phone_number: str,
) -> None:
    endpoint = (
        f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/numbers/"
        f"{quote(phone_number, safe='')}/application"
    )
    try:
        await _vobiz_request_json(
            session,
            "DELETE",
            endpoint,
            headers=headers,
            expected_statuses=(200, 202, 204),
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise


async def _assign_vobiz_phone_number(
    session: aiohttp.ClientSession,
    *,
    auth_id: str,
    headers: dict[str, str],
    phone_number: str,
    trunk_id: str,
) -> None:
    endpoint = _vobiz_number_assignment_endpoint(auth_id, phone_number)
    body = {"trunk_group_id": trunk_id}
    try:
        await _vobiz_request_json(
            session,
            "POST",
            endpoint,
            headers=headers,
            json_body=body,
            expected_statuses=(200, 201, 202, 204),
        )
    except HTTPException as exc:
        detail = str(exc.detail).lower()
        if exc.status_code != 400 or "already assigned" not in detail:
            raise
        if "application" in detail:
            await _unassign_vobiz_phone_number_application(
                session,
                auth_id=auth_id,
                headers=headers,
                phone_number=phone_number,
            )
        else:
            await _unassign_vobiz_phone_number(
                session,
                auth_id=auth_id,
                headers=headers,
                phone_number=phone_number,
            )
        await _vobiz_request_json(
            session,
            "POST",
            endpoint,
            headers=headers,
            json_body=body,
            expected_statuses=(200, 201, 202, 204),
        )


def _vobiz_number_assignment_endpoint(auth_id: str, phone_number: str) -> str:
    return (
        f"{VOBIZ_API_BASE_URL}/v1/Account/{auth_id}/numbers/"
        f"{quote(phone_number, safe='')}/assign"
    )


async def _vobiz_request_json(
    session: aiohttp.ClientSession,
    method: str,
    endpoint: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    expected_statuses: tuple[int, ...],
) -> dict[str, Any]:
    try:
        async with session.request(
            method, endpoint, json=json_body, headers=headers
        ) as response:
            text = await response.text()
            if response.status not in expected_statuses:
                logger.error(
                    f"[Vobiz/LiveKit] {method} {endpoint} failed: "
                    f"HTTP {response.status} body={text}"
                )
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Vobiz API {response.status}: {text}",
                )
            if not text:
                return {}
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=502,
                    detail=f"Vobiz API returned non-JSON response: {text}",
                )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach Vobiz API (timeout or network error): {exc}",
        ) from exc

    return _extract_response_object(body)


def _extract_response_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict):
        return data
    return body


def _vobiz_headers(auth_id: str, auth_token: str) -> dict[str, str]:
    return {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }


def _first_string_value(data: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def _phone_number_for_storage(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(("sip:", "sips:")):
        return raw
    compact = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if compact.startswith("+"):
        return compact
    if compact.isdigit() and 8 <= len(compact) <= 15:
        return f"+{compact}"
    return raw


def _clean_phone_numbers(values: list[str]) -> list[str]:
    return _dedupe([_phone_number_for_storage(v) for v in values if v])


def _livekit_inbound_number_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    for value in values:
        number = _phone_number_for_storage(value)
        if not number:
            continue
        aliases.append(number)
        if number.startswith("+"):
            without_plus = number[1:]
            aliases.append(without_plus)
            if without_plus.startswith("91") and len(without_plus) == 12:
                local = without_plus[2:]
                aliases.extend([local, f"0{local}"])
        elif number.startswith("91") and len(number) == 12:
            local = number[2:]
            aliases.extend([f"+{number}", local, f"0{local}"])
        elif number.startswith("0") and len(number) == 11:
            local = number[1:]
            aliases.extend([local, f"+91{local}", f"91{local}"])
        elif number.isdigit() and len(number) == 10:
            aliases.extend([f"0{number}", f"+91{number}", f"91{number}"])
    return _dedupe(aliases)


def _vobiz_sip_destination_numbers(values: list[str]) -> list[str]:
    destinations: list[str] = []
    for value in values:
        number = _phone_number_for_storage(value)
        if not number:
            continue
        if number.startswith("+91") and len(number) == 13:
            destinations.append(f"0{number[3:]}")
        elif number.startswith("91") and len(number) == 12:
            destinations.append(f"0{number[2:]}")
        else:
            destinations.append(number)
    return _dedupe(destinations)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _numbers_overlap(left: list[str], right: list[str]) -> bool:
    if not left or not right:
        return False
    return bool(set(left) & set(right))


def _same_vobiz_config_metadata(left: str, right: str) -> bool:
    left_data = _parse_metadata(left)
    right_data = _parse_metadata(right)
    if not left_data or not right_data:
        return False
    return (
        left_data.get("provider") == "vobiz"
        and right_data.get("provider") == "vobiz"
        and left_data.get("telephony_configuration_id")
        == right_data.get("telephony_configuration_id")
        and left_data.get("organization_id") == right_data.get("organization_id")
    )


def _parse_metadata(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _metadata(
    *,
    auth_id: str,
    organization_id: int | None,
    telephony_configuration_id: int | None,
) -> str:
    return _json_metadata(
        {
            "provider": "vobiz",
            "vobiz_auth_id": auth_id,
            "organization_id": organization_id,
            "telephony_configuration_id": telephony_configuration_id,
        }
    )


def _json_metadata(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)

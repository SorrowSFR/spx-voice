from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger

from api.services.livekit.runtime_config import effective_livekit_settings


POST_CALL_WEBHOOK_ENV = "SPX_VOICE_POST_CALL_WEBHOOK_URL"
RECORDINGS_S3_ENDPOINT_ENV = "SPX_VOICE_RECORDINGS_S3_ENDPOINT_URL"
RECORDINGS_S3_BUCKET_ENV = "SPX_VOICE_RECORDINGS_S3_BUCKET"
RECORDINGS_S3_ACCESS_KEY_ENV = "SPX_VOICE_RECORDINGS_S3_ACCESS_KEY_ID"
RECORDINGS_S3_SECRET_KEY_ENV = "SPX_VOICE_RECORDINGS_S3_SECRET_ACCESS_KEY"
RECORDINGS_S3_REGION_ENV = "SPX_VOICE_RECORDINGS_S3_REGION"
RECORDINGS_S3_FORCE_PATH_STYLE_ENV = "SPX_VOICE_RECORDINGS_S3_FORCE_PATH_STYLE"
RECORDINGS_PUBLIC_BASE_ENV = "SPX_VOICE_RECORDINGS_PUBLIC_BASE_URL"
RECORDINGS_KEY_PREFIX_ENV = "SPX_VOICE_RECORDINGS_KEY_PREFIX"


LEAD_FIELDS = ("district", "town", "looking_for", "customer_name", "remarks")
REQUIRED_LEAD_FIELDS = LEAD_FIELDS
WEBHOOK_TIMEOUT_SECONDS = 8.0
DEFAULT_RECORDING_REGION = "us-east-1"
DEFAULT_RECORDING_PREFIX = "SPX-VOICE-INBOUND"

_WHITESPACE_RE = re.compile(r"\s+")
_NON_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PLACEHOLDER_LEAD_VALUE_RE = re.compile(
    r"^(?:not\s+(?:provided|available|known|shared|collected)|"
    r"unknown|n/?a|none|null|nil|refused|declined|"
    r"did\s+not\s+provide|caller\s+did\s+not\s+provide)$",
    re.IGNORECASE,
)
_REFUSAL_NOTE_RE = re.compile(
    r"(?:refus(?:ed|es|al)|declin(?:ed|es)|not\s+provided|"
    r"did\s+not\s+provide|does\s+not\s+know|don't\s+know|"
    r"not\s+shared|unavailable|not\s+available|not\s+collected|"
    r"\u0c1a\u0c46\u0c2a\u0c4d\u0c2a\u0c32\u0c47\u0c26\u0c41|"
    r"\u0c24\u0c46\u0c32\u0c3f\u0c2f\u0c26\u0c41)",
    re.IGNORECASE,
)
_FIELD_REFUSAL_ALIASES: dict[str, tuple[str, ...]] = {
    "district": ("district", "jilla", "zilla"),
    "town": ("town", "village", "mandal", "locality", "location"),
    "looking_for": ("looking for", "looking_for", "requirement", "enquiry"),
    "customer_name": ("customer name", "customer_name", "caller name", "name"),
}


@dataclass(frozen=True)
class LiveKitRecordingState:
    egress_id: str
    recording_key: str
    recording_url: str
    status: str
    error: str | None = None
    details: str | None = None
    file_results: list[dict[str, Any]] | None = None

    def to_log(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RecordingConfig:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str
    force_path_style: bool
    public_base_url: str
    key_prefix: str


def post_call_webhook_url() -> str:
    return _env_first(POST_CALL_WEBHOOK_ENV)


def post_call_enabled() -> bool:
    return bool(post_call_webhook_url())


def normalize_lead_value(value: Any) -> str:
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


def is_placeholder_lead_value(value: Any) -> bool:
    cleaned = normalize_lead_value(value).strip(" .,:;_-")
    return bool(cleaned and _PLACEHOLDER_LEAD_VALUE_RE.fullmatch(cleaned))


def _lead_field_has_refusal_note(
    lead: dict[str, Any] | None,
    field: str,
) -> bool:
    if field == "remarks":
        return False
    remarks = normalize_lead_value((lead or {}).get("remarks"))
    if not remarks or not _REFUSAL_NOTE_RE.search(remarks):
        return False

    remarks_for_match = remarks.lower().replace("_", " ")
    aliases = _FIELD_REFUSAL_ALIASES.get(field, (field.replace("_", " "),))
    return any(alias.replace("_", " ") in remarks_for_match for alias in aliases)


def is_missing_lead_value(
    value: Any,
    *,
    lead: dict[str, Any] | None = None,
    field: str | None = None,
) -> bool:
    cleaned = normalize_lead_value(value)
    if not cleaned:
        return True
    if is_placeholder_lead_value(cleaned):
        return not (
            field is not None and _lead_field_has_refusal_note(lead, field)
        )
    return False


def extract_lead_details(gathered_context: dict[str, Any] | None) -> dict[str, str]:
    gathered_context = gathered_context or {}
    sources = [gathered_context]
    nested = gathered_context.get("lead_details")
    if isinstance(nested, dict):
        sources.insert(0, nested)

    lead = {field: "" for field in LEAD_FIELDS}
    for source in sources:
        for field in LEAD_FIELDS:
            value = source.get(field)
            if value in (None, "") and field == "looking_for":
                value = source.get("looking for")
            cleaned = normalize_lead_value(value)
            if cleaned:
                lead[field] = cleaned
    return lead


def merge_lead_details(
    existing: dict[str, Any] | None,
    updates: dict[str, Any] | None,
) -> dict[str, str]:
    lead = {field: normalize_lead_value((existing or {}).get(field)) for field in LEAD_FIELDS}
    for field in LEAD_FIELDS:
        value = normalize_lead_value((updates or {}).get(field))
        if value:
            current = lead[field]
            if (
                is_placeholder_lead_value(value)
                and current
                and not is_placeholder_lead_value(current)
            ):
                continue
            lead[field] = value
    return lead


def missing_lead_fields(lead: dict[str, Any] | None) -> list[str]:
    lead = lead or {}
    return [
        field
        for field in REQUIRED_LEAD_FIELDS
        if is_missing_lead_value(lead.get(field), lead=lead, field=field)
    ]


def lead_details_gathered_context(lead: dict[str, Any]) -> dict[str, Any]:
    normalized = {field: normalize_lead_value(lead.get(field)) for field in LEAD_FIELDS}
    return {
        "lead_details": normalized,
        **normalized,
        "looking for": normalized["looking_for"],
    }


def _truthy_env(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def _recording_config_from_env() -> _RecordingConfig | None:
    endpoint_url = _env_first(RECORDINGS_S3_ENDPOINT_ENV).rstrip("/")
    bucket = _env_first(RECORDINGS_S3_BUCKET_ENV)
    access_key_id = _env_first(RECORDINGS_S3_ACCESS_KEY_ENV)
    secret_access_key = _env_first(RECORDINGS_S3_SECRET_KEY_ENV)
    if not (endpoint_url and bucket and access_key_id and secret_access_key):
        return None

    return _RecordingConfig(
        endpoint_url=endpoint_url,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=_env_first(RECORDINGS_S3_REGION_ENV, default=DEFAULT_RECORDING_REGION)
        or DEFAULT_RECORDING_REGION,
        force_path_style=_truthy_env(RECORDINGS_S3_FORCE_PATH_STYLE_ENV, default=False),
        public_base_url=(
            _env_first(RECORDINGS_PUBLIC_BASE_ENV).rstrip("/")
            or endpoint_url
        ),
        key_prefix=(
            _env_first(RECORDINGS_KEY_PREFIX_ENV, default=DEFAULT_RECORDING_PREFIX)
            .strip("/")
            or DEFAULT_RECORDING_PREFIX
        ),
    )


def _safe_key_part(value: str) -> str:
    cleaned = _NON_KEY_RE.sub("_", value.strip())
    return cleaned.strip("_") or "room"


def _recording_key(room_name: str, workflow_run_id: int, prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    room_part = _safe_key_part(room_name)[-80:]
    return f"{prefix}/{workflow_run_id}-{room_part}-{stamp}.mp3"


def _public_recording_url(config: _RecordingConfig, key: str) -> str:
    return f"{config.public_base_url}/{key}"


def _recording_key_prefix() -> str:
    return (
        _env_first(RECORDINGS_KEY_PREFIX_ENV, default=DEFAULT_RECORDING_PREFIX)
        .strip("/")
        or DEFAULT_RECORDING_PREFIX
    )


def recording_object_key_from_url_or_key(value: str | None) -> str:
    raw = normalize_lead_value(value)
    if not raw:
        return ""

    prefix = _recording_key_prefix()
    if raw.startswith(f"{prefix}/"):
        return raw

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return ""

    public_base = _env_first(RECORDINGS_PUBLIC_BASE_ENV).rstrip("/")
    if public_base and raw.startswith(f"{public_base}/"):
        candidate = raw[len(public_base) + 1 :]
        return unquote(candidate) if candidate.startswith(f"{prefix}/") else ""

    candidate = unquote(parsed.path.lstrip("/"))
    return candidate if candidate.startswith(f"{prefix}/") else ""


def workflow_run_id_from_recording_key(key: str) -> int | None:
    prefix = re.escape(_recording_key_prefix())
    match = re.match(rf"^{prefix}/(\d+)-", key or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def generate_recording_signed_url(
    value: str,
    *,
    expiration: int = 3600,
    force_inline: bool = False,
) -> str | None:
    key = recording_object_key_from_url_or_key(value)
    config = _recording_config_from_env()
    if not key or config is None:
        return None

    from botocore.config import Config
    import aioboto3

    response_params: dict[str, str] = {}
    if force_inline:
        if key.lower().endswith(".mp3"):
            response_params["ResponseContentType"] = "audio/mpeg"
        elif key.lower().endswith(".wav"):
            response_params["ResponseContentType"] = "audio/wav"
        response_params["ResponseContentDisposition"] = "inline"

    addressing_style = "path" if config.force_path_style else "virtual"
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name=config.region,
        config=Config(
            s3={"addressing_style": addressing_style},
            signature_version="s3v4",
        ),
    ) as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.bucket, "Key": key, **response_params},
            ExpiresIn=expiration,
        )


def _plain_proto_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list) or type(value).__name__ == "RepeatedCompositeContainer":
        return [_plain_proto_value(item) for item in value]
    descriptor = getattr(value, "DESCRIPTOR", None)
    if descriptor is not None:
        return {
            field.name: _plain_proto_value(getattr(value, field.name))
            for field in descriptor.fields
        }
    return str(value)


def _egress_status_name(livekit_api: Any, status: Any) -> str:
    try:
        return livekit_api.EgressStatus.Name(status)
    except Exception:
        return str(status or "")


def _terminal_egress_status(status_name: str) -> bool:
    return any(
        marker in status_name
        for marker in ("COMPLETE", "FAILED", "ABORTED", "LIMIT_REACHED")
    )


def _recording_state_from_egress_info(
    livekit_api: Any,
    info: Any,
    *,
    recording_key: str,
    recording_url: str,
    fallback_status: str,
) -> LiveKitRecordingState:
    status_name = _egress_status_name(livekit_api, getattr(info, "status", None))
    error = normalize_lead_value(getattr(info, "error", None)) or None
    details = normalize_lead_value(getattr(info, "details", None)) or None
    status = status_name.lower() if status_name else fallback_status
    if error or "FAILED" in status_name or "ABORTED" in status_name:
        status = "failed"
    elif "COMPLETE" in status_name:
        status = "complete"
    file_results = [
        _plain_proto_value(result) for result in getattr(info, "file_results", []) or []
    ]
    return LiveKitRecordingState(
        egress_id=str(getattr(info, "egress_id", "") or ""),
        recording_key=recording_key,
        recording_url=recording_url,
        status=status,
        error=error,
        details=details,
        file_results=file_results or None,
    )


async def _wait_for_final_egress_info(
    lkapi: Any,
    livekit_api: Any,
    *,
    egress_id: str,
    initial_info: Any,
    attempts: int = 8,
) -> Any:
    info = initial_info
    for attempt in range(attempts):
        status_name = _egress_status_name(livekit_api, getattr(info, "status", None))
        if _terminal_egress_status(status_name):
            return info
        if attempt:
            await asyncio.sleep(1.0)
        response = await lkapi.egress.list_egress(
            livekit_api.ListEgressRequest(egress_id=egress_id)
        )
        if response.items:
            info = response.items[0]
    return info


def recording_available_url(
    state: LiveKitRecordingState | dict[str, Any] | None,
) -> str:
    if state is None:
        return ""
    if isinstance(state, LiveKitRecordingState):
        status = state.status
        url = state.recording_url
    elif isinstance(state, dict):
        status = normalize_lead_value(state.get("status"))
        url = normalize_lead_value(state.get("recording_url"))
    else:
        return ""
    return url if status in {"complete", "stopped"} else ""


async def start_livekit_room_recording(
    *,
    room_name: str,
    workflow_run_id: int,
) -> LiveKitRecordingState | None:
    config = _recording_config_from_env()
    if config is None:
        return None

    settings = effective_livekit_settings()
    if not settings.configured:
        logger.warning("[LiveKit] recording requested but LiveKit API is not configured")
        return LiveKitRecordingState(
            egress_id="",
            recording_key="",
            recording_url="",
            status="not_started",
            error="LiveKit API is not configured",
        )

    key = _recording_key(room_name, workflow_run_id, config.key_prefix)
    public_url = _public_recording_url(config, key)
    try:
        from livekit import api as livekit_api

        async with livekit_api.LiveKitAPI(
            settings.livekit_url,
            settings.livekit_api_key,
            settings.livekit_api_secret,
        ) as lkapi:
            info = await lkapi.egress.start_room_composite_egress(
                livekit_api.RoomCompositeEgressRequest(
                    room_name=room_name,
                    audio_only=True,
                    audio_mixing=livekit_api.AudioMixing.DUAL_CHANNEL_AGENT,
                    file_outputs=[
                        livekit_api.EncodedFileOutput(
                            file_type=livekit_api.EncodedFileType.MP3,
                            filepath=key,
                            s3=livekit_api.S3Upload(
                                endpoint=config.endpoint_url,
                                bucket=config.bucket,
                                access_key=config.access_key_id,
                                secret=config.secret_access_key,
                                region=config.region,
                                force_path_style=config.force_path_style,
                            ),
                        )
                    ],
                )
            )
        egress_id = str(getattr(info, "egress_id", "") or "")
        logger.info(
            "[LiveKit] started room recording "
            f"run_id={workflow_run_id} room={room_name!r} egress_id={egress_id!r}"
        )
        return _recording_state_from_egress_info(
            livekit_api,
            info,
            recording_key=key,
            recording_url=public_url,
            fallback_status="started",
        ) or LiveKitRecordingState(
            egress_id=egress_id,
            recording_key=key,
            recording_url=public_url,
            status="started",
        )
    except Exception as exc:
        logger.error(f"[LiveKit] failed to start room recording: {exc}")
        return LiveKitRecordingState(
            egress_id="",
            recording_key=key,
            recording_url=public_url,
            status="not_started",
            error=str(exc),
        )


async def stop_livekit_room_recording(
    state: LiveKitRecordingState | None,
) -> LiveKitRecordingState | None:
    if state is None or not state.egress_id or state.status == "stopped":
        return state

    settings = effective_livekit_settings()
    try:
        from livekit import api as livekit_api

        async with livekit_api.LiveKitAPI(
            settings.livekit_url,
            settings.livekit_api_key,
            settings.livekit_api_secret,
        ) as lkapi:
            info = await lkapi.egress.stop_egress(
                livekit_api.StopEgressRequest(egress_id=state.egress_id)
            )
            info = await _wait_for_final_egress_info(
                lkapi,
                livekit_api,
                egress_id=state.egress_id,
                initial_info=info,
            )
        logger.info(
            "[LiveKit] stopped room recording "
            f"egress_id={state.egress_id!r} key={state.recording_key!r}"
        )
        return _recording_state_from_egress_info(
            livekit_api,
            info,
            recording_key=state.recording_key,
            recording_url=state.recording_url,
            fallback_status="stopped",
        )
    except Exception as exc:
        error_text = str(exc)
        if "EGRESS_COMPLETE" in error_text or "failed_precondition" in error_text:
            try:
                from livekit import api as livekit_api

                async with livekit_api.LiveKitAPI(
                    settings.livekit_url,
                    settings.livekit_api_key,
                    settings.livekit_api_secret,
                ) as lkapi:
                    response = await lkapi.egress.list_egress(
                        livekit_api.ListEgressRequest(egress_id=state.egress_id)
                    )
                if response.items:
                    return _recording_state_from_egress_info(
                        livekit_api,
                        response.items[0],
                        recording_key=state.recording_key,
                        recording_url=state.recording_url,
                        fallback_status="complete",
                    )
            except Exception as list_exc:
                logger.warning(
                    "[LiveKit] failed to inspect completed room recording: "
                    f"{list_exc}"
                )
        logger.warning(f"[LiveKit] failed to stop room recording: {exc}")
        return LiveKitRecordingState(
            egress_id=state.egress_id,
            recording_key=state.recording_key,
            recording_url=state.recording_url,
            status="stop_failed",
            error=str(exc),
        )


def _first_present(*values: Any) -> str:
    for value in values:
        cleaned = normalize_lead_value(value)
        if cleaned:
            return cleaned
    return ""


def _participant_attrs(initial_context: dict[str, Any]) -> dict[str, Any]:
    attrs = initial_context.get("participant_attributes")
    return attrs if isinstance(attrs, dict) else {}


def _iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    cleaned = normalize_lead_value(value)
    if cleaned:
        return cleaned
    return datetime.now(timezone.utc).isoformat()


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(normalize_lead_value(part.get("text")))
        return " ".join(part for part in parts if part).strip()
    return ""


def _conversation_texts(logs: dict[str, Any] | None, role: str) -> list[str]:
    logs = logs or {}
    texts: list[str] = []
    for event in logs.get("realtime_feedback_events") or []:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if role == "user" and event.get("type") == "rtf-user-transcription":
            if payload.get("final") is False:
                continue
            text = normalize_lead_value(payload.get("text"))
            if text:
                texts.append(text)
        elif role == "assistant" and event.get("type") == "rtf-bot-text":
            text = normalize_lead_value(payload.get("text"))
            if text:
                texts.append(text)

    history = logs.get("livekit_history")
    if isinstance(history, dict):
        for item in history.get("items") or []:
            if not isinstance(item, dict) or item.get("role") != role:
                continue
            text = normalize_lead_value(_text_from_content(item.get("content")))
            if text:
                texts.append(text)
    return texts


_DISTRICT_ALIASES: dict[str, tuple[str, ...]] = {}


def _infer_district(text: str) -> str:
    lowered = text.lower()
    for district, aliases in _DISTRICT_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            return district
    return ""


def _infer_name(text: str) -> str:
    patterns = [
        r"(?:my name is|name is|i am)\s+([A-Za-z][A-Za-z .]{1,40})",
        r"\u0c28\u0c3e\s+\u0c2a\u0c47\u0c30\u0c41\s+([\u0c00-\u0c7fA-Za-z .]{2,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw_name = match.group(1)
            raw_name = re.split(r"[.,;:]", raw_name, maxsplit=1)[0]
            raw_name = re.split(
                r"\b(?:town|village|district|mandal|location|need|want|"
                r"subsidy|cost|registration)\b",
                raw_name,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            return normalize_lead_value(raw_name.strip(" .,:;"))
    return ""


def _infer_town(text: str) -> str:
    patterns = [
        r"(?:town|village|location|mandal)\s*(?:is|:)?\s+([A-Za-z][A-Za-z .-]{1,50})",
        r"(?:\u0c0a\u0c30\u0c41|\u0c17\u0c4d\u0c30\u0c3e\u0c2e\u0c02|\u0c32\u0c4a\u0c15\u0c47\u0c37\u0c28\u0c4d|\u0c2e\u0c02\u0c21\u0c32\u0c02)\s+([\u0c00-\u0c7fA-Za-z .-]{2,50})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_lead_value(match.group(1).strip(" .,:;"))
    return ""


def _infer_looking_for(text: str) -> str:
    lowered = text.lower()
    parts: list[str] = []
    # Match against the original text so the capacity token keeps its casing
    # (e.g. "15 kW", not "15 kw").
    match = re.search(r"\b\d+\s*(?:kw|k w|kilowatt)", text, re.IGNORECASE)
    if match:
        parts.append(match.group(0))
    if "shop" in lowered:
        parts.append("shop enquiry")
    if "subsid" in lowered:
        parts.append("subsidy information")
    if "registration" in lowered or "register" in lowered or "\u0c30\u0c3f\u0c1c\u0c3f\u0c38\u0c4d\u0c1f\u0c4d\u0c30" in text:
        parts.append("registration process")
    if "cost" in lowered or "rate" in lowered or "\u0c16\u0c30\u0c4d\u0c1a" in text:
        parts.append("cost estimate")
    if "vendor" in lowered or "\u0c35\u0c46\u0c02\u0c21\u0c30\u0c4d" in text:
        parts.append("vendor details")
    if not parts and text.strip():
        parts.append("general enquiry")
    return ", ".join(dict.fromkeys(parts))


def _lead_value_appears_in_user_text(value: Any, user_text: str) -> bool:
    cleaned = normalize_lead_value(value)
    text = normalize_lead_value(user_text)
    if not cleaned or not text:
        return False
    return cleaned.lower() in text.lower()


def user_evidence_supports_lead_value(
    field: str,
    value: Any,
    user_text: str,
) -> bool:
    if field not in LEAD_FIELDS:
        return True
    cleaned = normalize_lead_value(value)
    if not cleaned or is_placeholder_lead_value(cleaned):
        return True
    text = normalize_lead_value(user_text)
    if not text:
        return True
    if field == "district":
        inferred = _infer_district(text)
        return inferred == cleaned or _lead_value_appears_in_user_text(cleaned, text)
    if field in {"town", "customer_name"}:
        return _lead_value_appears_in_user_text(cleaned, text)
    return True


def infer_lead_details_from_logs(logs: dict[str, Any] | None) -> dict[str, str]:
    user_text = " ".join(_conversation_texts(logs, "user"))
    lead = {field: "" for field in LEAD_FIELDS}
    if not user_text.strip():
        return lead
    lead["district"] = _infer_district(user_text)
    lead["town"] = _infer_town(user_text)
    lead["looking_for"] = _infer_looking_for(user_text)
    lead["customer_name"] = _infer_name(user_text)
    if lead["looking_for"]:
        lead["remarks"] = f"Caller asked about {lead['looking_for']}."
    else:
        lead["remarks"] = "Caller discussed the configured workflow."
    return lead


def build_post_call_payload(
    workflow_run: Any,
    *,
    duration_seconds: int | None = None,
    logs: dict[str, Any] | None = None,
    recording_url: str | None = None,
) -> dict[str, Any]:
    initial_context = dict(getattr(workflow_run, "initial_context", None) or {})
    gathered_context = dict(getattr(workflow_run, "gathered_context", None) or {})
    attrs = _participant_attrs(initial_context)
    cost_info = dict(getattr(workflow_run, "cost_info", None) or {})
    logs = logs or dict(getattr(workflow_run, "logs", None) or {})

    lead = extract_lead_details(gathered_context)
    inferred = infer_lead_details_from_logs(logs)
    user_text = " ".join(_conversation_texts(logs, "user"))
    for field in LEAD_FIELDS:
        if is_missing_lead_value(lead.get(field), lead=lead, field=field):
            lead[field] = inferred.get(field) or ""
        elif field in {"district", "town", "customer_name"} and user_text:
            if not user_evidence_supports_lead_value(field, lead[field], user_text):
                lead[field] = inferred.get(field) or ""
        if is_missing_lead_value(lead.get(field), lead=lead, field=field):
            lead[field] = ""

    recording_state = gathered_context.get("livekit_recording")
    if isinstance(recording_state, dict):
        gathered_recording_url = recording_available_url(recording_state)
    else:
        gathered_recording_url = ""

    duration = duration_seconds
    if duration is None:
        raw_duration = cost_info.get("call_duration_seconds")
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError):
            duration = 0

    payload = {
        "customer_number": _first_present(
            initial_context.get("customer_number"),
            initial_context.get("caller_number"),
            attrs.get("sip.phoneNumber"),
            attrs.get("sip.from"),
            attrs.get("livekit.sip.phoneNumber"),
        ),
        "rep_number": _first_present(
            initial_context.get("rep_number"),
            initial_context.get("called_number"),
            attrs.get("sip.trunkPhoneNumber"),
            attrs.get("sip.to"),
            attrs.get("livekit.sip.trunkPhoneNumber"),
        ),
        "called_at": _iso_datetime(getattr(workflow_run, "created_at", None)),
        "duration": duration,
        "district": lead["district"],
        "town": lead["town"],
        "looking_for": lead["looking_for"],
        "looking for": lead["looking_for"],
        "customer_name": lead["customer_name"],
        "remarks": lead["remarks"],
        "recording_url": _first_present(
            recording_url,
            getattr(workflow_run, "recording_url", None),
            gathered_context.get("recording_url"),
            gathered_recording_url,
        ),
    }
    return payload


async def send_post_call_webhook(
    payload: dict[str, Any],
    *,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    url = (webhook_url or post_call_webhook_url()).strip()
    if not url:
        return {"status": "disabled"}

    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
        ok = 200 <= response.status_code < 300
        result = {
            "status": "sent" if ok else "failed",
            "status_code": response.status_code,
            "response": response.text[:500],
        }
        if ok:
            logger.info("[LiveKit] post-call webhook sent")
        else:
            logger.warning(
                "[LiveKit] post-call webhook failed "
                f"status_code={response.status_code} response={response.text[:200]!r}"
            )
        return result
    except Exception as exc:
        logger.error(f"[LiveKit] post-call webhook error: {exc}")
        return {"status": "error", "error": str(exc)}

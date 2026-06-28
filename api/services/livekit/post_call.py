from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from api.services.livekit.runtime_config import effective_livekit_settings


RECORDINGS_S3_ENDPOINT_ENV = "SPX_VOICE_RECORDINGS_S3_ENDPOINT_URL"
RECORDINGS_S3_BUCKET_ENV = "SPX_VOICE_RECORDINGS_S3_BUCKET"
RECORDINGS_S3_ACCESS_KEY_ENV = "SPX_VOICE_RECORDINGS_S3_ACCESS_KEY_ID"
RECORDINGS_S3_SECRET_KEY_ENV = "SPX_VOICE_RECORDINGS_S3_SECRET_ACCESS_KEY"
RECORDINGS_S3_REGION_ENV = "SPX_VOICE_RECORDINGS_S3_REGION"
RECORDINGS_S3_FORCE_PATH_STYLE_ENV = "SPX_VOICE_RECORDINGS_S3_FORCE_PATH_STYLE"
RECORDINGS_PUBLIC_BASE_ENV = "SPX_VOICE_RECORDINGS_PUBLIC_BASE_URL"
RECORDINGS_KEY_PREFIX_ENV = "SPX_VOICE_RECORDINGS_KEY_PREFIX"


DEFAULT_RECORDING_REGION = "us-east-1"
DEFAULT_RECORDING_PREFIX = "SPX-VOICE-INBOUND"

_WHITESPACE_RE = re.compile(r"\s+")
_NON_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


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
    raw = _normalize_text(value)
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
    error = _normalize_text(getattr(info, "error", None)) or None
    details = _normalize_text(getattr(info, "details", None)) or None
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
        status = _normalize_text(state.get("status"))
        url = _normalize_text(state.get("recording_url"))
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

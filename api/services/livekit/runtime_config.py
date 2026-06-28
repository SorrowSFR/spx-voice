from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from loguru import logger

from api.constants import (
    APP_ROOT_DIR,
    LIVEKIT_AGENT_NAME,
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_CLIENT_URL,
    LIVEKIT_ROOM_PREFIX,
    LIVEKIT_SIP_DEFAULT_FROM_NUMBER,
    LIVEKIT_SIP_INBOUND_HOST,
    LIVEKIT_SIP_MAX_CALL_DURATION_SECONDS,
    LIVEKIT_SIP_OUTBOUND_TRUNK_ID,
    LIVEKIT_TOKEN_TTL_SECONDS,
    LIVEKIT_URL,
    VOICE_RUNTIME,
)

SETTINGS_DIR = APP_ROOT_DIR / ".runtime"
SETTINGS_PATH = SETTINGS_DIR / "livekit_settings.json"


@dataclass(frozen=True)
class LiveKitRuntimeSettings:
    voice_runtime: str = "pipecat"
    livekit_url: str = ""
    livekit_client_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_agent_name: str = "spx-voice"
    livekit_room_prefix: str = "spx-voice"
    livekit_token_ttl_seconds: int = 3600
    livekit_sip_outbound_trunk_id: str = ""
    livekit_sip_default_from_number: str = ""
    livekit_sip_inbound_host: str = ""
    livekit_sip_max_call_duration_seconds: int = 1800
    source: str = "env"

    @property
    def is_livekit(self) -> bool:
        return self.voice_runtime == "livekit"

    @property
    def configured(self) -> bool:
        return bool(
            self.livekit_url and self.livekit_api_key and self.livekit_api_secret
        )

    @property
    def browser_url(self) -> str:
        url = (self.livekit_client_url or self.livekit_url).strip()
        if url.startswith("https://"):
            return "wss://" + url[len("https://") :]
        if url.startswith("http://"):
            return "ws://" + url[len("http://") :]
        return url

    @property
    def sip_inbound_destination(self) -> str:
        value = self.livekit_sip_inbound_host.strip()
        for prefix in ("sip:", "sips:"):
            if value.lower().startswith(prefix):
                value = value[len(prefix) :]
                break
        return value.strip().strip("/")

    def worker_signature(self) -> str:
        return json.dumps(
            {
                "voice_runtime": self.voice_runtime,
                "livekit_url": self.livekit_url,
                "livekit_api_key": self.livekit_api_key,
                "livekit_api_secret": self.livekit_api_secret,
                "livekit_agent_name": self.livekit_agent_name,
            },
            sort_keys=True,
        )


def effective_livekit_settings() -> LiveKitRuntimeSettings:
    base = _settings_from_env()
    saved = _read_saved_settings()
    if not saved:
        return base
    merged = asdict(base)
    merged.update({k: v for k, v in saved.items() if v is not None})
    merged["voice_runtime"] = _normal_runtime(merged.get("voice_runtime"))
    merged["source"] = "ui"
    return _settings_from_dict(merged)


def save_livekit_settings(values: dict[str, Any]) -> LiveKitRuntimeSettings:
    current = asdict(effective_livekit_settings())
    current.pop("source", None)
    for key, value in values.items():
        if value is None:
            continue
        current[key] = value
    current["voice_runtime"] = _normal_runtime(current.get("voice_runtime"))
    settings = _settings_from_dict(current, source="ui")

    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(asdict(settings) | {"source": "ui"}, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(SETTINGS_PATH)
    return settings


def is_livekit_runtime() -> bool:
    return effective_livekit_settings().is_livekit


def is_pipecat_runtime() -> bool:
    return not is_livekit_runtime()


# Providers the LiveKit worker (api/services/livekit/worker.py:_create_session)
# can actually instantiate. The provider registry advertises many more, but a
# provider that is not in this set validates and saves fine yet silently dies at
# call time with "LiveKit <X> provider is unsupported". We gate the model UI and
# config saves against this set when LiveKit is the active runtime so that
# unsupported choices fail loudly up front. Keep this in sync with
# `_create_session`. (Note: Google STT/TTS are listed here but are currently not
# registered in the configuration registry, so intersecting the registry with
# this set yields only OpenAI for the pipeline STT/TTS sections.)
LIVEKIT_SUPPORTED_PROVIDERS: dict[str, frozenset[str]] = {
    "llm": frozenset({"openai", "google"}),
    "stt": frozenset({"openai", "google"}),
    "tts": frozenset({"openai", "google"}),
    "realtime": frozenset(
        {"openai_realtime", "google_realtime", "google_vertex_realtime"}
    ),
}


def livekit_supports_provider(service: str, provider: str) -> bool:
    """Return True if the LiveKit worker can run *provider* for *service*.

    Services not gated here (e.g. ``embeddings``, which the worker does not use)
    always return True.
    """

    supported = LIVEKIT_SUPPORTED_PROVIDERS.get(service)
    if supported is None:
        return True
    return provider in supported


def livekit_configured() -> bool:
    return effective_livekit_settings().configured


def livekit_environment(settings: LiveKitRuntimeSettings | None = None) -> dict[str, str]:
    settings = settings or effective_livekit_settings()
    env = os.environ.copy()
    env.update(
        {
            "VOICE_RUNTIME": settings.voice_runtime,
            "LIVEKIT_URL": settings.livekit_url,
            "LIVEKIT_CLIENT_URL": settings.livekit_client_url,
            "LIVEKIT_API_KEY": settings.livekit_api_key,
            "LIVEKIT_API_SECRET": settings.livekit_api_secret,
            "LIVEKIT_AGENT_NAME": settings.livekit_agent_name,
            "LIVEKIT_ROOM_PREFIX": settings.livekit_room_prefix,
            "LIVEKIT_TOKEN_TTL_SECONDS": str(settings.livekit_token_ttl_seconds),
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": settings.livekit_sip_outbound_trunk_id,
            "LIVEKIT_SIP_DEFAULT_FROM_NUMBER": (
                settings.livekit_sip_default_from_number
            ),
            "LIVEKIT_SIP_INBOUND_HOST": settings.livekit_sip_inbound_host,
            "LIVEKIT_SIP_MAX_CALL_DURATION_SECONDS": str(
                settings.livekit_sip_max_call_duration_seconds
            ),
        }
    )
    return env


def _read_saved_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Failed to read LiveKit runtime settings: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _settings_from_env() -> LiveKitRuntimeSettings:
    return LiveKitRuntimeSettings(
        voice_runtime=_normal_runtime(VOICE_RUNTIME),
        livekit_url=LIVEKIT_URL,
        livekit_client_url=LIVEKIT_CLIENT_URL,
        livekit_api_key=LIVEKIT_API_KEY,
        livekit_api_secret=LIVEKIT_API_SECRET,
        livekit_agent_name=LIVEKIT_AGENT_NAME,
        livekit_room_prefix=LIVEKIT_ROOM_PREFIX,
        livekit_token_ttl_seconds=LIVEKIT_TOKEN_TTL_SECONDS,
        livekit_sip_outbound_trunk_id=LIVEKIT_SIP_OUTBOUND_TRUNK_ID,
        livekit_sip_default_from_number=LIVEKIT_SIP_DEFAULT_FROM_NUMBER,
        livekit_sip_inbound_host=LIVEKIT_SIP_INBOUND_HOST,
        livekit_sip_max_call_duration_seconds=LIVEKIT_SIP_MAX_CALL_DURATION_SECONDS,
        source="env",
    )


def _settings_from_dict(
    data: dict[str, Any], *, source: str | None = None
) -> LiveKitRuntimeSettings:
    return LiveKitRuntimeSettings(
        voice_runtime=_normal_runtime(data.get("voice_runtime")),
        livekit_url=str(data.get("livekit_url") or "").strip(),
        livekit_client_url=str(data.get("livekit_client_url") or "").strip(),
        livekit_api_key=str(data.get("livekit_api_key") or "").strip(),
        livekit_api_secret=str(data.get("livekit_api_secret") or "").strip(),
        livekit_agent_name=str(
            data.get("livekit_agent_name") or "spx-voice"
        ).strip(),
        livekit_room_prefix=str(
            data.get("livekit_room_prefix") or "spx-voice"
        ).strip(),
        livekit_token_ttl_seconds=_positive_int(
            data.get("livekit_token_ttl_seconds"), 3600
        ),
        livekit_sip_outbound_trunk_id=str(
            data.get("livekit_sip_outbound_trunk_id") or ""
        ).strip(),
        livekit_sip_default_from_number=str(
            data.get("livekit_sip_default_from_number") or ""
        ).strip(),
        livekit_sip_inbound_host=str(
            data.get("livekit_sip_inbound_host") or ""
        ).strip(),
        livekit_sip_max_call_duration_seconds=_positive_int(
            data.get("livekit_sip_max_call_duration_seconds"), 1800
        ),
        source=source or str(data.get("source") or "ui"),
    )


def _normal_runtime(value: Any) -> str:
    return "livekit" if str(value or "").strip().lower() == "livekit" else "pipecat"


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default

from __future__ import annotations

"""Utilities for building default service configurations for a new user.

SPX Voice is LiveKit + Gemini realtime first. The traditional STT/LLM/TTS stack
is still available as a secondary mode, but a fresh OSS/Coolify install should
be usable when a Gemini key is provided in the deployment environment.
"""

import os

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    GoogleEmbeddingsConfiguration,
    GoogleLLMService,
    GoogleRealtimeLLMConfiguration,
    OpenAIEmbeddingsConfiguration,
    OpenAILLMService,
    OpenAISTTConfiguration,
    OpenAITTSService,
    ServiceProviders,
)

DEFAULT_REALTIME_MODEL = os.getenv(
    "DEFAULT_REALTIME_MODEL", "gemini-3.1-flash-live-preview"
)
DEFAULT_REALTIME_VOICE = os.getenv("DEFAULT_REALTIME_VOICE", "Kore")
DEFAULT_REALTIME_LANGUAGE = os.getenv("DEFAULT_REALTIME_LANGUAGE", "en")
DEFAULT_GOOGLE_LLM_MODEL = os.getenv("DEFAULT_GOOGLE_LLM_MODEL", "gemini-2.5-flash")
DEFAULT_GOOGLE_EMBEDDING_MODEL = os.getenv(
    "DEFAULT_GOOGLE_EMBEDDING_MODEL", "gemini-embedding-001"
)

GOOGLE_API_KEY_ENV_NAMES = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GOOGLE_AI_API_KEY",
)


# Mapping of service to (provider enum, configuration class). The UI reads this
# to decide which provider to preselect before the user has saved a config.
#
# The pipeline (non-realtime) STT/TTS defaults are OpenAI because that is the
# only pipeline STT/TTS provider the LiveKit worker can actually run (see
# api/services/livekit/runtime_config.LIVEKIT_SUPPORTED_PROVIDERS). Deepgram /
# ElevenLabs are registered but unsupported by the worker, so defaulting to them
# produced calls that silently failed at session creation.
_DEFAULTS = {
    "llm": (ServiceProviders.GOOGLE, GoogleLLMService),
    "tts": (ServiceProviders.OPENAI, OpenAITTSService),
    "stt": (ServiceProviders.OPENAI, OpenAISTTConfiguration),
    "embeddings": (ServiceProviders.GOOGLE, GoogleEmbeddingsConfiguration),
    "realtime": (ServiceProviders.GOOGLE_REALTIME, GoogleRealtimeLLMConfiguration),
}

# Public mapping of service name -> default provider
DEFAULT_SERVICE_PROVIDERS = {
    field: provider for field, (provider, _) in _DEFAULTS.items()
}

DEFAULT_IS_REALTIME = True


def _first_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def build_env_default_user_configuration() -> UserConfiguration | None:
    """Build a first-run config from deployment env vars, if available.

    Gemini realtime is preferred. If no Gemini key exists, fall back to an
    all-OpenAI pipeline (LLM + STT + TTS) when an OpenAI key is present. OpenAI
    is the only pipeline STT/TTS provider the LiveKit worker can run, so the
    fallback deliberately does not use Deepgram/ElevenLabs (which would build a
    config that fails silently at call time).
    """

    google_key = _first_env_value(GOOGLE_API_KEY_ENV_NAMES)
    if google_key:
        return UserConfiguration(
            is_realtime=True,
            realtime=GoogleRealtimeLLMConfiguration(
                provider=ServiceProviders.GOOGLE_REALTIME,
                api_key=[google_key],
                model=DEFAULT_REALTIME_MODEL,
                voice=DEFAULT_REALTIME_VOICE,
                language=DEFAULT_REALTIME_LANGUAGE,
            ),
            llm=GoogleLLMService(
                provider=ServiceProviders.GOOGLE,
                api_key=[google_key],
                model=DEFAULT_GOOGLE_LLM_MODEL,
            ),
            embeddings=GoogleEmbeddingsConfiguration(
                provider=ServiceProviders.GOOGLE,
                api_key=[google_key],
                model=DEFAULT_GOOGLE_EMBEDDING_MODEL,
            ),
        )

    openai_key = _first_env_value(("OPENAI_API_KEY",))
    if openai_key:
        return UserConfiguration(
            is_realtime=False,
            llm=OpenAILLMService(
                provider=ServiceProviders.OPENAI,
                api_key=[openai_key],
                model="gpt-4.1",
            ),
            stt=OpenAISTTConfiguration(
                provider=ServiceProviders.OPENAI,
                api_key=[openai_key],
                model="gpt-4o-transcribe",
            ),
            tts=OpenAITTSService(
                provider=ServiceProviders.OPENAI,
                api_key=[openai_key],
                model="gpt-4o-mini-tts",
                voice="alloy",
            ),
            embeddings=OpenAIEmbeddingsConfiguration(
                provider=ServiceProviders.OPENAI,
                api_key=[openai_key],
                model="text-embedding-3-small",
            ),
        )

    return None


__all__ = [
    "DEFAULT_IS_REALTIME",
    "DEFAULT_SERVICE_PROVIDERS",
    "build_env_default_user_configuration",
]

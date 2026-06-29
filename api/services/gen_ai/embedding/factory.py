"""Factory helpers for embedding services."""

from typing import Any

from api.db.db_client import DBClient
from api.services.configuration.registry import ServiceProviders

from .base import BaseEmbeddingService
from .google_service import GoogleEmbeddingService
from .openai_service import OpenAIEmbeddingService


def create_embedding_service(
    *,
    db_client: DBClient,
    provider: str | None,
    api_key: str | None,
    model: str | None,
    base_url: str | None = None,
) -> BaseEmbeddingService:
    """Create an embedding service for the configured provider."""
    if _provider_value(provider) == ServiceProviders.GOOGLE.value:
        return GoogleEmbeddingService(
            db_client=db_client,
            api_key=api_key,
            model_id=model or "gemini-embedding-001",
        )

    return OpenAIEmbeddingService(
        db_client=db_client,
        api_key=api_key,
        model_id=model or "text-embedding-3-small",
        base_url=base_url,
    )


def resolve_embedding_settings(user_config: Any) -> dict[str, Any]:
    """Resolve explicit embeddings or reuse Google realtime credentials."""
    embeddings = getattr(user_config, "embeddings", None)
    if embeddings and _has_api_key(getattr(embeddings, "api_key", None)):
        return {
            "provider": _provider_value(getattr(embeddings, "provider", None)),
            "api_key": getattr(embeddings, "api_key", None),
            "model": getattr(embeddings, "model", None),
            "base_url": getattr(embeddings, "base_url", None),
        }

    realtime = getattr(user_config, "realtime", None)
    realtime_provider = _provider_value(getattr(realtime, "provider", None))
    if (
        realtime
        and realtime_provider
        in {
            ServiceProviders.GOOGLE_REALTIME.value,
            ServiceProviders.GOOGLE_VERTEX_REALTIME.value,
        }
        and _has_api_key(getattr(realtime, "api_key", None))
    ):
        return {
            "provider": ServiceProviders.GOOGLE.value,
            "api_key": getattr(realtime, "api_key", None),
            "model": "gemini-embedding-001",
            "base_url": None,
        }

    return {
        "provider": ServiceProviders.OPENAI.value,
        "api_key": None,
        "model": "text-embedding-3-small",
        "base_url": None,
    }


def _provider_value(provider: Any) -> str | None:
    if provider is None:
        return None
    return provider.value if isinstance(provider, ServiceProviders) else str(provider)


def _has_api_key(api_key: Any) -> bool:
    if isinstance(api_key, list):
        return any(bool(str(value).strip()) for value in api_key)
    return bool(str(api_key).strip()) if api_key is not None else False

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.user import router
from api.schemas.user_configuration import UserConfiguration
from api.services.auth.depends import get_user
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.defaults import build_env_default_user_configuration
from api.services.configuration.masking import check_for_masked_keys
from api.services.configuration.registry import ServiceProviders

REALTIME_ONLY_PAYLOAD = {
    "is_realtime": True,
    "llm": {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "api_key": "test-google-key",
    },
    "realtime": {
        "provider": "google_realtime",
        "model": "gemini-3.1-flash-live-preview",
        "voice": "Puck",
        "language": "multi",
        "api_key": "test-google-key",
    },
    "stt": {
        "provider": "deepgram",
        "model": "nova-3-general",
        "language": "multi",
    },
    "tts": {
        "provider": "elevenlabs",
        "model": "eleven_flash_v2_5",
        "voice": "21m00Tcm4TlvDq8ikWAM",
        "base_url": "https://api.elevenlabs.io",
    },
}


def test_realtime_configuration_ignores_inactive_stt_tts_without_api_keys():
    config = UserConfiguration.model_validate(REALTIME_ONLY_PAYLOAD)

    assert config.is_realtime is True
    assert config.stt is None
    assert config.tts is None
    assert config.realtime is not None


def test_env_default_configuration_prefers_gemini_realtime(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-test-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENERATIVE_AI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)

    config = build_env_default_user_configuration()

    assert config is not None
    assert config.is_realtime is True
    assert config.realtime is not None
    assert config.realtime.provider == "google_realtime"
    assert config.realtime.model == "gemini-3.1-flash-live-preview"
    assert config.realtime.voice == "Kore"
    assert config.realtime.language == "en"
    assert config.realtime.get_all_api_keys() == ["google-test-key"]
    assert config.llm is not None
    assert config.llm.provider == "google"
    assert config.llm.get_all_api_keys() == ["google-test-key"]
    assert config.embeddings is not None
    assert config.embeddings.provider == "google"


@pytest.mark.asyncio
async def test_realtime_validator_requires_realtime_not_stt_tts():
    config = UserConfiguration.model_validate(REALTIME_ONLY_PAYLOAD)

    result = await UserConfigurationValidator().validate(config)

    assert result == {"status": [{"model": "all", "message": "ok"}]}


@pytest.mark.asyncio
async def test_realtime_validator_rejects_spx_key_for_google_realtime():
    payload = {
        **REALTIME_ONLY_PAYLOAD,
        "realtime": {
            **REALTIME_ONLY_PAYLOAD["realtime"],
            "api_key": "dgr_wrong_slot",
        },
    }
    config = UserConfiguration.model_validate(payload)

    with pytest.raises(ValueError) as exc:
        await UserConfigurationValidator().validate(config)

    assert exc.value.args[0] == [
        {
            "model": "realtime",
            "message": (
                "Google/Gemini realtime needs a Google AI Studio API key. "
                "The value entered looks like an SPX Voice/Dograh key (dgr...)."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_realtime_validator_skips_stale_llm_by_default():
    payload = {
        **REALTIME_ONLY_PAYLOAD,
        "llm": {
            "provider": "openai",
            "model": "gpt-4.1",
            "api_key": "invalid-stale-openai-key",
        },
    }
    config = UserConfiguration.model_validate(payload)
    validator = UserConfigurationValidator()
    validator._validator_map[ServiceProviders.OPENAI.value] = lambda *_: False

    result = await validator.validate(config)

    assert result == {"status": [{"model": "all", "message": "ok"}]}


def _make_test_app():
    app = FastAPI()
    app.include_router(router)

    mock_user = MagicMock()
    mock_user.id = 1
    mock_user.is_superuser = False
    mock_user.selected_organization_id = None
    mock_user.provider_id = "local-test-user"

    app.dependency_overrides[get_user] = lambda: mock_user
    return app


def test_realtime_embedding_update_does_not_validate_stale_llm():
    app = _make_test_app()
    client = TestClient(app)
    existing = UserConfiguration.model_validate(
        {
            **REALTIME_ONLY_PAYLOAD,
            "llm": {
                "provider": "openai",
                "model": "gpt-4.1",
                "api_key": "invalid-stale-openai-key",
            },
        }
    )

    def fail_openai_validation(*_):
        raise AssertionError("stale LLM should not be validated for embedding updates")

    with (
        patch("api.routes.user.db_client") as mock_db,
        patch.object(
            UserConfigurationValidator,
            "_check_openai_api_key",
            side_effect=fail_openai_validation,
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(return_value=existing)
        mock_db.update_user_configuration = AsyncMock(side_effect=lambda _uid, cfg: cfg)

        response = client.put(
            "/user/configurations/user",
            json={
                "embeddings": {
                    "provider": "openrouter",
                    "model": "openai/text-embedding-3-small",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": ["or-test-key"],
                }
            },
        )

    assert response.status_code == 200


def test_realtime_mask_check_ignores_inactive_stt_tts_placeholders():
    payload = {
        **REALTIME_ONLY_PAYLOAD,
        "stt": {
            "provider": "deepgram",
            "model": "nova-3-general",
            "language": "multi",
            "api_key": "********1234",
        },
        "tts": {
            "provider": "elevenlabs",
            "model": "eleven_flash_v2_5",
            "voice": "21m00Tcm4TlvDq8ikWAM",
            "base_url": "https://api.elevenlabs.io",
            "api_key": "********5678",
        },
    }
    config = UserConfiguration.model_validate(payload)

    check_for_masked_keys(config)

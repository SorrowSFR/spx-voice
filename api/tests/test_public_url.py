from api.utils.public_url import (
    normalize_public_url,
    resolve_coolify_ui_url,
    resolve_public_url,
)


def test_app_url_wins_over_other_values():
    env = {
        "APP_URL": "https://voice.example.com/",
        "BACKEND_API_ENDPOINT": "http://localhost:3010",
        "SERVICE_URL_UI_3010": "https://generated.example.com:3010",
    }

    assert (
        resolve_public_url("BACKEND_API_ENDPOINT", env=env)
        == "https://voice.example.com"
    )


def test_explicit_non_local_url_wins_over_coolify_generated_url():
    env = {
        "BACKEND_API_ENDPOINT": "https://custom.example.com",
        "SERVICE_URL_UI_3010": "https://generated.example.com:3010",
    }

    assert (
        resolve_public_url("BACKEND_API_ENDPOINT", env=env)
        == "https://custom.example.com"
    )


def test_coolify_generated_url_replaces_localhost_compose_fallback():
    env = {
        "BACKEND_API_ENDPOINT": "http://localhost:3010",
        "SERVICE_URL_UI_3010": "https://web-ui.spxai.cloud:3010",
    }

    assert (
        resolve_public_url("BACKEND_API_ENDPOINT", env=env)
        == "https://web-ui.spxai.cloud"
    )


def test_coolify_generated_fqdn_gets_scheme_and_strips_internal_port():
    env = {"SERVICE_FQDN_UI_3010": "web-ui.spxai.cloud:3010"}

    assert resolve_coolify_ui_url(env) == "https://web-ui.spxai.cloud"


def test_localhost_is_kept_when_no_coolify_url_exists():
    env = {"BACKEND_API_ENDPOINT": "http://localhost:3010"}

    assert (
        resolve_public_url("BACKEND_API_ENDPOINT", env=env) == "http://localhost:3010"
    )


def test_default_used_when_no_values_exist():
    assert (
        resolve_public_url(
            "BACKEND_API_ENDPOINT", env={}, default="http://localhost:8000"
        )
        == "http://localhost:8000"
    )


def test_normalize_public_url_keeps_explicit_non_default_port():
    assert (
        normalize_public_url("https://voice.example.com:8443")
        == "https://voice.example.com:8443"
    )

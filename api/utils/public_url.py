from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

DEFAULT_UI_PORT = "3010"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def resolve_public_url(
    explicit_name: str,
    *,
    env: Mapping[str, str | None] | None = None,
    default: str | None = None,
) -> str | None:
    """Resolve the browser-facing app URL for Coolify and local deployments.

    Priority:
    1. APP_URL, when set.
    2. The named explicit URL, when non-local.
    3. Coolify-generated UI URL/FQDN.
    4. The named explicit URL, even if local.
    5. The supplied default.
    """

    env = env or os.environ

    app_url = _get_nonempty(env, "APP_URL")
    if app_url:
        return normalize_public_url(app_url)

    explicit_url = _get_nonempty(env, explicit_name)
    if explicit_url and not is_local_url(explicit_url):
        return normalize_public_url(explicit_url)

    coolify_url = resolve_coolify_ui_url(env)
    if coolify_url:
        return coolify_url

    if explicit_url:
        return normalize_public_url(explicit_url)

    if default:
        return normalize_public_url(default)

    return None


def resolve_coolify_ui_url(env: Mapping[str, str | None] | None = None) -> str | None:
    env = env or os.environ
    candidates = (
        "COOLIFY_UI_URL",
        "SERVICE_URL_UI_3010",
        "SERVICE_URL_UI",
        "COOLIFY_UI_FQDN",
        "SERVICE_FQDN_UI_3010",
        "SERVICE_FQDN_UI",
    )
    for name in candidates:
        value = _get_nonempty(env, name)
        if value:
            return normalize_public_url(
                value,
                strip_internal_port=name.endswith(f"_{DEFAULT_UI_PORT}")
                or name in {"COOLIFY_UI_URL", "COOLIFY_UI_FQDN"},
            )
    return None


def normalize_public_url(value: str, *, strip_internal_port: bool = False) -> str:
    url = value.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    parsed = urlsplit(url)
    netloc = parsed.netloc
    if (
        strip_internal_port
        and parsed.hostname
        and _port(parsed) == int(DEFAULT_UI_PORT)
    ):
        if parsed.hostname not in LOCAL_HOSTS:
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = host

    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def is_local_url(value: str) -> bool:
    try:
        parsed = urlsplit(normalize_public_url(value))
    except ValueError:
        return False
    return parsed.hostname in LOCAL_HOSTS


def _get_nonempty(env: Mapping[str, str | None], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _port(parsed) -> int | None:
    try:
        return parsed.port
    except ValueError:
        return None

"""Shared web-boundary policy for CORS, sessions, and device cookies."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response

from .pairing import DEVICE_COOKIE_NAME

SESSION_SECRET_PLACEHOLDERS = frozenset(
    {
        "",
        "chili-session-change-me",
        "change-me",
        "changeme",
        "change-me-to-a-random-string",
        "secret",
        "default",
    }
)
MIN_PROTECTED_SESSION_SECRET_LENGTH = 32
LOCAL_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _settings(settings: Any | None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as app_settings

    return app_settings


def parse_cors_origins(raw: Any) -> list[str]:
    if raw is None:
        return ["*"]
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return ["*"]
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                values = parsed if isinstance(parsed, list) else [text]
            except json.JSONDecodeError:
                values = [text]
        else:
            values = text.split(",")
    origins = [str(v).strip() for v in values if str(v).strip()]
    return origins or ["*"]


def cors_allow_origins(settings: Any | None = None) -> list[str]:
    return parse_cors_origins(getattr(_settings(settings), "web_cors_allow_origins", "*"))


def _is_local_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in LOCAL_ORIGIN_HOSTS


def is_protected_web_deployment(settings: Any | None = None) -> bool:
    cfg = _settings(settings)
    mode = str(getattr(cfg, "web_boundary_mode", "auto") or "auto").strip().lower()
    if mode == "production":
        return True
    if mode == "development":
        return False
    env = str(getattr(cfg, "chili_environment", "development") or "").strip().lower()
    if env in {"prod", "production", "staging"}:
        return True
    public_origin = str(getattr(cfg, "web_public_origin", "") or "").strip()
    parsed = urlparse(public_origin) if public_origin else None
    return bool(
        parsed
        and parsed.scheme.lower() == "https"
        and not _is_local_origin(public_origin)
    )


def validate_web_boundary(settings: Any | None = None) -> None:
    cfg = _settings(settings)
    if not is_protected_web_deployment(cfg):
        return
    origins = cors_allow_origins(cfg)
    if "*" in origins:
        raise RuntimeError(
            "Protected web deployments require explicit CHILI_WEB_CORS_ALLOW_ORIGINS "
            "when credentialed CORS is enabled."
        )
    secret = str(getattr(cfg, "session_secret", "") or "")
    if secret.strip().lower() in SESSION_SECRET_PLACEHOLDERS:
        raise RuntimeError(
            "Protected web deployments require a non-placeholder SESSION_SECRET."
        )
    if len(secret) < MIN_PROTECTED_SESSION_SECRET_LENGTH:
        raise RuntimeError(
            "Protected web deployments require SESSION_SECRET length >= "
            f"{MIN_PROTECTED_SESSION_SECRET_LENGTH} characters."
        )
    cookie_mode = str(getattr(cfg, "web_cookie_secure", "auto") or "auto").strip().lower()
    if cookie_mode == "never":
        raise RuntimeError(
            "Protected web deployments cannot set CHILI_WEB_COOKIE_SECURE=never."
        )


def request_is_https(request: Request | None) -> bool:
    if request is None:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded_proto == "https":
        return True
    return request.url.scheme == "https"


def should_secure_device_cookie(
    request: Request | None = None,
    settings: Any | None = None,
) -> bool:
    cfg = _settings(settings)
    mode = str(getattr(cfg, "web_cookie_secure", "auto") or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return request_is_https(request) or is_protected_web_deployment(cfg)


def set_device_cookie(
    response: Response,
    token: str,
    *,
    request: Request | None = None,
    settings: Any | None = None,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=should_secure_device_cookie(request, settings),
    )


def delete_device_cookie(
    response: Response,
    *,
    request: Request | None = None,
    settings: Any | None = None,
) -> None:
    response.delete_cookie(
        DEVICE_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=should_secure_device_cookie(request, settings),
    )

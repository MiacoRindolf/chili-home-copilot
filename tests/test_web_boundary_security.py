from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.pairing import DEVICE_COOKIE_NAME
from app.web_security import (
    cors_allow_origins,
    set_device_cookie,
    should_secure_device_cookie,
    validate_web_boundary,
)


def _settings(**overrides):
    values = {
        "chili_environment": "development",
        "web_boundary_mode": "auto",
        "web_public_origin": "",
        "web_cors_allow_origins": "*",
        "web_cookie_secure": "auto",
        "session_secret": "chili-session-change-me",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(scheme="http", headers=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw_headers,
            "scheme": scheme,
            "server": ("localhost", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_local_development_keeps_wildcard_cors_usable():
    cfg = _settings()

    validate_web_boundary(cfg)

    assert cors_allow_origins(cfg) == ["*"]


def test_protected_deployment_rejects_wildcard_credentialed_cors():
    cfg = _settings(
        web_boundary_mode="production",
        web_cors_allow_origins="*",
        session_secret="x" * 40,
    )

    with pytest.raises(RuntimeError, match="explicit CHILI_WEB_CORS_ALLOW_ORIGINS"):
        validate_web_boundary(cfg)


def test_protected_deployment_rejects_placeholder_or_short_session_secret():
    placeholder = _settings(
        web_boundary_mode="production",
        web_cors_allow_origins="https://chili.example",
        session_secret="chili-session-change-me",
    )
    short = _settings(
        web_boundary_mode="production",
        web_cors_allow_origins="https://chili.example",
        session_secret="short-but-not-placeholder",
    )

    with pytest.raises(RuntimeError, match="non-placeholder SESSION_SECRET"):
        validate_web_boundary(placeholder)
    with pytest.raises(RuntimeError, match="SESSION_SECRET length"):
        validate_web_boundary(short)


def test_protected_deployment_allows_explicit_origin_and_strong_secret():
    cfg = _settings(
        web_boundary_mode="production",
        web_cors_allow_origins="https://chili.example,https://ops.example",
        session_secret="s" * 40,
    )

    validate_web_boundary(cfg)

    assert cors_allow_origins(cfg) == ["https://chili.example", "https://ops.example"]


def test_device_cookie_secure_policy_tracks_request_and_protected_mode():
    local_cfg = _settings()
    protected_cfg = _settings(
        web_boundary_mode="production",
        web_cors_allow_origins="https://chili.example",
        session_secret="s" * 40,
    )

    assert should_secure_device_cookie(_request("http"), local_cfg) is False
    assert should_secure_device_cookie(_request("https"), local_cfg) is True
    assert should_secure_device_cookie(_request("http"), protected_cfg) is True
    assert should_secure_device_cookie(
        _request("http", headers={"x-forwarded-proto": "https"}),
        local_cfg,
    ) is True

    response = Response()
    set_device_cookie(response, "token", request=_request("http"), settings=local_cfg)
    assert DEVICE_COOKIE_NAME in response.headers["set-cookie"]
    assert "secure" not in response.headers["set-cookie"].lower()

    secure_response = Response()
    set_device_cookie(
        secure_response,
        "token",
        request=_request("http"),
        settings=protected_cfg,
    )
    assert "secure" in secure_response.headers["set-cookie"].lower()

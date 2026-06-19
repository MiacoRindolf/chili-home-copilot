"""Robinhood Agentic MCP — OAuth 2.1 token-bundle + secure storage + refresh contracts.

This module is the headless-operation foundation for the sanctioned Robinhood
Agentic Trading MCP rail (``https://agent.robinhood.com/mcp/trading``). The MCP
bearer is short-lived; persistent unattended operation in the scheduler container
needs OAuth *refresh*. This file owns the on-disk **token bundle** (access +
refresh + rotation metadata), its fail-closed loader, and an atomic, restrictive
writer. The refreshing token-source + the interactive consent helper live in
``rh_mcp_client.py`` and ``scripts/rh_agentic_oauth.py`` respectively.

SECURITY: token material is NEVER logged, printed, or placed in exception text.
``TokenBundle.__repr__``/``__str__`` are redacted so an f-string or traceback
cannot leak the secret. ``write_bundle_atomic`` writes 0600 (POSIX) / owner-only
ACL (Windows) and warns (without contents) if the path is git-tracked.

Nothing imports this module until the rail is wired, so adding it is inert.
"""

from __future__ import annotations

# stdlib
import base64
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- OAuth endpoints + tunables (single source of truth, env-overridable) -------
# Verified 2026-06-19 from https://agent.robinhood.com/.well-known/oauth-authorization-server
DEFAULT_AUTHORIZATION_ENDPOINT = "https://robinhood.com/oauth"
DEFAULT_TOKEN_ENDPOINT = "https://api.robinhood.com/oauth2/token/"
DEFAULT_REGISTRATION_ENDPOINT = "https://agent.robinhood.com/oauth/trading/register"
DEFAULT_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
DEFAULT_RESOURCE = DEFAULT_ENDPOINT
DEFAULT_SCOPE = "internal"
PKCE_METHOD = "S256"
BUNDLE_VERSION = 1

# Only these hosts may ever receive the bearer or the refresh token. Any redirect
# or misconfig that would send credentials elsewhere is rejected by the transport.
ALLOWED_OAUTH_HOSTS = {"agent.robinhood.com", "api.robinhood.com", "robinhood.com"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


# Refresh proactively this many seconds before expiry (token TTL unknown -> generous).
REFRESH_SKEW_SECONDS = _env_float("CHILI_RH_AGENTIC_REFRESH_SKEW_SECONDS", 300.0)
REFRESH_LOCK_TIMEOUT = _env_float("CHILI_RH_AGENTIC_REFRESH_LOCK_TIMEOUT_SECONDS", 30.0)
TOKEN_HTTP_TIMEOUT = _env_float("CHILI_RH_AGENTIC_OAUTH_HTTP_TIMEOUT_SECONDS", 20.0)
# Guard against a server pinning a token "valid forever": clamp absurd expires_in.
MAX_PLAUSIBLE_EXPIRES_IN = 30 * 86400


class NeedsReauth(Exception):
    """Auth cannot be recovered without a fresh interactive operator consent.

    Raised when there is no usable refresh path (no bundle / no refresh token,
    the refresh was rejected, a rotation was lost, or the grant was revoked).
    The adapter converts this into a fail-closed ``needs_reauth`` result so the
    rail reports DISABLED and never places an order with stale/ambiguous auth.

    The message/``__repr__`` carry ONLY the reason — never token material.
    """

    VALID_REASONS = {
        "no_bundle",
        "no_refresh_token",
        "refresh_rejected",
        "grant_revoked",
        "rotation_lost",
        "expired_no_refresh",
    }

    def __init__(self, reason: str = "refresh_rejected"):
        self.reason = str(reason or "refresh_rejected")
        super().__init__(f"NeedsReauth(reason={self.reason})")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"NeedsReauth(reason={self.reason})"


# --- the token bundle ----------------------------------------------------------
@dataclass
class TokenBundle:
    """The persisted OAuth state for the agentic rail. Self-contained: it carries
    the endpoints + client_id needed to refresh without any extra config."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: float = 0.0  # absolute epoch seconds
    scope: str = DEFAULT_SCOPE
    client_id: str = ""
    token_type: str = "Bearer"
    endpoint: str = DEFAULT_ENDPOINT
    resource: str = DEFAULT_RESOURCE
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT
    obtained_at: float = 0.0
    pending_refresh: bool = False
    version: int = BUNDLE_VERSION
    # extra fields tolerated on load (forward-compat) but never echoed
    _extra: dict = field(default_factory=dict, repr=False, compare=False)

    # --- predicates ---
    def is_expired(self, skew: float = REFRESH_SKEW_SECONDS, now: float = 0.0) -> bool:
        """True if the access token is at/past (expires_at - skew)."""
        try:
            exp = float(self.expires_at or 0.0)
        except Exception:
            return True
        if exp <= 0:
            return True
        return float(now) >= (exp - max(0.0, float(skew)))

    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)

    def is_hard_dead(self, now: float = 0.0) -> bool:
        """No way back without re-consent: no refresh token AND access past expiry.
        (A bundle with a refresh token is NOT hard-dead even if the access expired.)"""
        if self.has_refresh_token():
            return False
        return self.is_expired(skew=0.0, now=now)

    # --- redaction (the only thing safe to log) ---
    def redacted(self) -> dict:
        cid = str(self.client_id or "")
        return {
            "expires_at": self.expires_at,
            "scope": self.scope,
            "client_id_tail": cid[-6:] if cid else "",
            "has_refresh": self.has_refresh_token(),
            "pending_refresh": bool(self.pending_refresh),
            "version": self.version,
        }

    def __repr__(self) -> str:
        return f"TokenBundle({self.redacted()})"

    __str__ = __repr__

    # --- serialization ---
    def to_dict(self) -> dict:
        return {
            "version": int(self.version or BUNDLE_VERSION),
            "token_type": self.token_type or "Bearer",
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": float(self.expires_at or 0.0),
            "scope": self.scope or DEFAULT_SCOPE,
            "client_id": self.client_id or "",
            "endpoint": self.endpoint or DEFAULT_ENDPOINT,
            "resource": self.resource or DEFAULT_RESOURCE,
            "token_endpoint": self.token_endpoint or DEFAULT_TOKEN_ENDPOINT,
            "obtained_at": float(self.obtained_at or 0.0),
            "pending_refresh": bool(self.pending_refresh),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Optional["TokenBundle"]:
        if not isinstance(d, dict):
            return None
        access = d.get("access_token")
        if not access or not isinstance(access, str):
            # Fail-closed: a bundle without an access token is unusable. NEVER
            # fall back to treating refresh_token (or any field) as the bearer.
            return None
        known = {
            "access_token", "refresh_token", "expires_at", "scope", "client_id",
            "token_type", "endpoint", "resource", "token_endpoint", "obtained_at",
            "pending_refresh", "version",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        try:
            return cls(
                access_token=access,
                refresh_token=d.get("refresh_token") or None,
                expires_at=float(d.get("expires_at") or 0.0),
                scope=str(d.get("scope") or DEFAULT_SCOPE),
                client_id=str(d.get("client_id") or ""),
                token_type=str(d.get("token_type") or "Bearer"),
                endpoint=str(d.get("endpoint") or DEFAULT_ENDPOINT),
                resource=str(d.get("resource") or DEFAULT_RESOURCE),
                token_endpoint=str(d.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
                obtained_at=float(d.get("obtained_at") or 0.0),
                pending_refresh=bool(d.get("pending_refresh") or False),
                version=int(d.get("version") or BUNDLE_VERSION),
                _extra=extra,
            )
        except Exception:
            return None


# --- PKCE (used by the consent helper + dynamic registration) ------------------
def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256. Verifier stays in
    memory only — never written or logged."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    return base64.urlsafe_b64encode(os.urandom(24)).rstrip(b"=").decode("ascii")


# --- path resolution -----------------------------------------------------------
def default_bundle_path() -> str:
    """Out-of-repo default so a token file can never be committed. Operator may
    override via settings.chili_robinhood_agentic_mcp_token_file /
    CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE (e.g. a volume mounted into the
    scheduler container)."""
    explicit = os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE")
    if explicit:
        return explicit
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "chili", "rh_agentic", "token.json")
    # Linux / container: a dedicated dir under HOME (mount this into the container).
    base = os.environ.get("CHILI_SECRETS_DIR") or os.path.join(os.path.expanduser("~"), ".chili")
    return os.path.join(base, "rh_agentic", "token.json")


# --- secure load / write -------------------------------------------------------
def load_bundle(path: str) -> Optional[TokenBundle]:
    """Load a token bundle. Fail-CLOSED: any parse error, missing file, or a
    JSON object without an access_token returns None (never a partial/raw token)."""
    try:
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except Exception as exc:  # noqa: BLE001 - never log the contents
        logger.warning("[rh_oauth] token bundle read failed path=%s err=%s", path, type(exc).__name__)
        return None
    stripped = (raw or "").strip()
    if not stripped.startswith("{"):
        # Not a bundle (legacy raw-string tokens are handled by rh_mcp_client,
        # not here). Do not guess.
        return None
    try:
        data = json.loads(stripped)
    except Exception:
        logger.warning("[rh_oauth] token bundle parse failed path=%s (fail-closed)", path)
        return None
    bundle = TokenBundle.from_dict(data)
    if bundle is None:
        logger.warning("[rh_oauth] token bundle missing access_token path=%s (fail-closed)", path)
    return bundle


def _restrict_windows_acl(path: str) -> None:
    """Owner-only ACL on Windows (POSIX uses chmod 0600). Best-effort; loudly
    warns (no contents) if the file remains broadly readable."""
    try:
        import getpass

        user = getpass.getuser()
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        verify = subprocess.run(
            ["icacls", path], check=False, capture_output=True, text=True, timeout=15,
        )
        out = (verify.stdout or "")
        if "Everyone" in out or "\\Users" in out or "BUILTIN\\Users" in out:
            logger.warning(
                "[rh_oauth] token file may be broadly readable after ACL set path=%s "
                "(remove Users/Everyone read manually)", path,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rh_oauth] could not restrict ACL path=%s err=%s", path, type(exc).__name__)


def _warn_if_path_tracked(path: str) -> None:
    """Warn (never the contents) if the bundle path is git-tracked / not ignored."""
    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        # check-ignore returns 0 if the path IS ignored (good)
        res = subprocess.run(
            ["git", "-C", d, "check-ignore", "-q", path],
            check=False, capture_output=True, timeout=10,
        )
        if res.returncode != 0:
            logger.warning(
                "[rh_oauth] token bundle path is NOT git-ignored path=%s — ensure it "
                "is outside the repo or matched by .gitignore", path,
            )
    except Exception:
        # git absent / not a repo — fine.
        pass


def write_bundle_atomic(path: str, bundle: TokenBundle) -> None:
    """Atomically write the bundle with owner-only perms. Writes to a 0600 temp
    in the same dir, fsyncs, restricts perms, then os.replace (atomic). Never
    logs the contents."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rh_tok_", suffix=".tmp")
    try:
        if os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except Exception:
                pass
        os.write(fd, json.dumps(bundle.to_dict()).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if os.name == "nt":
            _restrict_windows_acl(tmp)
        os.replace(tmp, path)  # atomic on same filesystem
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        _warn_if_path_tracked(path)
    except Exception:
        # best-effort cleanup of the temp; re-raise so the caller knows the write failed
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise

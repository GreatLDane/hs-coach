"""Startup configuration loading and hosted-mode validation."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from hs_coach.auth import get_session_secret

DeploymentMode = Literal["local", "hosted"]

_TRUTHY = frozenset({"1", "true", "yes"})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        return default


def is_loopback_host(host: str) -> bool:
    """Return True if host is localhost or a loopback IP address (127.0.0.0/8, ::1)."""
    norm = host.strip().lower()
    if norm.startswith("[") and norm.endswith("]"):
        norm = norm[1:-1]
    if not norm:
        return False
    if norm in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(norm).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class AppConfig:
    mode: DeploymentMode = "local"
    host: str = "127.0.0.1"
    session_secret: str = ""
    cookie_secure: bool = False
    trusted_proxies: tuple[str, ...] = ()
    public_url: str = "http://127.0.0.1:8000"
    allow_raw_log_upload: bool = True
    allow_hosted_guests: bool = False
    allow_hosted_client_coaching: bool = False
    max_upload_bytes: int = 50 * 1024 * 1024
    max_replay_bytes: int = 2 * 1024 * 1024
    max_json_bytes: int = 64 * 1024
    max_webhook_bytes: int = 256 * 1024
    max_workers: int = 4
    max_queued_jobs: int = 32
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_pro_price_id: str = ""


def validate_app_config(cfg: AppConfig) -> None:
    """Validate runtime safety invariants before constructing the ASGI app."""
    if cfg.mode not in ("local", "hosted"):
        raise ValueError(f"Unsupported HS_COACH_MODE: {cfg.mode!r} (must be 'local' or 'hosted').")

    if cfg.mode == "local":
        if not is_loopback_host(cfg.host):
            raise ValueError(
                "Local mode may only bind to loopback addresses. "
                "Set HS_COACH_MODE=hosted for public binding."
            )
    else:
        if not cfg.session_secret or len(cfg.session_secret) < 32:
            raise ValueError(
                "HS_COACH_SESSION_SECRET must be set to at least 32 characters in hosted mode."
            )
        parsed = urlparse(cfg.public_url)
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or not hostname or is_loopback_host(hostname):
            raise ValueError(
                "HS_COACH_PUBLIC_URL must use https:// with a non-loopback host in hosted mode."
            )
        if cfg.allow_raw_log_upload:
            raise ValueError(
                "Raw Power.log upload must be disabled (allow_raw_log_upload=False) in hosted mode."
            )
        stripe_fields = (
            cfg.stripe_secret_key,
            cfg.stripe_webhook_secret,
            cfg.stripe_pro_price_id,
        )
        if any(stripe_fields) and not all(stripe_fields):
            raise ValueError(
                "Partial Stripe configuration in hosted mode: set STRIPE_SECRET_KEY, "
                "STRIPE_WEBHOOK_SECRET, and STRIPE_PRO_PRICE_ID together."
            )


def load_app_config(
    *,
    mode: str | None = None,
    host: str = "127.0.0.1",
    load_dotenv: bool = False,
) -> AppConfig:
    """Load and validate AppConfig from environment variables before building the web app."""
    if load_dotenv:
        from hs_coach.coach import _load_dotenv_if_present

        _load_dotenv_if_present()

    raw_mode = (mode or os.environ.get("HS_COACH_MODE", "local")).strip().lower()
    if raw_mode not in ("local", "hosted"):
        raise ValueError(f"Unsupported HS_COACH_MODE: {raw_mode!r} (must be 'local' or 'hosted').")
    resolved_mode: DeploymentMode = raw_mode  # type: ignore[assignment]

    env_secret = os.environ.get("HS_COACH_SESSION_SECRET", "").strip()
    session_secret = env_secret if resolved_mode == "hosted" else (env_secret or get_session_secret())

    raw_proxies = os.environ.get("HS_COACH_TRUSTED_PROXIES", "")
    trusted_proxies = tuple(p.strip() for p in raw_proxies.split(",") if p.strip())

    raw_raw_upload = os.environ.get("HS_COACH_ALLOW_RAW_UPLOAD", "").strip()
    if resolved_mode == "hosted":
        allow_raw_log_upload = raw_raw_upload.lower() in _TRUTHY if raw_raw_upload else False
    else:
        allow_raw_log_upload = True

    default_public = "https://hs-coach.invalid" if resolved_mode == "hosted" else "http://127.0.0.1:8000"
    public_url = os.environ.get("HS_COACH_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url:
        public_url = default_public if resolved_mode == "local" else ""

    cfg = AppConfig(
        mode=resolved_mode,
        host=host.strip() or "127.0.0.1",
        session_secret=session_secret,
        cookie_secure=_env_flag("HS_COACH_COOKIE_SECURE"),
        trusted_proxies=trusted_proxies,
        public_url=public_url,
        allow_raw_log_upload=allow_raw_log_upload,
        allow_hosted_guests=_env_flag("HS_COACH_ALLOW_HOSTED_GUESTS"),
        allow_hosted_client_coaching=_env_flag("HS_COACH_ALLOW_HOSTED_CLIENT_COACHING"),
        max_upload_bytes=_env_int("HS_COACH_MAX_UPLOAD_BYTES", 50 * 1024 * 1024),
        max_replay_bytes=_env_int("HS_COACH_MAX_REPLAY_BYTES", 2 * 1024 * 1024),
        max_json_bytes=_env_int("HS_COACH_MAX_JSON_BYTES", 64 * 1024),
        max_webhook_bytes=_env_int("HS_COACH_MAX_WEBHOOK_BYTES", 256 * 1024),
        max_workers=_env_int("HS_COACH_MAX_WORKERS", 4),
        max_queued_jobs=_env_int("HS_COACH_MAX_QUEUED_JOBS", 32),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
        stripe_pro_price_id=os.environ.get("STRIPE_PRO_PRICE_ID", "").strip(),
    )
    validate_app_config(cfg)
    return cfg

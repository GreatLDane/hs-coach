"""Authentication, signed session cookies, upload tokens, and Freemium plan quotas."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

SESSION_COOKIE_NAME = "hs_coach_session"
GUEST_COOKIE_NAME = "hs_coach_guest"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
PASSWORD_RESET_TTL_SECONDS = 900  # 15 minutes
_PBKDF2_ITERS = 200_000

FAST_MODEL = "google/gemini-3.7-flash"
# Models that count as cheap "fast" reviews; every other model is a metered "deep review".
FAST_MODELS: frozenset[str] = frozenset({FAST_MODEL})


def is_deep_model(model: str | None) -> bool:
    """True when coaching with this model consumes a monthly deep-review slot."""
    return bool(model) and model not in FAST_MODELS


DEFAULT_PRO_MODELS: frozenset[str] = frozenset(
    {
        "google/gemini-3.7-flash",
        "google/gemini-3.1-pro-preview",
        "anthropic/claude-sonnet-4.5",
        "openai/gpt-4.1",
        "gemini-2.5-pro",
    }
)


@dataclass(frozen=True)
class PlanConfig:
    name: str
    monthly_coach_limit: int
    allowed_models: frozenset[str]
    default_model: str
    # Monthly cap on coaching runs with non-fast ("deep") models. Counted inside monthly_coach_limit.
    monthly_deep_limit: int = 0


PLAN_CONFIGS: dict[str, PlanConfig] = {
    "guest": PlanConfig(
        name="guest",
        monthly_coach_limit=2,
        allowed_models=frozenset({"google/gemini-3.7-flash"}),
        default_model="google/gemini-3.7-flash",
    ),
    "free": PlanConfig(
        name="free",
        monthly_coach_limit=5,
        allowed_models=frozenset({"google/gemini-3.7-flash"}),
        default_model="google/gemini-3.7-flash",
    ),
    "pro": PlanConfig(
        name="pro",
        monthly_coach_limit=200,
        allowed_models=DEFAULT_PRO_MODELS,
        default_model=FAST_MODEL,
        monthly_deep_limit=30,
    ),
}


def get_plan_allowed_models(plan_name: str) -> frozenset[str]:
    """Return server-enforced allowed model IDs for a plan."""
    if plan_name == "pro":
        override = os.environ.get("HS_COACH_PRO_MODELS", "").strip()
        if override:
            parsed = frozenset(m.strip() for m in override.split(",") if m.strip())
            if parsed:
                return parsed
    cfg = PLAN_CONFIGS.get(plan_name, PLAN_CONFIGS["free"])
    return cfg.allowed_models


def get_session_secret() -> str:
    """Resolve session signing secret from env var or ephemeral process secret."""
    env_secret = os.environ.get("HS_COACH_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret
    return _PROCESS_FALLBACK_SECRET


_PROCESS_FALLBACK_SECRET = secrets.token_hex(32)


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a random 16-byte salt."""
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2 hash."""
    try:
        alg, iters_s, salt_hex, dk_hex = stored.split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except Exception:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(candidate, expected)


def generate_upload_token() -> str:
    """Generate a per-user Bearer token for `hs-coach watch`."""
    return "hsc_" + secrets.token_urlsafe(24)


def create_session_token(
    user_id: str,
    secret: str,
    *,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    session_id: str | None = None,
) -> str:
    """Create an HMAC-SHA256 signed session cookie token with an embedded session ID (jti)."""
    exp = int(time.time()) + ttl_seconds
    jti = session_id or secrets.token_hex(16)
    signing_input = f"session:{user_id}:{exp}:{jti}"
    sig = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{user_id}:{exp}:{jti}:{sig}"


def verify_session_token_details(
    token: str, secret: str
) -> tuple[str, str, int] | None:
    """Verify signature and expiration; return (user_id, session_id, exp) or None."""
    if not token or not secret:
        return None
    parts = token.split(":")
    if len(parts) != 4:
        return None
    user_id, exp_s, jti, sig = parts
    if not user_id or not jti or not sig:
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    signing_input = f"session:{user_id}:{exp_s}:{jti}"
    expected_sig = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return user_id, jti, exp


def verify_session_token(token: str, secret: str) -> str | None:
    """Verify signature and expiration; return user_id or None."""
    details = verify_session_token_details(token, secret)
    return details[0] if details is not None else None


def create_guest_token(
    guest_id: str,
    secret: str,
    *,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> str:
    """Create an HMAC-SHA256 signed isolated guest cookie token."""
    if not guest_id.startswith("guest_"):
        raise ValueError("guest_id must start with 'guest_'")
    exp = int(time.time()) + ttl_seconds
    signing_input = f"guest:{guest_id}:{exp}"
    sig = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{guest_id}:{exp}:{sig}"


def verify_guest_token(token: str, secret: str) -> str | None:
    """Verify signed isolated guest cookie; return guest_id or None."""
    if not token or not secret:
        return None
    parts = token.split(":")
    if len(parts) != 3:
        return None
    guest_id, exp_s, sig = parts
    if not guest_id.startswith("guest_") or not sig:
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    signing_input = f"guest:{guest_id}:{exp_s}"
    expected_sig = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return guest_id


def hash_reset_token(raw_token: str) -> str:
    """Return SHA-256 hex digest of a raw password reset token."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def generate_password_reset_token() -> tuple[str, str]:
    """Generate a high-entropy single-use reset token and its SHA-256 hash."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_reset_token(raw)


def send_password_reset_email(
    to_email: str,
    reset_url: str,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Deliver a password reset email via Resend or SMTP if configured; return True if sent."""
    subject = "Reset your HS-Coach password"
    body = (
        "Use this one-time link within 15 minutes to reset your HS-Coach password:\n\n"
        f"{reset_url}\n\n"
        "If you did not request a password reset, you can safely ignore this email."
    )

    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if resend_key:
        sender = os.environ.get("HS_COACH_EMAIL_FROM", "HS-Coach <noreply@hs-coach.app>").strip()
        owns_client = client is None
        http = client or httpx.Client(timeout=15.0)
        try:
            resp = http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}"},
                json={
                    "from": sender,
                    "to": [to_email],
                    "subject": subject,
                    "text": body,
                },
            )
            resp.raise_for_status()
            return True
        finally:
            if owns_client:
                http.close()

    smtp_host = os.environ.get("HS_COACH_SMTP_HOST", "").strip()
    if smtp_host:
        smtp_port = int(os.environ.get("HS_COACH_SMTP_PORT", "587").strip() or "587")
        smtp_user = os.environ.get("HS_COACH_SMTP_USER", "").strip()
        smtp_pass = os.environ.get("HS_COACH_SMTP_PASS", "").strip()
        sender = os.environ.get(
            "HS_COACH_EMAIL_FROM", smtp_user or "noreply@hs-coach.app"
        ).strip()
        use_tls = os.environ.get("HS_COACH_SMTP_TLS", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(body)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True

    return False

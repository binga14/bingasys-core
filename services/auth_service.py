from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Union

from config import settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_ITERATIONS = 260_000


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))


def validate_password(password: str) -> bool:
    return len(password) >= 8


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        _b64encode(salt),
        _b64encode(digest),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected_digest = password_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _b64decode(salt),
        int(iterations),
    )
    return hmac.compare_digest(_b64encode(digest), expected_digest)


def create_access_token(user: dict[str, Any]) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.auth_token_expire_minutes
    )
    payload = {
        "sub": str(user["id"]),
        "email": user["email"],
        "exp": int(expires_at.timestamp()),
    }
    encoded_payload = _b64encode_json(payload)
    signature = _sign(encoded_payload)
    return f"{encoded_payload}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Invalid token") from exc

    expected_signature = _sign(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid token")

    payload = json.loads(_b64decode(encoded_payload))
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise ValueError("Token expired")

    return payload


def create_password_reset_token() -> str:
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_reset_expires_at() -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_expire_minutes
    )
    return expires_at.isoformat()


def is_expired(value: Union[datetime, str]) -> bool:
    expires_at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def _sign(value: str) -> str:
    digest = hmac.new(
        settings.auth_secret_key.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _b64encode_json(value: dict[str, Any]) -> str:
    return _b64encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)

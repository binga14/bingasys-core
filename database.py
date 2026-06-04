from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from config import settings
from models import (
    INTEGRATION_SETTINGS_TABLE,
    PASSWORD_RESET_TOKENS_TABLE,
    USERS_TABLE,
    utc_now,
)


def get_connection() -> psycopg2.extensions.connection:
    return psycopg2.connect(settings.database_url, cursor_factory=RealDictCursor)


def init_db() -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(USERS_TABLE)
            cursor.execute(PASSWORD_RESET_TOKENS_TABLE)
            cursor.execute(INTEGRATION_SETTINGS_TABLE)
            cursor.execute(
                """
                ALTER TABLE integration_settings
                ADD COLUMN IF NOT EXISTS shopify_access_token_expires_at TEXT,
                ADD COLUMN IF NOT EXISTS shopify_refresh_token TEXT,
                ADD COLUMN IF NOT EXISTS shopify_refresh_token_expires_at TEXT
                """
            )


def create_user(email: str, password_hash: str) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, password_hash)
                VALUES (%s, %s)
                ON CONFLICT (email) DO NOTHING
                RETURNING id, email, created_at, updated_at
                """,
                (email, password_hash),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            row = cursor.fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, email, created_at, updated_at FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def update_user_password(user_id: int, password_hash: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET password_hash = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (password_hash, user_id),
            )


def save_password_reset_token(
    user_id: int,
    token_hash: str,
    expires_at: str,
) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE password_reset_tokens
                SET used_at = NOW()
                WHERE user_id = %s AND used_at IS NULL
                """,
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (user_id, token_hash, expires_at),
            )
            row = cursor.fetchone()
    return dict(row)


def get_password_reset_token(token_hash: str) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT password_reset_tokens.*, users.email
                FROM password_reset_tokens
                JOIN users ON users.id = password_reset_tokens.user_id
                WHERE password_reset_tokens.token_hash = %s
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def consume_password_reset_token(token_id: int) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE password_reset_tokens
                SET used_at = NOW()
                WHERE id = %s
                """,
                (token_id,),
            )


def get_integration_settings(user_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM integration_settings WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def find_integration_by_webhook_verify_token(
    webhook_verify_token: str,
) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM integration_settings
                WHERE webhook_verify_token = %s
                """,
                (webhook_verify_token,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def save_shopify_connection(
    user_id: int,
    store_domain: str,
    access_token: str,
    access_token_expires_in: Optional[int] = None,
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_settings (
                    user_id,
                    shopify_store_domain,
                    shopify_access_token,
                    shopify_access_token_expires_at,
                    shopify_refresh_token,
                    shopify_refresh_token_expires_at,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    shopify_store_domain = EXCLUDED.shopify_store_domain,
                    shopify_access_token = EXCLUDED.shopify_access_token,
                    shopify_access_token_expires_at = EXCLUDED.shopify_access_token_expires_at,
                    shopify_refresh_token = EXCLUDED.shopify_refresh_token,
                    shopify_refresh_token_expires_at = EXCLUDED.shopify_refresh_token_expires_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    store_domain,
                    access_token,
                    _expires_at(access_token_expires_in),
                    refresh_token,
                    _expires_at(refresh_token_expires_in),
                    utc_now(),
                    utc_now(),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def save_meta_connection(
    user_id: int,
    page_id: str,
    access_token: str,
    webhook_verify_token: str,
    instagram_business_account_id: Optional[str] = None,
) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_settings (
                    user_id,
                    meta_page_id,
                    meta_access_token,
                    instagram_business_account_id,
                    webhook_verify_token,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    meta_page_id = EXCLUDED.meta_page_id,
                    meta_access_token = EXCLUDED.meta_access_token,
                    instagram_business_account_id = EXCLUDED.instagram_business_account_id,
                    webhook_verify_token = EXCLUDED.webhook_verify_token,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    page_id,
                    access_token,
                    instagram_business_account_id,
                    webhook_verify_token,
                    utc_now(),
                    utc_now(),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _expires_at(expires_in: Optional[int]) -> Optional[str]:
    if expires_in is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

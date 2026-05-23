from __future__ import annotations

import sqlite3
from typing import Any, Optional

from config import settings
from models import INTEGRATION_SETTINGS_TABLE, utc_now


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_connection() as connection:
        connection.execute(INTEGRATION_SETTINGS_TABLE)
        connection.commit()


def get_integration_settings() -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM integration_settings WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None


def save_shopify_connection(store_domain: str, access_token: str) -> dict[str, Any]:
    now = utc_now()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO integration_settings (
                id,
                shopify_store_domain,
                shopify_access_token,
                created_at,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                shopify_store_domain = excluded.shopify_store_domain,
                shopify_access_token = excluded.shopify_access_token,
                updated_at = excluded.updated_at
            """,
            (store_domain, access_token, now, now),
        )
        connection.commit()

    return get_integration_settings() or {}


def save_meta_connection(
    page_id: str,
    access_token: str,
    webhook_verify_token: str,
    instagram_business_account_id: Optional[str] = None,
) -> dict[str, Any]:
    now = utc_now()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO integration_settings (
                id,
                meta_page_id,
                meta_access_token,
                instagram_business_account_id,
                webhook_verify_token,
                created_at,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                meta_page_id = excluded.meta_page_id,
                meta_access_token = excluded.meta_access_token,
                instagram_business_account_id = excluded.instagram_business_account_id,
                webhook_verify_token = excluded.webhook_verify_token,
                updated_at = excluded.updated_at
            """,
            (
                page_id,
                access_token,
                instagram_business_account_id,
                webhook_verify_token,
                now,
                now,
            ),
        )
        connection.commit()

    return get_integration_settings() or {}

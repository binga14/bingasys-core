from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
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

logger = logging.getLogger(__name__)


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
                ADD COLUMN IF NOT EXISTS shopify_refresh_token_expires_at TEXT,
                ADD COLUMN IF NOT EXISTS shopify_catalog_synced_at TEXT,
                ADD COLUMN IF NOT EXISTS shopify_catalog_sync_status TEXT,
                ADD COLUMN IF NOT EXISTS meta_page_name TEXT,
                ADD COLUMN IF NOT EXISTS meta_user_access_token TEXT,
                ADD COLUMN IF NOT EXISTS meta_user_token_expires_at TEXT,
                ADD COLUMN IF NOT EXISTS instagram_username TEXT
                """
            )
            _init_catalog_tables(cursor)


def _init_catalog_tables(cursor: psycopg2.extensions.cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS shopify_products (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            shop_domain TEXT NOT NULL,
            shopify_product_id TEXT NOT NULL,
            admin_graphql_api_id TEXT,
            title TEXT NOT NULL,
            handle TEXT,
            status TEXT,
            vendor TEXT,
            product_type TEXT,
            tags TEXT,
            raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            UNIQUE (user_id, shopify_product_id)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS shopify_products_user_title_idx
        ON shopify_products (user_id, title)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS shopify_product_variants (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            shopify_product_id TEXT NOT NULL,
            shopify_variant_id TEXT NOT NULL,
            admin_graphql_api_id TEXT,
            title TEXT,
            sku TEXT,
            price NUMERIC,
            inventory_item_id TEXT,
            inventory_quantity INTEGER,
            inventory_management TEXT,
            inventory_policy TEXT,
            raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            UNIQUE (user_id, shopify_variant_id)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS shopify_product_variants_product_idx
        ON shopify_product_variants (user_id, shopify_product_id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS shopify_product_variants_inventory_item_idx
        ON shopify_product_variants (user_id, inventory_item_id)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS shopify_product_images (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            shopify_product_id TEXT NOT NULL,
            shopify_image_id TEXT,
            admin_graphql_api_id TEXT,
            media_id TEXT,
            image_url TEXT NOT NULL,
            position INTEGER,
            alt TEXT,
            variant_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            UNIQUE (user_id, shopify_product_id, image_url)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS shopify_product_images_product_idx
        ON shopify_product_images (user_id, shopify_product_id)
        """
    )
    _init_product_image_embeddings_table(cursor)


def _init_product_image_embeddings_table(cursor: psycopg2.extensions.cursor) -> None:
    cursor.execute("SAVEPOINT pgvector_setup")
    try:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS product_image_embeddings (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                shopify_product_id TEXT NOT NULL,
                shopify_variant_id TEXT,
                shopify_image_id TEXT,
                image_url TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimension INTEGER NOT NULL,
                embedding VECTOR({settings.gemini_embedding_dimensions}),
                embedding_values JSONB NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE product_image_embeddings
            ADD COLUMN IF NOT EXISTS embedding VECTOR({settings.gemini_embedding_dimensions})
            """
        )
        cursor.execute(
            """
            UPDATE product_image_embeddings
            SET embedding = embedding_values::text::vector
            WHERE embedding IS NULL
              AND embedding_dimension = %s
            """,
            (settings.gemini_embedding_dimensions,),
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS product_image_embeddings_unique_idx
            ON product_image_embeddings (
                user_id,
                shopify_product_id,
                (COALESCE(shopify_variant_id, '')),
                image_url,
                embedding_model,
                embedding_dimension
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS product_image_embeddings_vector_idx
            ON product_image_embeddings
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
            """
        )
        cursor.execute("RELEASE SAVEPOINT pgvector_setup")
    except psycopg2.Error as exc:
        logger.warning(
            "pgvector is not available; product image embeddings will use JSONB fallback: %s",
            exc,
        )
        cursor.execute("ROLLBACK TO SAVEPOINT pgvector_setup")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS product_image_embeddings (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                shopify_product_id TEXT NOT NULL,
                shopify_variant_id TEXT,
                shopify_image_id TEXT,
                image_url TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimension INTEGER NOT NULL,
                embedding_values JSONB NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS product_image_embeddings_unique_idx
            ON product_image_embeddings (
                user_id,
                shopify_product_id,
                (COALESCE(shopify_variant_id, '')),
                image_url,
                embedding_model,
                embedding_dimension
            )
            """
        )
        cursor.execute("RELEASE SAVEPOINT pgvector_setup")


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


def find_integration_by_meta_page_id(page_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM integration_settings
                WHERE meta_page_id = %s
                """,
                (page_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def find_integration_by_shop_domain(shop_domain: str) -> Optional[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM integration_settings
                WHERE shopify_store_domain = %s
                """,
                (shop_domain,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def list_shopify_integrations() -> list[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM integration_settings
                WHERE shopify_store_domain IS NOT NULL
                  AND shopify_access_token IS NOT NULL
                  AND TRIM(shopify_store_domain) <> ''
                  AND TRIM(shopify_access_token) <> ''
                """
            )
            rows = cursor.fetchall()
    return [dict(row) for row in rows]


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


def save_shopify_catalog_sync_status(
    user_id: int,
    status: str,
    synced_at: Optional[str] = None,
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_settings
                SET shopify_catalog_sync_status = %s,
                    shopify_catalog_synced_at = COALESCE(%s, shopify_catalog_synced_at),
                    updated_at = %s
                WHERE user_id = %s
                """,
                (status, synced_at, utc_now(), user_id),
            )


def save_meta_connection(
    user_id: int,
    page_id: str,
    access_token: str,
    webhook_verify_token: str,
    instagram_business_account_id: Optional[str] = None,
    page_name: Optional[str] = None,
    instagram_username: Optional[str] = None,
) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_settings (
                    user_id,
                    meta_page_id,
                    meta_page_name,
                    meta_access_token,
                    instagram_business_account_id,
                    instagram_username,
                    webhook_verify_token,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    meta_page_id = EXCLUDED.meta_page_id,
                    meta_page_name = EXCLUDED.meta_page_name,
                    meta_access_token = EXCLUDED.meta_access_token,
                    instagram_business_account_id = EXCLUDED.instagram_business_account_id,
                    instagram_username = EXCLUDED.instagram_username,
                    webhook_verify_token = EXCLUDED.webhook_verify_token,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    page_id,
                    page_name,
                    access_token,
                    instagram_business_account_id,
                    instagram_username,
                    webhook_verify_token,
                    utc_now(),
                    utc_now(),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def save_meta_oauth_authorization(
    user_id: int,
    user_access_token: str,
    token_expires_in: Optional[int] = None,
    webhook_verify_token: Optional[str] = None,
) -> dict[str, Any]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_settings (
                    user_id,
                    meta_user_access_token,
                    meta_user_token_expires_at,
                    webhook_verify_token,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    meta_user_access_token = EXCLUDED.meta_user_access_token,
                    meta_user_token_expires_at = EXCLUDED.meta_user_token_expires_at,
                    webhook_verify_token = COALESCE(
                        integration_settings.webhook_verify_token,
                        EXCLUDED.webhook_verify_token
                    ),
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    user_access_token,
                    _expires_at(token_expires_in),
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

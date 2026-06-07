from datetime import datetime, timezone


USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


PASSWORD_RESET_TOKENS_TABLE = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


INTEGRATION_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS integration_settings (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    shopify_store_domain TEXT,
    shopify_access_token TEXT,
    shopify_access_token_expires_at TEXT,
    shopify_refresh_token TEXT,
    shopify_refresh_token_expires_at TEXT,
    meta_page_id TEXT,
    meta_page_name TEXT,
    meta_access_token TEXT,
    meta_user_access_token TEXT,
    meta_user_token_expires_at TEXT,
    instagram_business_account_id TEXT,
    instagram_username TEXT,
    webhook_verify_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

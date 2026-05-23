from datetime import datetime, timezone


INTEGRATION_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS integration_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    shopify_store_domain TEXT,
    shopify_access_token TEXT,
    meta_page_id TEXT,
    meta_access_token TEXT,
    instagram_business_account_id TEXT,
    webhook_verify_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

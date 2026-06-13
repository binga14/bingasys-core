from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    load_dotenv = None

if load_dotenv:
    load_dotenv()


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Bingasys Core")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/bingasys",
    )
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    backend_url: str = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
    cors_origins: list[str] = field(
        default_factory=lambda: _csv(
            os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            )
        )
    )
    auth_secret_key: str = os.getenv(
        "AUTH_SECRET_KEY",
        "change-this-development-secret-before-production",
    )
    auth_token_expire_minutes: int = int(os.getenv("AUTH_TOKEN_EXPIRE_MINUTES", "1440"))
    password_reset_expire_minutes: int = int(
        os.getenv("PASSWORD_RESET_EXPIRE_MINUTES", "30")
    )
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "no-reply@bingasys.local")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    gemini_vision_model: str = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
    gemini_embedding_model: str = os.getenv(
        "GEMINI_EMBEDDING_MODEL",
        "gemini-embedding-2",
    )
    gemini_embedding_dimensions: int = int(
        os.getenv("GEMINI_EMBEDDING_DIMENSIONS", "768")
    )
    product_image_download_max_bytes: int = int(
        os.getenv("PRODUCT_IMAGE_DOWNLOAD_MAX_BYTES", "12582912")
    )
    gemini_api_base_url: str = os.getenv(
        "GEMINI_API_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    )
    gemini_temperature: float = float(os.getenv("GEMINI_TEMPERATURE", "0.4"))
    gemini_max_output_tokens: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "512"))
    shopify_client_id: str = os.getenv("SHOPIFY_CLIENT_ID", "")
    shopify_client_secret: str = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    shopify_scopes: str = os.getenv(
        "SHOPIFY_SCOPES",
        "read_products,read_inventory,read_orders,write_orders",
    )
    shopify_redirect_uri: str = os.getenv(
        "SHOPIFY_REDIRECT_URI",
        f"{os.getenv('BACKEND_URL', 'http://127.0.0.1:8000').rstrip('/')}"
        "/api/integrations/shopify/oauth/callback",
    )
    shopify_oauth_state_expire_minutes: int = int(
        os.getenv("SHOPIFY_OAUTH_STATE_EXPIRE_MINUTES", "10")
    )
    shopify_api_version: str = os.getenv("SHOPIFY_API_VERSION", "2025-10")
    shopify_product_cache_ttl_seconds: int = int(
        os.getenv("SHOPIFY_PRODUCT_CACHE_TTL_SECONDS", "900")
    )
    shopify_image_match_catalog_limit: int = int(
        os.getenv("SHOPIFY_IMAGE_MATCH_CATALOG_LIMIT", "120")
    )
    shopify_image_match_top_k: int = int(os.getenv("SHOPIFY_IMAGE_MATCH_TOP_K", "5"))
    shopify_image_match_high_threshold: float = float(
        os.getenv("SHOPIFY_IMAGE_MATCH_HIGH_THRESHOLD", "0.82")
    )
    shopify_image_match_medium_threshold: float = float(
        os.getenv("SHOPIFY_IMAGE_MATCH_MEDIUM_THRESHOLD", "0.72")
    )
    shopify_daily_sync_enabled: bool = _bool(
        os.getenv("SHOPIFY_DAILY_SYNC_ENABLED", "true")
    )
    shopify_daily_sync_interval_seconds: int = int(
        os.getenv("SHOPIFY_DAILY_SYNC_INTERVAL_SECONDS", "86400")
    )
    shopify_webhook_topics: list[str] = field(
        default_factory=lambda: _csv(
            os.getenv(
                "SHOPIFY_WEBHOOK_TOPICS",
                "products/create,products/update,products/delete,"
                "inventory_levels/update,inventory_items/update",
            )
        )
    )
    meta_app_id: str = os.getenv("META_APP_ID", "")
    meta_app_secret: str = os.getenv("META_APP_SECRET", "")
    meta_graph_api_version: str = os.getenv("META_GRAPH_API_VERSION", "v21.0")
    meta_scopes: str = os.getenv(
        "META_SCOPES",
        "pages_show_list,pages_messaging,pages_manage_metadata,business_management",
    )
    meta_redirect_uri: str = os.getenv(
        "META_REDIRECT_URI",
        f"{os.getenv('BACKEND_URL', 'http://127.0.0.1:8000').rstrip('/')}"
        "/api/integrations/meta/oauth/callback",
    )
    meta_oauth_state_expire_minutes: int = int(
        os.getenv("META_OAUTH_STATE_EXPIRE_MINUTES", "10")
    )
    meta_message_debounce_seconds: float = float(
        os.getenv("META_MESSAGE_DEBOUNCE_SECONDS", "5.0")
    )
    meta_webhook_debug: bool = _bool(os.getenv("META_WEBHOOK_DEBUG", "true"))


settings = Settings()

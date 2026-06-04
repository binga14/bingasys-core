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


settings = Settings()

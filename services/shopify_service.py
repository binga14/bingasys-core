from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from config import settings

SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$")


class ShopifyOAuthError(ValueError):
    pass


def normalize_shop_domain(value: str) -> str:
    shop = value.strip().lower()
    shop = shop.removeprefix("https://").removeprefix("http://").strip("/")
    if "." not in shop:
        shop = f"{shop}.myshopify.com"
    if not SHOP_DOMAIN_RE.fullmatch(shop):
        raise ShopifyOAuthError(
            "Use your .myshopify.com store domain or store handle, not the storefront domain"
        )
    return shop


def ensure_oauth_configured() -> None:
    missing = []
    if not settings.shopify_client_id:
        missing.append("SHOPIFY_CLIENT_ID")
    if not settings.shopify_client_secret:
        missing.append("SHOPIFY_CLIENT_SECRET")
    if missing:
        raise ShopifyOAuthError(
            "Shopify OAuth is missing backend config: " + ", ".join(missing)
        )


def build_authorization_url(shop_domain: str, user_id: int) -> str:
    shop = normalize_shop_domain(shop_domain)
    ensure_oauth_configured()
    state = create_oauth_state(user_id=user_id, shop_domain=shop)
    query = urlencode(
        {
            "client_id": settings.shopify_client_id,
            "scope": settings.shopify_scopes,
            "redirect_uri": settings.shopify_redirect_uri,
            "state": state,
        }
    )
    return f"https://{shop}/admin/oauth/authorize?{query}"


def create_oauth_state(user_id: int, shop_domain: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.shopify_oauth_state_expire_minutes
    )
    payload = {
        "user_id": user_id,
        "shop": shop_domain,
        "exp": int(expires_at.timestamp()),
    }
    encoded_payload = _b64encode_json(payload)
    signature = _sign(encoded_payload)
    return f"{encoded_payload}.{signature}"


def decode_oauth_state(state: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = state.split(".", 1)
    except ValueError as exc:
        raise ShopifyOAuthError("Invalid Shopify authorization state") from exc

    expected_signature = _sign(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise ShopifyOAuthError("Invalid Shopify authorization state")

    payload = json.loads(_b64decode(encoded_payload))
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise ShopifyOAuthError("Shopify authorization expired")
    return payload


def verify_callback_hmac(query_items: list[tuple[str, str]]) -> bool:
    provided_hmac = next((value for key, value in query_items if key == "hmac"), "")
    if not provided_hmac:
        return False

    message = "&".join(
        f"{key}={value}"
        for key, value in sorted(
            (key, value)
            for key, value in query_items
            if key not in {"hmac", "signature"}
        )
    )
    digest = hmac.new(
        settings.shopify_client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, provided_hmac)


async def exchange_code_for_access_token(shop_domain: str, code: str) -> dict[str, Any]:
    ensure_oauth_configured()
    shop = normalize_shop_domain(shop_domain)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "client_id": settings.shopify_client_id,
                "client_secret": settings.shopify_client_secret,
                "code": code,
                "expiring": "1",
            },
            headers={"Accept": "application/json"},
        )

    if response.status_code >= 400:
        raise ShopifyOAuthError("Shopify did not return an access token")

    data = response.json()
    if not data.get("access_token"):
        raise ShopifyOAuthError("Shopify did not return an access token")
    return data


async def migrate_to_expiring_offline_token(
    shop_domain: str,
    access_token: str,
) -> dict[str, Any]:
    ensure_oauth_configured()
    shop = normalize_shop_domain(shop_domain)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "client_id": settings.shopify_client_id,
                "client_secret": settings.shopify_client_secret,
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": access_token,
                "subject_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
                "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
                "expiring": "1",
            },
            headers={"Accept": "application/json"},
        )

    if response.status_code >= 400:
        raise ShopifyOAuthError("Shopify did not migrate the access token")

    data = response.json()
    if not data.get("access_token") or not data.get("refresh_token"):
        raise ShopifyOAuthError("Shopify did not return expiring token details")
    return data


async def get_inventory_placeholder(store_domain: str, access_token: str) -> dict[str, str]:
    return {
        "store_domain": store_domain,
        "status": "not_implemented",
        "message": "Shopify inventory will be fetched live from Shopify APIs later.",
    }


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

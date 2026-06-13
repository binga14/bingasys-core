from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from config import settings

SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$")
MAX_PRODUCT_FETCH_PAGES = 20
_product_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class ShopifyOAuthError(ValueError):
    pass


class ShopifyAPIError(RuntimeError):
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


async def refresh_expiring_offline_token(
    shop_domain: str,
    refresh_token: str,
) -> dict[str, Any]:
    ensure_oauth_configured()
    shop = normalize_shop_domain(shop_domain)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "client_id": settings.shopify_client_id,
                "client_secret": settings.shopify_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
        )

    if response.status_code >= 400:
        raise ShopifyOAuthError("Shopify did not refresh the access token")

    data = response.json()
    if not data.get("access_token"):
        raise ShopifyOAuthError("Shopify did not return a refreshed access token")
    return data


async def get_inventory_placeholder(store_domain: str, access_token: str) -> dict[str, str]:
    return {
        "store_domain": store_domain,
        "status": "not_implemented",
        "message": "Shopify inventory will be fetched live from Shopify APIs later.",
    }


async def fetch_all_products(
    store_domain: str,
    access_token: str,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    shop = normalize_shop_domain(store_domain)
    return await _fetch_products(shop, access_token, force_refresh=force_refresh)


async def search_product_summaries(
    store_domain: str,
    access_token: str,
    buyer_text: str,
    limit: int = 3,
) -> dict[str, Any]:
    shop = normalize_shop_domain(store_domain)
    products = await _fetch_products(shop, access_token)
    matches = _rank_products(products, buyer_text)[:limit]
    if not matches:
        # A product added since the cache was last filled (TTL up to 15 min) would
        # otherwise look "not found". Refetch once, bypassing the cache, before
        # telling the buyer we don't have it.
        products = await _fetch_products(shop, access_token, force_refresh=True)
        matches = _rank_products(products, buyer_text)[:limit]
    return {
        "status": "found" if matches else "not_found",
        "query": buyer_text,
        "products": [_product_summary(product) for product in matches],
    }


async def list_product_summaries(
    store_domain: str,
    access_token: str,
    limit: int = 24,
    with_images_only: bool = False,
) -> list[dict[str, Any]]:
    shop = normalize_shop_domain(store_domain)
    products = await _fetch_products(shop, access_token)
    summaries = [_product_summary(product) for product in products]
    if with_images_only:
        summaries = [summary for summary in summaries if summary.get("images")]
    return summaries[:limit]


async def fetch_product_by_id(
    store_domain: str,
    access_token: str,
    shopify_product_id: str,
) -> dict[str, Any]:
    shop = normalize_shop_domain(store_domain)
    url = (
        f"https://{shop}/admin/api/{settings.shopify_api_version}"
        f"/products/{shopify_product_id}.json"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
            params={"fields": "id,title,handle,status,vendor,product_type,tags,variants,images,image,admin_graphql_api_id"},
        )
    if response.status_code >= 400:
        raise ShopifyAPIError(_format_shopify_error(response))
    product = response.json().get("product")
    if not product:
        raise ShopifyAPIError("Shopify did not return the product")
    return product


async def fetch_product_summaries_by_ids(
    store_domain: str,
    access_token: str,
    shopify_product_ids: list[str],
) -> dict[str, Any]:
    products = []
    for product_id in shopify_product_ids:
        try:
            product = await fetch_product_by_id(store_domain, access_token, product_id)
        except ShopifyAPIError:
            continue
        products.append(_product_summary(product))
    return {
        "status": "found" if products else "not_found",
        "query": ",".join(shopify_product_ids),
        "source": "shopify_live_product_check",
        "products": products,
    }


async def create_order(
    store_domain: str,
    access_token: str,
    order: dict[str, Any],
) -> dict[str, Any]:
    shop = normalize_shop_domain(store_domain)
    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/orders.json"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
            json={"order": order},
        )

    if response.status_code >= 400:
        raise ShopifyAPIError(_format_shopify_error(response))
    data = response.json().get("order")
    if not data:
        raise ShopifyAPIError("Shopify did not return the created order")
    return data


async def ensure_webhook_subscriptions(
    store_domain: str,
    access_token: str,
    callback_url: str,
    topics: list[str],
) -> list[dict[str, Any]]:
    existing = await list_webhooks(store_domain, access_token)
    results = []
    for topic in topics:
        match = next(
            (
                webhook
                for webhook in existing
                if webhook.get("topic") == topic and webhook.get("address") == callback_url
            ),
            None,
        )
        if match:
            results.append(match)
            continue
        results.append(
            await create_webhook_subscription(
                store_domain=store_domain,
                access_token=access_token,
                topic=topic,
                callback_url=callback_url,
            )
        )
    return results


async def list_webhooks(store_domain: str, access_token: str) -> list[dict[str, Any]]:
    shop = normalize_shop_domain(store_domain)
    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/webhooks.json"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
        )
    if response.status_code >= 400:
        raise ShopifyAPIError(_format_shopify_error(response))
    return response.json().get("webhooks", [])


async def create_webhook_subscription(
    store_domain: str,
    access_token: str,
    topic: str,
    callback_url: str,
) -> dict[str, Any]:
    shop = normalize_shop_domain(store_domain)
    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/webhooks.json"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
            json={
                "webhook": {
                    "topic": topic,
                    "address": callback_url,
                    "format": "json",
                }
            },
        )
    if response.status_code >= 400:
        raise ShopifyAPIError(_format_shopify_error(response))
    webhook = response.json().get("webhook")
    if not webhook:
        raise ShopifyAPIError("Shopify did not return the webhook subscription")
    return webhook


def verify_webhook_hmac(body: bytes, hmac_header: Optional[str]) -> bool:
    if not hmac_header:
        return False
    digest = hmac.new(
        settings.shopify_client_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, hmac_header)


def clear_product_cache(store_domain: Optional[str] = None) -> None:
    if not store_domain:
        _product_cache.clear()
        return
    shop = normalize_shop_domain(store_domain)
    for key in list(_product_cache.keys()):
        if key.startswith(f"{shop}:"):
            _product_cache.pop(key, None)


async def _fetch_products(
    shop_domain: str,
    access_token: str,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    cache_key = f"{shop_domain}:{access_token[-12:]}"
    cached = _product_cache.get(cache_key)
    if (
        not force_refresh
        and cached
        and time.monotonic() - cached[0] < settings.shopify_product_cache_ttl_seconds
    ):
        return cached[1]

    url = (
        f"https://{shop_domain}/admin/api/{settings.shopify_api_version}"
        "/products.json"
    )
    products: list[dict[str, Any]] = []
    headers = {
        "Accept": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        next_url: Optional[str] = url
        params: Optional[dict[str, Any]] = {
            "limit": 250,
            "fields": "id,title,handle,status,vendor,product_type,tags,variants,images,image,admin_graphql_api_id",
        }
        pages_fetched = 0

        while next_url and pages_fetched < MAX_PRODUCT_FETCH_PAGES:
            response = await client.get(next_url, params=params, headers=headers)
            if response.status_code >= 400:
                raise ShopifyAPIError(_format_shopify_error(response))

            products.extend(response.json().get("products", []))
            next_url = _next_link_url(response.headers.get("link", ""))
            params = None
            pages_fetched += 1

    _product_cache[cache_key] = (time.monotonic(), products)
    return products


def _next_link_url(link_header: str) -> Optional[str]:
    for item in link_header.split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', item.strip(), flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _rank_products(
    products: list[dict[str, Any]],
    buyer_text: str,
) -> list[dict[str, Any]]:
    buyer_tokens = set(_tokens(buyer_text))
    normalized_buyer_text = _normalize(buyer_text)
    exact_matches = [
        product
        for product in products
        if _normalize(str(product.get("title", "")))
        and _normalize(str(product.get("title", ""))) in normalized_buyer_text
    ]
    if exact_matches:
        return exact_matches

    ranked = []
    for product in products:
        title_tokens = set(_tokens(str(product.get("title", "")))) | set(
            _tokens(str(product.get("handle", "")))
        )
        type_tokens = set(_tokens(str(product.get("product_type", ""))))
        meta_tokens = set(_tokens(str(product.get("tags", "")))) | set(
            _tokens(str(product.get("vendor", "")))
        )
        variant_text = " ".join(
            f"{variant.get('title') or ''} {variant.get('sku') or ''}"
            for variant in product.get("variants") or []
        )
        variant_tokens = set(_tokens(variant_text))
        if not (title_tokens or type_tokens or variant_tokens or meta_tokens):
            continue

        # Weighted, prefix-tolerant matching across every field Shopify gives us.
        # Reading product_type/tags/vendor (e.g. type "T-Shirts") and tolerating
        # plurals (shirt/shirts, shoe/shoes) is what lets the catalog answer
        # category-style questions — without any hardcoded product vocabulary.
        score = 0.0
        score += 1.0 * _matched_token_count(buyer_tokens, title_tokens)
        score += 0.8 * _matched_token_count(buyer_tokens, variant_tokens)
        score += 0.6 * _matched_token_count(buyer_tokens, type_tokens)
        score += 0.4 * _matched_token_count(buyer_tokens, meta_tokens)
        if normalized_buyer_text and normalized_buyer_text in _normalize(
            str(product.get("title", ""))
        ):
            score += 4
        haystack = title_tokens | variant_tokens | type_tokens
        if any(
            any(char.isdigit() for char in token) and token in haystack
            for token in buyer_tokens
        ):
            score += 2
        if score > 0:
            ranked.append((score, product))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [product for _, product in ranked]


def _matched_token_count(query_tokens: set[str], haystack_tokens: set[str]) -> int:
    """Count query tokens that match a haystack token exactly or by prefix.

    Prefix tolerance (for tokens of length >= 4) makes plural/singular and
    partial words match without maintaining any word list: 'shirts' matches
    'shirt', 'snowboards' matches 'snowboard'.
    """
    matched = 0
    for query in query_tokens:
        if any(_tokens_match(query, candidate) for candidate in haystack_tokens):
            matched += 1
    return matched


def _tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def _product_summary(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("variants") or []
    variant_summaries = [_variant_summary(variant) for variant in variants]
    prices = [
        float(variant["price"])
        for variant in variant_summaries
        if variant.get("price") not in {None, ""}
    ]
    available = any(variant.get("available") for variant in variant_summaries)

    return {
        "id": product.get("id"),
        "title": product.get("title"),
        "handle": product.get("handle"),
        "status": product.get("status"),
        "available": available,
        "price": _price_range(prices),
        "variants": variant_summaries[:8],
        "images": _product_image_urls(product)[:3],
    }


def _variant_summary(variant: dict[str, Any]) -> dict[str, Any]:
    inventory_quantity = variant.get("inventory_quantity")
    tracks_inventory = variant.get("inventory_management") not in {None, ""}
    return {
        "id": variant.get("id"),
        "title": variant.get("title"),
        "sku": variant.get("sku"),
        "price": variant.get("price"),
        "inventory_item_id": variant.get("inventory_item_id"),
        "inventory_quantity": inventory_quantity,
        "inventory_policy": variant.get("inventory_policy"),
        "tracks_inventory": tracks_inventory,
        "available": (
            int(inventory_quantity or 0) > 0
            or variant.get("inventory_policy") == "continue"
            or not tracks_inventory
        ),
    }


def _product_image_urls(product: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    primary = product.get("image") or {}
    if isinstance(primary.get("src"), str):
        urls.append(primary["src"])
    for image in product.get("images") or []:
        if isinstance(image.get("src"), str) and image["src"] not in urls:
            urls.append(image["src"])
    return urls


def _price_range(prices: list[float]) -> Optional[str]:
    if not prices:
        return None
    minimum = min(prices)
    maximum = max(prices)
    if minimum == maximum:
        return _format_price(minimum)
    return f"{_format_price(minimum)} - {_format_price(maximum)}"


def _format_price(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 1
        and token
        not in {
            "the",
            "and",
            "for",
            "with",
            "price",
            "available",
            "availability",
            "product",
            "name",
            "whats",
            "what",
            "how",
            "much",
            "is",
            "it",
            # generic conversational fillers so phrasing does not pollute matching
            "do",
            "you",
            "your",
            "have",
            "has",
            "any",
            "are",
            "can",
            "could",
            "would",
            "get",
            "got",
            "want",
            "need",
            "looking",
            "show",
            "me",
            "we",
            "our",
            "this",
            "that",
            "these",
            "those",
            "please",
            "thanks",
            "thank",
            "hi",
            "hello",
            "hey",
            "some",
            "about",
            "tell",
            "sell",
            "carry",
            "stock",
            "in",
            "of",
            "to",
        }
    ]


def _normalize(value: str) -> str:
    return " ".join(_tokens(value))


def _format_shopify_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"Shopify API returned {response.status_code}: {detail}"


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

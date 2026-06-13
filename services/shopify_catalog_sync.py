from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from database import (
    list_shopify_integrations,
    save_shopify_catalog_sync_status,
    save_shopify_connection,
)
from services.image_embedding_service import generate_image_embedding_from_url
from services.product_metadata_store import (
    mark_missing_catalog_rows_deleted,
    mark_product_deleted,
    upsert_shopify_product,
    upsert_shopify_product_image,
    upsert_shopify_variant,
    update_inventory_by_inventory_item_id,
)
from services.shopify_service import (
    ShopifyAPIError,
    ShopifyOAuthError,
    clear_product_cache,
    fetch_all_products,
    fetch_product_by_id,
    refresh_expiring_offline_token,
)
from services.vector_store import embedding_exists, upsert_image_embedding

logger = logging.getLogger(__name__)
_daily_sync_task: Optional[asyncio.Task] = None


async def sync_shopify_catalog_for_integration(
    integration: dict[str, Any],
    force_refresh: bool = True,
) -> dict[str, Any]:
    user_id = int(integration["user_id"])
    store_domain = integration.get("shopify_store_domain")
    access_token = integration.get("shopify_access_token")
    if not store_domain or not access_token:
        raise ShopifyOAuthError("Shopify is not connected")

    try:
        access_token = await _refresh_shopify_token_if_needed(integration)
        products = await fetch_all_products(
            store_domain=store_domain,
            access_token=access_token,
            force_refresh=force_refresh,
        )
        result = await sync_shopify_products_payload(
            user_id=user_id,
            shop_domain=store_domain,
            products=products,
            full_sync=True,
        )
        save_shopify_catalog_sync_status(
            user_id=user_id,
            status="ok",
            synced_at=datetime.now(timezone.utc).isoformat(),
        )
        return result
    except Exception as exc:
        save_shopify_catalog_sync_status(user_id=user_id, status=f"failed: {exc}")
        raise


async def sync_shopify_products_payload(
    user_id: int,
    shop_domain: str,
    products: list[dict[str, Any]],
    full_sync: bool = False,
) -> dict[str, Any]:
    stats = {
        "products": 0,
        "variants": 0,
        "images": 0,
        "embeddings_created": 0,
        "embeddings_skipped": 0,
        "embedding_errors": 0,
    }
    seen_product_ids: list[str] = []
    seen_variant_ids: list[str] = []
    seen_image_urls: list[str] = []

    for product in products:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        upsert_shopify_product(user_id, shop_domain, product)
        seen_product_ids.append(product_id)
        stats["products"] += 1

        for variant in product.get("variants") or []:
            variant_id = str(variant.get("id") or "")
            if not variant_id:
                continue
            upsert_shopify_variant(user_id, product_id, variant)
            seen_variant_ids.append(variant_id)
            stats["variants"] += 1

        for image in _product_images(product):
            image_url = image.get("src") or image.get("url")
            if not image_url:
                continue
            image_row = upsert_shopify_product_image(user_id, product_id, image)
            seen_image_urls.append(image_url)
            stats["images"] += 1
            image_stats = await _ensure_embeddings_for_image(
                user_id=user_id,
                product_id=product_id,
                image_row=image_row,
                variant_ids=[str(value) for value in image.get("variant_ids") or []],
            )
            stats["embeddings_created"] += image_stats["created"]
            stats["embeddings_skipped"] += image_stats["skipped"]
            stats["embedding_errors"] += image_stats["errors"]

    if full_sync:
        mark_missing_catalog_rows_deleted(
            user_id=user_id,
            seen_product_ids=seen_product_ids,
            seen_variant_ids=seen_variant_ids,
            seen_image_urls=seen_image_urls,
        )
    return stats


async def sync_single_shopify_product(
    integration: dict[str, Any],
    shopify_product_id: str,
) -> dict[str, Any]:
    access_token = await _refresh_shopify_token_if_needed(integration)
    product = await fetch_product_by_id(
        store_domain=integration["shopify_store_domain"],
        access_token=access_token,
        shopify_product_id=shopify_product_id,
    )
    return await sync_shopify_products_payload(
        user_id=int(integration["user_id"]),
        shop_domain=integration["shopify_store_domain"],
        products=[product],
        full_sync=False,
    )


async def handle_product_webhook(
    integration: dict[str, Any],
    topic: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    user_id = int(integration["user_id"])
    product_id = str(payload.get("id") or "")
    if not product_id:
        return {"status": "ignored", "reason": "missing_product_id"}

    clear_product_cache(integration["shopify_store_domain"])
    if topic == "products/delete":
        mark_product_deleted(user_id, product_id)
        return {"status": "deleted", "product_id": product_id}

    return {
        "status": "synced",
        "product_id": product_id,
        "sync": await sync_shopify_products_payload(
            user_id=user_id,
            shop_domain=integration["shopify_store_domain"],
            products=[payload],
            full_sync=False,
        ),
    }


def handle_inventory_level_webhook(
    integration: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    inventory_item_id = str(payload.get("inventory_item_id") or "")
    if not inventory_item_id:
        return {"status": "ignored", "reason": "missing_inventory_item_id"}
    updated = update_inventory_by_inventory_item_id(
        user_id=int(integration["user_id"]),
        inventory_item_id=inventory_item_id,
        available=_int_or_none(payload.get("available")),
    )
    clear_product_cache(integration["shopify_store_domain"])
    return {"status": "updated", "variants_updated": updated}


def start_daily_catalog_sync_scheduler() -> None:
    global _daily_sync_task
    if not settings.shopify_daily_sync_enabled:
        return
    if _daily_sync_task and not _daily_sync_task.done():
        return
    _daily_sync_task = asyncio.create_task(_daily_sync_loop())


async def stop_daily_catalog_sync_scheduler() -> None:
    global _daily_sync_task
    if not _daily_sync_task:
        return
    _daily_sync_task.cancel()
    try:
        await _daily_sync_task
    except asyncio.CancelledError:
        pass
    _daily_sync_task = None


async def sync_all_shopify_catalogs() -> dict[str, Any]:
    integrations = list_shopify_integrations()
    results = []
    for integration in integrations:
        try:
            result = await sync_shopify_catalog_for_integration(integration)
            results.append(
                {
                    "user_id": integration["user_id"],
                    "store_domain": integration["shopify_store_domain"],
                    "status": "ok",
                    "result": result,
                }
            )
        except (ShopifyAPIError, ShopifyOAuthError, ValueError) as exc:
            logger.warning(
                "Daily Shopify catalog sync failed user_id=%s store=%s reason=%s",
                integration.get("user_id"),
                integration.get("shopify_store_domain"),
                exc,
            )
            results.append(
                {
                    "user_id": integration["user_id"],
                    "store_domain": integration["shopify_store_domain"],
                    "status": "failed",
                    "reason": str(exc),
                }
            )
    return {"stores": len(results), "results": results}


async def _daily_sync_loop() -> None:
    while True:
        await asyncio.sleep(settings.shopify_daily_sync_interval_seconds)
        try:
            await sync_all_shopify_catalogs()
        except Exception:  # noqa: BLE001 - scheduler should stay alive
            logger.exception("Daily Shopify catalog sync crashed")


async def _ensure_embeddings_for_image(
    user_id: int,
    product_id: str,
    image_row: dict[str, Any],
    variant_ids: list[str],
) -> dict[str, int]:
    stats = {"created": 0, "skipped": 0, "errors": 0}
    image_url = image_row["image_url"]
    shopify_image_id = image_row.get("shopify_image_id")
    target_variant_ids = variant_ids or [None]

    missing_variant_ids = [
        variant_id
        for variant_id in target_variant_ids
        if not embedding_exists(
            user_id=user_id,
            shopify_product_id=product_id,
            shopify_variant_id=variant_id,
            image_url=image_url,
            embedding_model=settings.gemini_embedding_model,
            embedding_dimension=settings.gemini_embedding_dimensions,
        )
    ]
    if not missing_variant_ids:
        stats["skipped"] += len(target_variant_ids)
        return stats

    try:
        embedding_result = await generate_image_embedding_from_url(image_url)
    except Exception as exc:  # noqa: BLE001 - one bad image should not kill sync
        logger.warning(
            "Product image embedding failed user_id=%s product_id=%s image_url=%s reason=%s",
            user_id,
            product_id,
            image_url,
            exc,
        )
        stats["errors"] += len(missing_variant_ids)
        return stats

    for variant_id in missing_variant_ids:
        upsert_image_embedding(
            user_id=user_id,
            shopify_product_id=product_id,
            shopify_variant_id=variant_id,
            shopify_image_id=shopify_image_id,
            image_url=image_url,
            embedding=embedding_result["embedding"],
            embedding_model=embedding_result["model"],
            metadata={
                "source": "shopify_catalog_sync",
                "image_alt": image_row.get("alt"),
            },
        )
        stats["created"] += 1
    stats["skipped"] += len(target_variant_ids) - len(missing_variant_ids)
    return stats


async def _refresh_shopify_token_if_needed(integration: dict[str, Any]) -> str:
    access_token = integration["shopify_access_token"]
    expires_at = integration.get("shopify_access_token_expires_at")
    if not expires_at or not _is_expiring_soon(str(expires_at)):
        return access_token

    refresh_token = integration.get("shopify_refresh_token")
    if not refresh_token:
        return access_token

    token_response = await refresh_expiring_offline_token(
        shop_domain=integration["shopify_store_domain"],
        refresh_token=refresh_token,
    )
    save_shopify_connection(
        user_id=integration["user_id"],
        store_domain=integration["shopify_store_domain"],
        access_token=token_response["access_token"],
        access_token_expires_in=token_response.get("expires_in"),
        refresh_token=token_response.get("refresh_token", refresh_token),
        refresh_token_expires_in=token_response.get("refresh_token_expires_in"),
    )
    return token_response["access_token"]


def _product_images(product: dict[str, Any]) -> list[dict[str, Any]]:
    images = product.get("images") or []
    if images:
        return images
    image = product.get("image")
    return [image] if isinstance(image, dict) and image.get("src") else []


def _is_expiring_soon(value: str) -> bool:
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (expiry - now).total_seconds() < 300


def _int_or_none(value: Any) -> Optional[int]:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

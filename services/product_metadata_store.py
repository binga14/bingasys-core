from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any, Iterable, Optional

from psycopg2.extras import Json

from database import get_connection


def upsert_shopify_product(
    user_id: int,
    shop_domain: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    shopify_product_id = str(product.get("id") or "")
    if not shopify_product_id:
        raise ValueError("Shopify product payload is missing id")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shopify_products (
                    user_id,
                    shop_domain,
                    shopify_product_id,
                    admin_graphql_api_id,
                    title,
                    handle,
                    status,
                    vendor,
                    product_type,
                    tags,
                    raw_metadata,
                    synced_at,
                    deleted_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NULL)
                ON CONFLICT (user_id, shopify_product_id) DO UPDATE SET
                    shop_domain = EXCLUDED.shop_domain,
                    admin_graphql_api_id = EXCLUDED.admin_graphql_api_id,
                    title = EXCLUDED.title,
                    handle = EXCLUDED.handle,
                    status = EXCLUDED.status,
                    vendor = EXCLUDED.vendor,
                    product_type = EXCLUDED.product_type,
                    tags = EXCLUDED.tags,
                    raw_metadata = EXCLUDED.raw_metadata,
                    synced_at = NOW(),
                    deleted_at = NULL
                RETURNING *
                """,
                (
                    user_id,
                    shop_domain,
                    shopify_product_id,
                    product.get("admin_graphql_api_id"),
                    str(product.get("title") or "Untitled product"),
                    product.get("handle"),
                    product.get("status"),
                    product.get("vendor"),
                    product.get("product_type"),
                    product.get("tags"),
                    Json(_compact_product_metadata(product)),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def upsert_shopify_variant(
    user_id: int,
    shopify_product_id: str,
    variant: dict[str, Any],
) -> dict[str, Any]:
    shopify_variant_id = str(variant.get("id") or "")
    if not shopify_variant_id:
        raise ValueError("Shopify variant payload is missing id")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shopify_product_variants (
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    admin_graphql_api_id,
                    title,
                    sku,
                    price,
                    inventory_item_id,
                    inventory_quantity,
                    inventory_management,
                    inventory_policy,
                    raw_metadata,
                    synced_at,
                    deleted_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NULL)
                ON CONFLICT (user_id, shopify_variant_id) DO UPDATE SET
                    shopify_product_id = EXCLUDED.shopify_product_id,
                    admin_graphql_api_id = EXCLUDED.admin_graphql_api_id,
                    title = EXCLUDED.title,
                    sku = EXCLUDED.sku,
                    price = EXCLUDED.price,
                    inventory_item_id = EXCLUDED.inventory_item_id,
                    inventory_quantity = EXCLUDED.inventory_quantity,
                    inventory_management = EXCLUDED.inventory_management,
                    inventory_policy = EXCLUDED.inventory_policy,
                    raw_metadata = EXCLUDED.raw_metadata,
                    synced_at = NOW(),
                    deleted_at = NULL
                RETURNING *
                """,
                (
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    variant.get("admin_graphql_api_id"),
                    variant.get("title"),
                    variant.get("sku"),
                    _decimal_or_none(variant.get("price")),
                    _str_or_none(variant.get("inventory_item_id")),
                    _int_or_none(variant.get("inventory_quantity")),
                    variant.get("inventory_management"),
                    variant.get("inventory_policy"),
                    Json(_compact_variant_metadata(variant)),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def upsert_shopify_product_image(
    user_id: int,
    shopify_product_id: str,
    image: dict[str, Any],
) -> dict[str, Any]:
    image_url = image.get("src") or image.get("url")
    if not image_url:
        raise ValueError("Shopify product image payload is missing src")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shopify_product_images (
                    user_id,
                    shopify_product_id,
                    shopify_image_id,
                    admin_graphql_api_id,
                    media_id,
                    image_url,
                    position,
                    alt,
                    variant_ids,
                    raw_metadata,
                    synced_at,
                    deleted_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NULL)
                ON CONFLICT (user_id, shopify_product_id, image_url) DO UPDATE SET
                    shopify_image_id = EXCLUDED.shopify_image_id,
                    admin_graphql_api_id = EXCLUDED.admin_graphql_api_id,
                    media_id = EXCLUDED.media_id,
                    position = EXCLUDED.position,
                    alt = EXCLUDED.alt,
                    variant_ids = EXCLUDED.variant_ids,
                    raw_metadata = EXCLUDED.raw_metadata,
                    synced_at = NOW(),
                    deleted_at = NULL
                RETURNING *
                """,
                (
                    user_id,
                    shopify_product_id,
                    _str_or_none(image.get("id")),
                    image.get("admin_graphql_api_id"),
                    _str_or_none(image.get("media_id")),
                    image_url,
                    _int_or_none(image.get("position")),
                    image.get("alt"),
                    Json([str(value) for value in image.get("variant_ids") or []]),
                    Json(_compact_image_metadata(image)),
                ),
            )
            row = cursor.fetchone()
    return dict(row)


def mark_missing_catalog_rows_deleted(
    user_id: int,
    seen_product_ids: Iterable[str],
    seen_variant_ids: Iterable[str],
    seen_image_urls: Iterable[str],
) -> None:
    product_ids = list(seen_product_ids)
    variant_ids = list(seen_variant_ids)
    image_urls = list(seen_image_urls)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE shopify_products
                SET deleted_at = NOW()
                WHERE user_id = %s
                  AND NOT (shopify_product_id = ANY(%s))
                """,
                (user_id, product_ids),
            )
            cursor.execute(
                """
                UPDATE shopify_product_variants
                SET deleted_at = NOW()
                WHERE user_id = %s
                  AND NOT (shopify_variant_id = ANY(%s))
                """,
                (user_id, variant_ids),
            )
            cursor.execute(
                """
                UPDATE shopify_product_images
                SET deleted_at = NOW()
                WHERE user_id = %s
                  AND NOT (image_url = ANY(%s))
                """,
                (user_id, image_urls),
            )
            cursor.execute(
                """
                DELETE FROM product_image_embeddings
                WHERE user_id = %s
                  AND NOT (image_url = ANY(%s))
                """,
                (user_id, image_urls),
            )


def mark_product_deleted(user_id: int, shopify_product_id: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE shopify_products
                SET deleted_at = NOW()
                WHERE user_id = %s AND shopify_product_id = %s
                """,
                (user_id, shopify_product_id),
            )
            cursor.execute(
                """
                UPDATE shopify_product_variants
                SET deleted_at = NOW()
                WHERE user_id = %s AND shopify_product_id = %s
                """,
                (user_id, shopify_product_id),
            )
            cursor.execute(
                """
                UPDATE shopify_product_images
                SET deleted_at = NOW()
                WHERE user_id = %s AND shopify_product_id = %s
                """,
                (user_id, shopify_product_id),
            )
            cursor.execute(
                """
                DELETE FROM product_image_embeddings
                WHERE user_id = %s AND shopify_product_id = %s
                """,
                (user_id, shopify_product_id),
            )


def update_inventory_by_inventory_item_id(
    user_id: int,
    inventory_item_id: str,
    available: Optional[int],
) -> int:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE shopify_product_variants
                SET inventory_quantity = %s,
                    synced_at = NOW()
                WHERE user_id = %s
                  AND inventory_item_id = %s
                  AND deleted_at IS NULL
                """,
                (available, user_id, inventory_item_id),
            )
            return cursor.rowcount


def get_product_summaries_by_ids(
    user_id: int,
    shopify_product_ids: list[str],
) -> list[dict[str, Any]]:
    if not shopify_product_ids:
        return []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM shopify_products
                WHERE user_id = %s
                  AND shopify_product_id = ANY(%s)
                  AND deleted_at IS NULL
                """,
                (user_id, shopify_product_ids),
            )
            products = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT *
                FROM shopify_product_variants
                WHERE user_id = %s
                  AND shopify_product_id = ANY(%s)
                  AND deleted_at IS NULL
                ORDER BY id
                """,
                (user_id, shopify_product_ids),
            )
            variants = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT *
                FROM shopify_product_images
                WHERE user_id = %s
                  AND shopify_product_id = ANY(%s)
                  AND deleted_at IS NULL
                ORDER BY position NULLS LAST, id
                """,
                (user_id, shopify_product_ids),
            )
            images = [dict(row) for row in cursor.fetchall()]

    variants_by_product: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        variants_by_product.setdefault(str(variant["shopify_product_id"]), []).append(variant)

    images_by_product: dict[str, list[dict[str, Any]]] = {}
    for image in images:
        images_by_product.setdefault(str(image["shopify_product_id"]), []).append(image)

    product_by_id = {
        str(product["shopify_product_id"]): _product_summary(
            product,
            variants_by_product.get(str(product["shopify_product_id"]), []),
            images_by_product.get(str(product["shopify_product_id"]), []),
        )
        for product in products
    }
    return [
        product_by_id[product_id]
        for product_id in shopify_product_ids
        if product_id in product_by_id
    ]


def search_product_summaries_by_text(
    user_id: int,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    tokens = _tokens(query)
    if not tokens:
        return []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM shopify_products
                WHERE user_id = %s
                  AND deleted_at IS NULL
                """,
                (user_id,),
            )
            products = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT *
                FROM shopify_product_variants
                WHERE user_id = %s
                  AND deleted_at IS NULL
                """,
                (user_id,),
            )
            variants = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT *
                FROM shopify_product_images
                WHERE user_id = %s
                  AND deleted_at IS NULL
                ORDER BY position NULLS LAST, id
                """,
                (user_id,),
            )
            images = [dict(row) for row in cursor.fetchall()]

    variants_by_product: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        variants_by_product.setdefault(str(variant["shopify_product_id"]), []).append(variant)

    images_by_product: dict[str, list[dict[str, Any]]] = {}
    for image in images:
        images_by_product.setdefault(str(image["shopify_product_id"]), []).append(image)

    scored: list[tuple[float, dict[str, Any]]] = []
    normalized_query = _normalize(query)
    query_tokens = set(tokens)
    for product in products:
        product_id = str(product["shopify_product_id"])
        title_text = " ".join(
            str(part or "")
            for part in [product.get("title"), product.get("handle")]
        )
        variant_text_parts = []
        for variant in variants_by_product.get(product_id, []):
            variant_text_parts.extend([variant.get("title"), variant.get("sku")])
        variant_text = " ".join(str(part or "") for part in variant_text_parts)
        metadata_text = " ".join(
            str(part or "")
            for part in [
                product.get("product_type"),
                product.get("vendor"),
                product.get("tags"),
            ]
        )
        title_tokens = set(_tokens(title_text))
        variant_tokens = set(_tokens(variant_text))
        metadata_tokens = set(_tokens(metadata_text))
        haystack_tokens = title_tokens | variant_tokens | metadata_tokens
        if not haystack_tokens:
            continue

        title_matches = query_tokens & title_tokens
        variant_matches = query_tokens & variant_tokens
        metadata_matches = query_tokens & metadata_tokens
        if not (title_matches or variant_matches or metadata_matches):
            continue

        score = 0.0
        score += len(title_matches) / max(len(query_tokens), 1)
        score += 0.7 * len(variant_matches) / max(len(query_tokens), 1)
        score += 0.2 * len(metadata_matches) / max(len(query_tokens), 1)
        title_normalized = _normalize(str(product.get("title") or ""))
        if title_normalized and title_normalized in normalized_query:
            score += 0.8
        elif normalized_query and normalized_query in title_normalized:
            score += 0.5
        if any(any(char.isdigit() for char in token) for token in title_matches | variant_matches):
            score += 0.15

        scored.append(
            (
                score,
                _product_summary(
                    product,
                    variants_by_product.get(product_id, []),
                    images_by_product.get(product_id, []),
                ),
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, product in scored[:limit]:
        product["text_match_score"] = min(score, 1.0)
        results.append(product)
    return results

def _product_summary(
    product: dict[str, Any],
    variants: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> dict[str, Any]:
    variant_summaries = [_variant_summary(variant) for variant in variants]
    prices = [
        Decimal(str(variant["price"]))
        for variant in variants
        if variant.get("price") not in {None, ""}
    ]
    return {
        "id": product["shopify_product_id"],
        "title": product["title"],
        "handle": product.get("handle"),
        "status": product.get("status"),
        "available": any(variant.get("available") for variant in variant_summaries),
        "price": _price_range(prices),
        "variants": variant_summaries[:8],
        "images": [image["image_url"] for image in images[:3]],
        "source": "catalog_db",
    }


def _variant_summary(variant: dict[str, Any]) -> dict[str, Any]:
    inventory_quantity = variant.get("inventory_quantity")
    tracks_inventory = variant.get("inventory_management") not in {None, ""}
    return {
        "id": variant.get("shopify_variant_id"),
        "title": variant.get("title"),
        "sku": variant.get("sku"),
        "price": _decimal_to_string(variant.get("price")),
        "inventory_item_id": variant.get("inventory_item_id"),
        "available": (
            int(inventory_quantity or 0) > 0
            or variant.get("inventory_policy") == "continue"
            or not tracks_inventory
        ),
    }


def _compact_product_metadata(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "published_at": product.get("published_at"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "options": product.get("options") or [],
    }


def _compact_variant_metadata(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "barcode": variant.get("barcode"),
        "option1": variant.get("option1"),
        "option2": variant.get("option2"),
        "option3": variant.get("option3"),
        "taxable": variant.get("taxable"),
        "created_at": variant.get("created_at"),
        "updated_at": variant.get("updated_at"),
    }


def _compact_image_metadata(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "width": image.get("width"),
        "height": image.get("height"),
        "created_at": image.get("created_at"),
        "updated_at": image.get("updated_at"),
    }


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value in {None, ""}:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    return f"{Decimal(str(value)):.2f}".rstrip("0").rstrip(".")


def _int_or_none(value: Any) -> Optional[int]:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> Optional[str]:
    if value in {None, ""}:
        return None
    return str(value)


def _price_range(prices: list[Decimal]) -> Optional[str]:
    if not prices:
        return None
    minimum = min(prices)
    maximum = max(prices)
    if minimum == maximum:
        return _decimal_to_string(minimum)
    return f"{_decimal_to_string(minimum)} - {_decimal_to_string(maximum)}"


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
            "this",
            "that",
            "product",
            "item",
            "one",
            "photo",
            "picture",
            "image",
            "screenshot",
            "do",
            "you",
            "have",
        }
    ]


def _normalize(value: str) -> str:
    return " ".join(_tokens(value))

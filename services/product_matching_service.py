from __future__ import annotations

from typing import Any

from config import settings
from services.ai_service import GeminiAPIError, identify_product_from_image
from services.image_embedding_service import generate_image_embedding_from_url
from services.product_metadata_store import (
    get_product_summaries_by_ids,
    search_product_summaries_by_text,
)
from services.shopify_service import ShopifyAPIError, fetch_product_summaries_by_ids
from services.vector_store import search_similar_image_embeddings


async def match_buyer_screenshot_to_products(
    merchant_id: int,
    image_url: str,
    shop_domain: str,
    access_token: str,
    buyer_text: str = "",
) -> dict[str, Any]:
    embedding_result = await generate_image_embedding_from_url(image_url)
    matches = search_similar_image_embeddings(
        user_id=merchant_id,
        embedding=embedding_result["embedding"],
        limit=settings.shopify_image_match_top_k * 3,
        embedding_model=embedding_result["model"],
    )
    candidates = _collapse_matches_by_product(matches)[: settings.shopify_image_match_top_k]
    vision_query = ""
    vision_confidence = ""
    text_candidates: list[dict[str, Any]] = []
    if not candidates or candidates[0]["score"] < settings.shopify_image_match_high_threshold:
        try:
            vision_result = await identify_product_from_image(image_url, buyer_text)
            vision_query = _vision_query_text(vision_result)
            vision_confidence = str(vision_result.get("confidence") or "")
        except GeminiAPIError:
            vision_query = ""
            vision_confidence = ""
        if vision_query:
            text_products = search_product_summaries_by_text(
                user_id=merchant_id,
                query=vision_query,
                limit=settings.shopify_image_match_top_k,
            )
            text_candidates = _text_candidates(text_products)

    candidates = _merge_candidates(candidates, text_candidates)[
        : settings.shopify_image_match_top_k
    ]
    product_ids = [candidate["shopify_product_id"] for candidate in candidates]

    if not product_ids:
        return {
            "status": "not_found",
            "source": "image_embedding",
            "query": buyer_text,
            "products": [],
            "matches": [],
            "requires_confirmation": True,
            "reason": "No image embeddings are available for this merchant catalog yet.",
            "embedding_model": embedding_result["model"],
            "vision_query": vision_query,
            "vision_confidence": vision_confidence,
        }

    try:
        live_lookup = await fetch_product_summaries_by_ids(
            store_domain=shop_domain,
            access_token=access_token,
            shopify_product_ids=product_ids,
        )
        products = live_lookup.get("products") or []
    except ShopifyAPIError:
        products = get_product_summaries_by_ids(merchant_id, product_ids)

    products = _attach_match_scores(products, candidates)
    top_score = candidates[0]["score"] if candidates else 0.0
    requires_confirmation = top_score < settings.shopify_image_match_high_threshold
    status = "found" if products else "not_found"

    return {
        "status": status,
        "source": "image_embedding",
        "query": buyer_text,
        "products": products,
        "matches": candidates,
        "top_score": top_score,
        "embedding_model": embedding_result["model"],
        "vision_query": vision_query,
        "vision_confidence": vision_confidence,
        "requires_confirmation": requires_confirmation,
        "confidence": _confidence_for_score(top_score),
    }


def _collapse_matches_by_product(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_product: dict[str, dict[str, Any]] = {}
    for match in matches:
        product_id = str(match.get("shopify_product_id") or "")
        if not product_id:
            continue
        current = best_by_product.get(product_id)
        if not current or float(match.get("score") or 0.0) > float(current.get("score") or 0.0):
            best_by_product[product_id] = {
                "shopify_product_id": product_id,
                "shopify_variant_id": match.get("shopify_variant_id"),
                "shopify_image_id": match.get("shopify_image_id"),
                "image_url": match.get("image_url"),
                "score": float(match.get("score") or 0.0),
                "source": "image_embedding",
                "metadata": match.get("metadata") or {},
            }
    collapsed = list(best_by_product.values())
    collapsed.sort(key=lambda item: item["score"], reverse=True)
    return collapsed


def _text_candidates(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for product in products:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        score = float(product.get("text_match_score") or 0.0)
        # OCR/title matches are useful, but keep them slightly below a perfect
        # visual match so exact image matches still win.
        score = min(0.9, max(0.0, score))
        candidates.append(
            {
                "shopify_product_id": product_id,
                "shopify_variant_id": None,
                "shopify_image_id": None,
                "image_url": (product.get("images") or [None])[0],
                "score": score,
                "source": "vision_text_catalog_search",
                "metadata": {"title": product.get("title")},
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _merge_candidates(
    vector_candidates: list[dict[str, Any]],
    text_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidate in [*vector_candidates, *text_candidates]:
        product_id = candidate["shopify_product_id"]
        current = merged.get(product_id)
        if not current or candidate["score"] > current["score"]:
            merged[product_id] = candidate
    results = list(merged.values())
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def _attach_match_scores(
    products: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_product_id = {candidate["shopify_product_id"]: candidate for candidate in candidates}
    enriched = []
    for product in products:
        product_id = str(product.get("id") or "")
        candidate = by_product_id.get(product_id)
        if not candidate:
            enriched.append(product)
            continue
        product = dict(product)
        product["match_score"] = candidate["score"]
        product["match_source"] = candidate.get("source")
        product["matched_image_url"] = candidate.get("image_url")
        product["matched_variant_id"] = candidate.get("shopify_variant_id")
        enriched.append(product)
    enriched.sort(key=lambda item: float(item.get("match_score") or 0.0), reverse=True)
    return enriched


def _confidence_for_score(score: float) -> str:
    if score >= settings.shopify_image_match_high_threshold:
        return "high"
    if score >= settings.shopify_image_match_medium_threshold:
        return "medium"
    return "low"


def _vision_query_text(vision_result: dict[str, Any]) -> str:
    vision = vision_result.get("vision") if isinstance(vision_result.get("vision"), dict) else {}
    readable_text = str(vision.get("readable_text") or "").strip()
    if readable_text:
        return readable_text

    parts = [
        vision.get("search_query"),
        vision_result.get("text"),
    ]
    primary = " ".join(str(part).strip() for part in parts if part)
    if primary.strip():
        return primary

    fallback_parts = [vision.get("product_type")]
    visual_features = vision.get("visual_features")
    if isinstance(visual_features, list):
        fallback_parts.extend(str(feature) for feature in visual_features)
    elif isinstance(visual_features, str):
        fallback_parts.append(visual_features)
    colors = vision.get("colors")
    if isinstance(colors, list):
        fallback_parts.extend(str(color) for color in colors)
    elif isinstance(colors, str):
        fallback_parts.append(colors)
    return " ".join(str(part).strip() for part in fallback_parts if part)

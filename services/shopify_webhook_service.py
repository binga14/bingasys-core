from __future__ import annotations

import json
import logging
from typing import Any

from database import find_integration_by_shop_domain
from services.shopify_catalog_sync import (
    handle_inventory_level_webhook,
    handle_product_webhook,
)
from services.shopify_service import verify_webhook_hmac

logger = logging.getLogger(__name__)


async def handle_shopify_webhook(
    body: bytes,
    hmac_header: str | None,
    topic: str | None,
    shop_domain: str | None,
) -> dict[str, Any]:
    if not verify_webhook_hmac(body, hmac_header):
        return {"status": "failed", "reason": "invalid_hmac"}
    if not topic:
        return {"status": "failed", "reason": "missing_topic"}
    if not shop_domain:
        return {"status": "failed", "reason": "missing_shop_domain"}

    integration = find_integration_by_shop_domain(shop_domain)
    if not integration:
        return {"status": "ignored", "reason": "shop_not_connected"}

    try:
        payload = json.loads(body)
    except ValueError:
        return {"status": "failed", "reason": "invalid_json"}

    if topic in {"products/create", "products/update", "products/delete"}:
        result = await handle_product_webhook(integration, topic, payload)
    elif topic == "inventory_levels/update":
        result = handle_inventory_level_webhook(integration, payload)
    elif topic == "inventory_items/update":
        # This topic can tell us item metadata changed, but it does not include
        # availability. Product and inventory-level webhooks handle searchable
        # metadata and availability, so acknowledge without inventing state.
        result = {"status": "accepted", "reason": "inventory_item_metadata_update"}
    else:
        result = {"status": "ignored", "reason": f"unsupported_topic:{topic}"}

    logger.info(
        "Shopify webhook processed shop=%s topic=%s status=%s",
        shop_domain,
        topic,
        result.get("status"),
    )
    return result

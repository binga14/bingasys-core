async def get_inventory_placeholder(store_domain: str, access_token: str) -> dict:
    return {
        "store_domain": store_domain,
        "status": "not_implemented",
        "message": "Shopify inventory will be fetched live from Shopify APIs later.",
    }

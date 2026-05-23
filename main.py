from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from config import settings
from database import (
    get_integration_settings,
    init_db,
    save_meta_connection,
    save_shopify_connection,
)
from meta_service import handle_webhook_placeholder, verify_webhook
from schemas import (
    MetaConnectionIn,
    MetaConnectionOut,
    ShopifyConnectionIn,
    ShopifyConnectionOut,
)

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/shopify/connection", response_model=ShopifyConnectionOut)
def upsert_shopify_connection(payload: ShopifyConnectionIn) -> dict[str, Any]:
    row = save_shopify_connection(
        store_domain=payload.store_domain,
        access_token=payload.access_token,
    )
    return {
        "store_domain": row.get("shopify_store_domain"),
        "access_token": row.get("shopify_access_token"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/shopify/connection", response_model=ShopifyConnectionOut)
def read_shopify_connection() -> dict[str, Any]:
    row = get_integration_settings()
    if not row or not row.get("shopify_store_domain"):
        raise HTTPException(status_code=404, detail="Shopify connection not configured")

    return {
        "store_domain": row.get("shopify_store_domain"),
        "access_token": row.get("shopify_access_token"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@app.put("/meta/connection", response_model=MetaConnectionOut)
def upsert_meta_connection(payload: MetaConnectionIn) -> dict[str, Any]:
    row = save_meta_connection(
        page_id=payload.page_id,
        access_token=payload.access_token,
        instagram_business_account_id=payload.instagram_business_account_id,
        webhook_verify_token=payload.webhook_verify_token,
    )
    return {
        "page_id": row.get("meta_page_id"),
        "access_token": row.get("meta_access_token"),
        "instagram_business_account_id": row.get("instagram_business_account_id"),
        "webhook_verify_token": row.get("webhook_verify_token"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/meta/connection", response_model=MetaConnectionOut)
def read_meta_connection() -> dict[str, Any]:
    row = get_integration_settings()
    if not row or not row.get("meta_page_id"):
        raise HTTPException(status_code=404, detail="Meta connection not configured")

    return {
        "page_id": row.get("meta_page_id"),
        "access_token": row.get("meta_access_token"),
        "instagram_business_account_id": row.get("instagram_business_account_id"),
        "webhook_verify_token": row.get("webhook_verify_token"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/meta/webhook")
def verify_meta_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    row = get_integration_settings()
    expected_token = row.get("webhook_verify_token") if row else None

    if verify_webhook(hub_mode, hub_verify_token, expected_token) and hub_challenge:
        return PlainTextResponse(hub_challenge)

    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/meta/webhook")
async def receive_meta_webhook(request: Request) -> dict[str, str]:
    payload = await request.json()
    return await handle_webhook_placeholder(payload)

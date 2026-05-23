from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ShopifyConnectionIn(BaseModel):
    store_domain: str = Field(..., examples=["example.myshopify.com"])
    access_token: str


class ShopifyConnectionOut(BaseModel):
    store_domain: Optional[str] = None
    access_token: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MetaConnectionIn(BaseModel):
    page_id: str
    access_token: str
    webhook_verify_token: str
    instagram_business_account_id: Optional[str] = None


class MetaConnectionOut(BaseModel):
    page_id: Optional[str] = None
    access_token: Optional[str] = None
    instagram_business_account_id: Optional[str] = None
    webhook_verify_token: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

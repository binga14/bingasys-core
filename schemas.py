from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: int
    email: str
    created_at: Optional[str] = None


class SignupIn(BaseModel):
    email: str = Field(..., examples=["owner@example.com"])
    password: str = Field(..., min_length=8)


class LoginIn(BaseModel):
    email: str = Field(..., examples=["owner@example.com"])
    password: str


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ForgotPasswordIn(BaseModel):
    email: str = Field(..., examples=["owner@example.com"])


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(..., min_length=8)


class MessageOut(BaseModel):
    message: str


class ShopifyOAuthStartIn(BaseModel):
    store_domain: str = Field(..., examples=["example.myshopify.com"])


class ShopifyOAuthStartOut(BaseModel):
    authorization_url: str


class ShopifyConnectionOut(BaseModel):
    connected: bool = False
    store_domain: Optional[str] = None
    access_token_last4: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MetaConnectionIn(BaseModel):
    page_id: str
    access_token: str
    webhook_verify_token: str
    instagram_business_account_id: Optional[str] = None


class MetaConnectionOut(BaseModel):
    connected: bool = False
    page_id: Optional[str] = None
    access_token_last4: Optional[str] = None
    instagram_business_account_id: Optional[str] = None
    webhook_verify_token: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OnboardingStatusOut(BaseModel):
    shopify_connected: bool
    meta_connected: bool
    ready: bool

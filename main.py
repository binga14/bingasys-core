from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from services.auth_service import (
    create_access_token,
    create_password_reset_token,
    decode_access_token,
    hash_password,
    hash_reset_token,
    is_expired,
    normalize_email,
    password_reset_expires_at,
    validate_email,
    validate_password,
    verify_password,
)
from config import settings
from database import (
    consume_password_reset_token,
    create_user,
    find_integration_by_webhook_verify_token,
    get_integration_settings,
    get_password_reset_token,
    get_user_by_email,
    get_user_by_id,
    has_value,
    init_db,
    save_meta_connection,
    save_password_reset_token,
    save_shopify_connection,
    to_iso,
    update_user_password,
)
from services.email_service import build_password_reset_link, send_password_reset_email
from services.meta_service import handle_webhook_placeholder, verify_webhook
from schemas import (
    AuthOut,
    ForgotPasswordIn,
    LoginIn,
    MessageOut,
    MetaConnectionIn,
    MetaConnectionOut,
    OnboardingStatusOut,
    ResetPasswordIn,
    ShopifyConnectionOut,
    ShopifyOAuthStartIn,
    ShopifyOAuthStartOut,
    SignupIn,
    UserOut,
)
from services.shopify_service import (
    ShopifyOAuthError,
    build_authorization_url,
    decode_oauth_state,
    exchange_code_for_access_token,
    normalize_shop_domain,
    verify_callback_hmac,
)

app = FastAPI(title=settings.app_name)
security = HTTPBearer(auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict[str, Any]:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@app.post("/api/auth/signup", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn) -> dict[str, Any]:
    email = normalize_email(payload.email)
    if not validate_email(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    if not validate_password(payload.password):
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    user = create_user(email=email, password_hash=hash_password(payload.password))
    if not user:
        raise HTTPException(status_code=409, detail="An account already exists for this email")

    return _auth_response(user)


@app.post("/api/auth/login", response_model=AuthOut)
def login(payload: LoginIn) -> dict[str, Any]:
    user = get_user_by_email(normalize_email(payload.email))
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return _auth_response(user)


@app.get("/api/auth/me", response_model=UserOut)
def me(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return _user_response(current_user)


@app.post("/api/auth/forgot-password", response_model=MessageOut)
def forgot_password(payload: ForgotPasswordIn) -> dict[str, str]:
    user = get_user_by_email(normalize_email(payload.email))
    if user:
        token = create_password_reset_token()
        save_password_reset_token(
            user_id=user["id"],
            token_hash=hash_reset_token(token),
            expires_at=password_reset_expires_at(),
        )
        send_password_reset_email(user["email"], build_password_reset_link(token))

    return {"message": "If that email exists, a reset link has been sent."}


@app.post("/api/auth/reset-password", response_model=MessageOut)
def reset_password(payload: ResetPasswordIn) -> dict[str, str]:
    if not validate_password(payload.password):
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    reset_token = get_password_reset_token(hash_reset_token(payload.token))
    if (
        not reset_token
        or reset_token.get("used_at") is not None
        or is_expired(reset_token["expires_at"])
    ):
        raise HTTPException(status_code=400, detail="Reset link is invalid or expired")

    update_user_password(reset_token["user_id"], hash_password(payload.password))
    consume_password_reset_token(reset_token["id"])
    return {"message": "Password updated. You can now sign in."}


@app.get("/api/onboarding/status", response_model=OnboardingStatusOut)
def onboarding_status(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, bool]:
    row = get_integration_settings(current_user["id"])
    shopify_connected = bool(row and has_value(row.get("shopify_store_domain")))
    meta_connected = bool(row and has_value(row.get("meta_page_id")))
    return {
        "shopify_connected": shopify_connected,
        "meta_connected": meta_connected,
        "ready": shopify_connected and meta_connected,
    }


@app.post(
    "/api/integrations/shopify/oauth/start",
    response_model=ShopifyOAuthStartOut,
)
def start_shopify_oauth(
    payload: ShopifyOAuthStartIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        authorization_url = build_authorization_url(
            shop_domain=payload.store_domain,
            user_id=current_user["id"],
        )
    except ShopifyOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"authorization_url": authorization_url}


@app.get("/api/integrations/shopify/oauth/callback")
async def complete_shopify_oauth(request: Request) -> RedirectResponse:
    query = request.query_params
    shop = query.get("shop", "")
    code = query.get("code", "")
    state = query.get("state", "")

    try:
        if not verify_callback_hmac(list(query.multi_items())):
            raise ShopifyOAuthError("Shopify authorization could not be verified")

        shop_domain = normalize_shop_domain(shop)
        state_payload = decode_oauth_state(state)
        if state_payload.get("shop") != shop_domain:
            raise ShopifyOAuthError("Shopify authorization state does not match the store")
        if not code:
            raise ShopifyOAuthError("Shopify did not return an authorization code")

        token_response = await exchange_code_for_access_token(shop_domain, code)
        save_shopify_connection(
            user_id=int(state_payload["user_id"]),
            store_domain=shop_domain,
            access_token=token_response["access_token"],
            access_token_expires_in=token_response.get("expires_in"),
            refresh_token=token_response.get("refresh_token"),
            refresh_token_expires_in=token_response.get("refresh_token_expires_in"),
        )
    except (KeyError, TypeError, ValueError, ShopifyOAuthError) as exc:
        return RedirectResponse(_frontend_redirect({"shopify_error": str(exc)}))

    return RedirectResponse(_frontend_redirect({"shopify": "connected", "shop": shop_domain}))


@app.get("/api/integrations/shopify", response_model=ShopifyConnectionOut)
def read_shopify_connection(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    row = get_integration_settings(current_user["id"])
    return _shopify_response(row)


@app.put("/api/integrations/meta", response_model=MetaConnectionOut)
def upsert_meta_connection(
    payload: MetaConnectionIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    row = save_meta_connection(
        user_id=current_user["id"],
        page_id=payload.page_id.strip(),
        access_token=payload.access_token.strip(),
        instagram_business_account_id=(
            payload.instagram_business_account_id.strip()
            if payload.instagram_business_account_id
            else None
        ),
        webhook_verify_token=payload.webhook_verify_token.strip(),
    )
    return _meta_response(row)


@app.get("/api/integrations/meta", response_model=MetaConnectionOut)
def read_meta_connection(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    row = get_integration_settings(current_user["id"])
    return _meta_response(row)


@app.get("/meta/webhook")
def verify_meta_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    row = (
        find_integration_by_webhook_verify_token(hub_verify_token)
        if hub_verify_token
        else None
    )
    expected_token = row.get("webhook_verify_token") if row else None

    if verify_webhook(hub_mode, hub_verify_token, expected_token) and hub_challenge:
        return PlainTextResponse(hub_challenge)

    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/meta/webhook")
async def receive_meta_webhook(request: Request) -> dict[str, str]:
    payload = await request.json()
    return await handle_webhook_placeholder(payload)


def _auth_response(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": _user_response(user),
    }


def _user_response(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": to_iso(user.get("created_at")),
    }


def _secret_last4(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[-4:]


def _shopify_response(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    store_domain = row.get("shopify_store_domain") if row else None
    access_token = row.get("shopify_access_token") if row else None
    return {
        "connected": has_value(store_domain),
        "store_domain": store_domain,
        "access_token_last4": _secret_last4(access_token),
        "created_at": to_iso(row.get("created_at")) if row else None,
        "updated_at": to_iso(row.get("updated_at")) if row else None,
    }


def _meta_response(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    page_id = row.get("meta_page_id") if row else None
    access_token = row.get("meta_access_token") if row else None
    return {
        "connected": has_value(page_id),
        "page_id": page_id,
        "access_token_last4": _secret_last4(access_token),
        "instagram_business_account_id": (
            row.get("instagram_business_account_id") if row else None
        ),
        "webhook_verify_token": row.get("webhook_verify_token") if row else None,
        "created_at": to_iso(row.get("created_at")) if row else None,
        "updated_at": to_iso(row.get("updated_at")) if row else None,
    }


def _frontend_redirect(params: dict[str, str]) -> str:
    separator = "&" if "?" in settings.frontend_url else "?"
    return f"{settings.frontend_url}{separator}{urlencode(params)}"

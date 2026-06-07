from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from config import settings


class MetaOAuthError(ValueError):
    pass


class MetaWebhookError(RuntimeError):
    pass


class MetaSendMessageError(RuntimeError):
    pass


def ensure_oauth_configured() -> None:
    missing = []
    if not settings.meta_app_id:
        missing.append("META_APP_ID")
    if not settings.meta_app_secret:
        missing.append("META_APP_SECRET")
    if missing:
        raise MetaOAuthError("Meta OAuth is missing backend config: " + ", ".join(missing))


def build_authorization_url(user_id: int) -> str:
    ensure_oauth_configured()
    state = create_oauth_state(user_id)
    query = urlencode(
        {
            "client_id": settings.meta_app_id,
            "redirect_uri": settings.meta_redirect_uri,
            "state": state,
            "scope": settings.meta_scopes,
        }
    )
    return f"https://www.facebook.com/{settings.meta_graph_api_version}/dialog/oauth?{query}"


def create_oauth_state(user_id: int) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.meta_oauth_state_expire_minutes
    )
    payload = {"user_id": user_id, "exp": int(expires_at.timestamp())}
    encoded_payload = _b64encode_json(payload)
    signature = _sign(encoded_payload)
    return f"{encoded_payload}.{signature}"


def decode_oauth_state(state: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = state.split(".", 1)
    except ValueError as exc:
        raise MetaOAuthError("Invalid Meta authorization state") from exc

    expected_signature = _sign(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise MetaOAuthError("Invalid Meta authorization state")

    payload = json.loads(_b64decode(encoded_payload))
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise MetaOAuthError("Meta authorization expired")
    return payload


async def exchange_code_for_user_token(code: str) -> dict[str, Any]:
    ensure_oauth_configured()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            _graph_url("oauth/access_token"),
            params={
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "redirect_uri": settings.meta_redirect_uri,
                "code": code,
            },
        )

    if response.status_code >= 400:
        raise MetaOAuthError("Meta did not return an access token")

    data = response.json()
    if not data.get("access_token"):
        raise MetaOAuthError("Meta did not return an access token")

    return await exchange_for_long_lived_token(data)


async def exchange_for_long_lived_token(token_response: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            _graph_url("oauth/access_token"),
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "fb_exchange_token": token_response["access_token"],
            },
        )

    if response.status_code >= 400:
        return token_response

    data = response.json()
    if not data.get("access_token"):
        return token_response
    return data


async def fetch_pages(user_access_token: str) -> list[dict[str, Any]]:
    fields = "id,name,access_token,tasks,instagram_business_account{id,username,name}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            _graph_url("me/accounts"),
            params={
                "fields": fields,
                "access_token": user_access_token,
            },
        )

        if response.status_code >= 400:
            response = await client.get(
                _graph_url("me/accounts"),
                params={
                    "fields": "id,name,access_token,tasks",
                    "access_token": user_access_token,
                },
            )

    if response.status_code >= 400:
        raise MetaOAuthError("Could not fetch Meta Pages for this account")

    pages = response.json().get("data", [])
    return [_normalize_page(page) for page in pages]


async def subscribe_page_webhooks(page_id: str, page_access_token: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            _graph_url(f"{page_id}/subscribed_apps"),
            params={
                "subscribed_fields": (
                    "messages,messaging_postbacks,message_echoes,"
                    "message_reads,message_deliveries"
                ),
                "access_token": page_access_token,
            },
        )

    if response.status_code >= 400:
        raise MetaOAuthError("Could not subscribe the selected Page to Meta webhooks")


async def subscribe_app_page_webhooks(callback_url: str, verify_token: str) -> None:
    ensure_oauth_configured()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            _graph_url(f"{settings.meta_app_id}/subscriptions"),
            data={
                "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}",
                "object": "page",
                "callback_url": callback_url,
                "verify_token": verify_token,
                "fields": "messages",
            },
        )

    if response.status_code >= 400:
        raise MetaOAuthError("Could not configure Meta app Page webhooks")


def create_webhook_verify_token() -> str:
    return secrets.token_urlsafe(32)


def verify_webhook(
    mode: Optional[str],
    verify_token: Optional[str],
    expected_token: Optional[str],
) -> bool:
    return mode == "subscribe" and verify_token is not None and verify_token == expected_token


def verify_webhook_signature(body: bytes, signature: Optional[str]) -> bool:
    if not settings.meta_app_secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.meta_app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def extract_messenger_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("object") != "page":
        return []

    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id") or "")
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            sender_id = str((event.get("sender") or {}).get("id") or "")
            recipient_id = str((event.get("recipient") or {}).get("id") or page_id)
            message_id = str(message.get("mid") or "")
            text = message.get("text")

            if not sender_id or not recipient_id or message.get("is_echo"):
                continue

            image_urls = _extract_image_attachment_urls(message)
            text_value = text.strip() if isinstance(text, str) else ""
            if not text_value and not image_urls:
                continue

            messages.append(
                {
                    "page_id": page_id or recipient_id,
                    "recipient_id": recipient_id,
                    "sender_id": sender_id,
                    "message_id": message_id,
                    "text": text_value,
                    "image_urls": image_urls,
                }
            )

    return messages


def extract_messenger_text_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "page_id": message["page_id"],
            "recipient_id": message["recipient_id"],
            "sender_id": message["sender_id"],
            "message_id": message["message_id"],
            "text": message["text"],
        }
        for message in extract_messenger_messages(payload)
        if message.get("text")
    ]


def _extract_image_attachment_urls(message: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for attachment in message.get("attachments") or []:
        if attachment.get("type") != "image":
            continue
        payload = attachment.get("payload") or {}
        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    return urls


async def send_messenger_text_reply(
    page_id: str,
    page_access_token: str,
    recipient_id: str,
    text: str,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            _graph_url(f"{page_id}/messages"),
            params={"access_token": page_access_token},
            json={
                "recipient": {"id": recipient_id},
                "messaging_type": "RESPONSE",
                "message": {"text": _messenger_safe_text(text)},
            },
        )

    if response.status_code >= 400:
        raise MetaSendMessageError(_format_meta_error(response))
    return response.json()


def _messenger_safe_text(text: str) -> str:
    clean = text.strip()
    if not clean:
        return "Thanks for your message. How can I help you today?"
    return clean[:2000]


def _normalize_page(page: dict[str, Any]) -> dict[str, Any]:
    instagram = page.get("instagram_business_account") or {}
    return {
        "id": str(page.get("id", "")),
        "name": page.get("name") or "Facebook Page",
        "access_token": page.get("access_token"),
        "tasks": page.get("tasks") or [],
        "instagram_business_account": (
            {
                "id": str(instagram.get("id", "")),
                "username": instagram.get("username") or instagram.get("name"),
            }
            if instagram.get("id")
            else None
        ),
    }


def _graph_url(path: str) -> str:
    clean_path = path.lstrip("/")
    return f"https://graph.facebook.com/{settings.meta_graph_api_version}/{clean_path}"


def _format_meta_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"Meta API returned {response.status_code}: {detail}"


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

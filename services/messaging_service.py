from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from database import find_integration_by_meta_page_id, has_value, save_shopify_connection
from services.ai_service import (
    GeminiAPIError,
    GeminiConfigurationError,
    choose_product_from_image_candidates,
    generate_sales_reply,
    identify_product_from_image,
)
from services.meta_service import (
    MetaSendMessageError,
    MetaWebhookError,
    extract_messenger_messages,
    fetch_message_image_attachment_urls,
    send_messenger_text_reply,
)
from services.shopify_service import (
    ShopifyAPIError,
    ShopifyOAuthError,
    create_order,
    list_product_summaries,
    refresh_expiring_offline_token,
    search_product_summaries,
)


DEDUP_WINDOW_SECONDS = 60 * 10
RECENT_RESULT_LIMIT = 20
CONTEXT_WINDOW_SECONDS = 60 * 30
MAX_CONTEXT_MESSAGES = 8
ORDER_STATE_WINDOW_SECONDS = 60 * 30
# How long to wait for more events from the same buyer before replying. Facebook
# often delivers a caption and its image as separate webhook events; merging them
# avoids sending two replies to one logical message.
MESSAGE_DEBOUNCE_SECONDS = float(settings.meta_message_debounce_seconds)
_processed_message_ids: dict[str, float] = {}
_recent_webhook_results: list[dict[str, Any]] = []
_conversation_contexts: dict[str, dict[str, Any]] = {}
_order_states: dict[str, dict[str, Any]] = {}
# Per-buyer debounce buffers: context_key -> {"events": [...], "task": Task, "deadline": float}
_pending_message_buffers: dict[str, dict[str, Any]] = {}
logger = logging.getLogger(__name__)


async def handle_meta_message_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Buffer incoming events per buyer and reply once after a short quiet window.

    Facebook frequently delivers a caption and its image as separate webhook
    events. We acknowledge each webhook immediately and defer the actual reply so
    that events arriving close together are merged into one logical message.
    """
    if settings.meta_webhook_debug:
        logger.warning("Meta webhook raw summary: %s", _raw_webhook_summary(payload))
    incoming_messages = extract_messenger_messages(payload)
    buffered = 0
    ignored = 0
    for incoming in incoming_messages:
        message_id = incoming.get("message_id", "")
        if message_id and _already_processed(message_id):
            ignored += 1
            continue
        # Reserve the id now so a Meta webhook retry does not double-buffer it.
        if message_id:
            _mark_processed(message_id)
        _buffer_incoming(incoming)
        buffered += 1

    return {
        "status": "accepted",
        "received_messages": len(incoming_messages),
        "buffered": buffered,
        "ignored": ignored,
    }


def _buffer_incoming(incoming: dict[str, Any]) -> None:
    context_key = _context_key(incoming)
    buffer = _pending_message_buffers.setdefault(context_key, {"events": [], "task": None})
    buffer["events"].append(incoming)
    buffer["deadline"] = time.monotonic() + MESSAGE_DEBOUNCE_SECONDS

    task = buffer.get("task")
    if task is None or task.done():
        try:
            buffer["task"] = asyncio.create_task(_flush_after_debounce(context_key))
        except RuntimeError:
            # No running loop (e.g. called outside async context in a test). Fall
            # back to processing the buffered events immediately is not possible
            # here, so leave them for an explicit flush.
            buffer["task"] = None


async def _flush_after_debounce(context_key: str) -> None:
    # Sleep until the buffer has been quiet for the full debounce window. Each new
    # event pushes the deadline out, so we re-check rather than sleeping once.
    while True:
        buffer = _pending_message_buffers.get(context_key)
        if not buffer:
            return
        remaining = buffer.get("deadline", 0) - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(remaining)

    buffer = _pending_message_buffers.pop(context_key, None)
    if not buffer or not buffer.get("events"):
        return
    merged = _merge_incoming_group(buffer["events"])
    merged_ids = merged.get("merged_message_ids") or []
    try:
        result = await _process_incoming_message(merged)
        if result.get("status") == "failed":
            _unmark_processed(merged_ids)
    except Exception:  # noqa: BLE001 - background task must not crash silently
        _unmark_processed(merged_ids)
        logger.exception(
            "Buffered Meta message processing failed page_id=%s sender_id=%s",
            merged.get("page_id", ""),
            merged.get("sender_id", ""),
        )


def _merge_incoming_group(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine buffered events from one buyer into a single logical message."""
    base = dict(events[-1])
    texts: list[str] = []
    image_urls: list[str] = []
    message_ids: list[str] = []
    attachment_count = 0
    attachment_types: list[str] = []
    for event in events:
        text = (event.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
        for url in event.get("image_urls") or []:
            if url not in image_urls:
                image_urls.append(url)
        attachment_count += int(event.get("attachment_count") or 0)
        for attachment_type in event.get("attachment_types") or []:
            if attachment_type not in attachment_types:
                attachment_types.append(attachment_type)
        mid = event.get("message_id") or ""
        if mid:
            message_ids.append(mid)
    base["text"] = "\n".join(texts)
    base["image_urls"] = image_urls
    base["attachment_count"] = attachment_count
    base["attachment_types"] = attachment_types
    base["message_id"] = message_ids[-1] if message_ids else base.get("message_id", "")
    base["merged_message_ids"] = message_ids
    return base


async def flush_pending_messages_now() -> None:
    """Flush all buffered buyers immediately. Used by tests and shutdown."""
    for context_key in list(_pending_message_buffers.keys()):
        buffer = _pending_message_buffers.pop(context_key, None)
        if not buffer or not buffer.get("events"):
            continue
        merged = _merge_incoming_group(buffer["events"])
        await _process_incoming_message(merged)


async def _process_incoming_message(incoming: dict[str, Any]) -> dict[str, Any]:
    """Run the full lookup + reply pipeline for one (possibly merged) message."""
    message_id = incoming.get("message_id", "")
    integration = find_integration_by_meta_page_id(incoming["page_id"])
    page_access_token = integration.get("meta_access_token") if integration else None
    if not integration or not has_value(page_access_token):
        result = _result(incoming, status="failed", reason="page_not_connected")
        _record_single_result(result)
        return result

    context_key = _context_key(incoming)
    incoming_text = ""
    try:
        context_messages = _get_context_messages(context_key)
        image_urls = incoming.get("image_urls") or []
        if (
            not image_urls
            and message_id
            and _should_fetch_meta_attachments(incoming, context_key)
        ):
            fetched_image_urls = await _fetch_missing_image_urls_from_meta(
                message_id=message_id,
                page_access_token=page_access_token,
            )
            if fetched_image_urls:
                image_urls = fetched_image_urls
                incoming = dict(incoming)
                incoming["image_urls"] = image_urls
        if image_urls:
            _remember_recent_image_urls(context_key, image_urls)
        elif _refers_to_recent_image_request(incoming.get("text", "")) or (
            _is_awaiting_image_clarification(context_key)
            and not _is_order_intent(incoming.get("text", ""))
        ):
            # The buyer is answering a product clarification question we asked
            # about a recent photo. Re-run the lookup against that image plus
            # their new details.
            image_urls = _get_recent_image_urls(context_key)
            if image_urls:
                incoming = dict(incoming)
                incoming["image_urls"] = image_urls

        if not image_urls and _requires_image_attachment_reply(incoming, context_key):
            incoming_text = incoming.get("text", "")
            missing_image_reply = _missing_image_attachment_reply(incoming, None)
            await _send_and_record(
                incoming,
                page_access_token,
                context_key,
                incoming_text,
                missing_image_reply,
            )
            return _logged_result(incoming, status="replied")

        product_lookup = await _maybe_lookup_products(
            integration=integration,
            buyer_text=incoming["text"],
            context_key=context_key,
            image_urls=image_urls,
        )
        incoming_text = _incoming_text_for_context(incoming, product_lookup)
        order_reply = await _maybe_handle_order_flow(
            context_key=context_key,
            integration=integration,
            incoming_text=incoming_text,
            context_messages=context_messages,
            product_lookup=product_lookup,
        )
        if order_reply:
            await _send_and_record(
                incoming, page_access_token, context_key, incoming_text, order_reply
            )
            return _logged_result(incoming, status="replied")

        clarification_reply = _image_clarification_reply(product_lookup)
        if clarification_reply:
            await _send_and_record(
                incoming, page_access_token, context_key, incoming_text, clarification_reply
            )
            return _logged_result(incoming, status="replied", product_lookup=product_lookup)

        image_uncertain_reply = _image_lookup_uncertain_reply(product_lookup)
        if image_uncertain_reply:
            await _send_and_record(
                incoming, page_access_token, context_key, incoming_text, image_uncertain_reply
            )
            return _logged_result(incoming, status="replied", product_lookup=product_lookup)

        missing_image_reply = _missing_image_attachment_reply(incoming, product_lookup)
        if missing_image_reply:
            await _send_and_record(
                incoming, page_access_token, context_key, incoming_text, missing_image_reply
            )
            return _logged_result(incoming, status="replied", product_lookup=product_lookup)

        gemini_messages = [
            {"role": "system", "content": _merchant_context(integration)},
            *context_messages,
            {"role": "user", "content": incoming_text},
        ]
        if product_lookup:
            gemini_messages.append(
                {"role": "system", "content": _product_lookup_context(product_lookup)}
            )
        reply = await generate_sales_reply(gemini_messages)
        reply_text = _sanitize_llm_reply(reply.get("text") or _fallback_reply())
        await _send_and_record(
            incoming, page_access_token, context_key, incoming_text, reply_text
        )
        return _logged_result(incoming, status="replied", product_lookup=product_lookup)
    except (
        GeminiAPIError,
        GeminiConfigurationError,
        MetaSendMessageError,
        ShopifyOAuthError,
        ValueError,
    ) as exc:
        logger.warning(
            "Meta message handling failed page_id=%s message_id=%s reason=%s",
            incoming.get("page_id", ""),
            message_id,
            str(exc),
        )
        return _logged_result(incoming, status="failed", reason=str(exc))
    except ShopifyAPIError as exc:
        logger.warning(
            "Meta message handling failed page_id=%s message_id=%s reason=%s",
            incoming.get("page_id", ""),
            message_id,
            str(exc),
        )
        if _is_shopify_phone_error(str(exc)):
            _forget_order_phone(context_key)
            error_reply = (
                "Shopify could not accept that phone number. Please send it with "
                "the country code, for example +8801766813937."
            )
            await _send_and_record(
                incoming, page_access_token, context_key, incoming_text, error_reply
            )
        return _logged_result(incoming, status="failed", reason=str(exc))


async def _send_and_record(
    incoming: dict[str, Any],
    page_access_token: str,
    context_key: str,
    incoming_text: str,
    reply_text: str,
) -> None:
    await send_messenger_text_reply(
        page_id=incoming["page_id"],
        page_access_token=page_access_token,
        recipient_id=incoming["sender_id"],
        text=reply_text,
    )
    _append_context_message(context_key, "user", incoming_text)
    _append_context_message(context_key, "assistant", reply_text)


async def _fetch_missing_image_urls_from_meta(
    message_id: str,
    page_access_token: str,
) -> list[str]:
    try:
        image_urls = await fetch_message_image_attachment_urls(message_id, page_access_token)
    except MetaWebhookError as exc:
        logger.warning(
            "Could not fetch Meta message attachments message_id=%s reason=%s",
            message_id,
            exc,
        )
        return []
    if image_urls:
        logger.info(
            "Fetched %s Meta attachment image URL(s) for message_id=%s",
            len(image_urls),
            message_id,
        )
    return image_urls


def _logged_result(
    incoming: dict[str, Any],
    status: str,
    reason: Optional[str] = None,
    product_lookup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    result = _result(incoming, status=status, reason=reason, product_lookup=product_lookup)
    _record_single_result(result)
    return result


def _record_single_result(result: dict[str, Any]) -> None:
    response = {
        "received_messages": 1,
        "replied": 1 if result["status"] == "replied" else 0,
        "ignored": 1 if result["status"] == "ignored" else 0,
        "failed": 1 if result["status"] == "failed" else 0,
        "results": [result],
    }
    _record_webhook_result(response)
    logger.info(
        "Meta message processed status=%s page_id=%s message_id=%s",
        result["status"],
        result.get("page_id", ""),
        result.get("message_id", ""),
    )


def get_recent_meta_webhook_results() -> list[dict[str, Any]]:
    return list(reversed(_recent_webhook_results))


def clear_conversation_context(page_id: str, sender_id: str) -> None:
    _conversation_contexts.pop(f"{page_id}:{sender_id}", None)


def _merchant_context(integration: dict[str, Any]) -> str:
    page_name = integration.get("meta_page_name") or "the merchant"
    return (
        f"You are replying to a buyer messaging {page_name} on Facebook Messenger. "
        "Answer briefly and helpfully. If the buyer asks for exact stock, price, "
        "delivery timing, or order placement and live store data is not available, "
        "ask one concise follow-up question or say the team will confirm. "
        "Never say an order has been created, placed, confirmed, or finalized. "
        "Only backend code may create orders and send order confirmation messages. "
        "When product lookup results are provided, answer using those exact prices "
        "and availability details. Say whether the product is available or not, "
        "but do not mention exact stock or inventory counts. Do not say you will check if lookup results "
        "are already available. "
        "Only mention product names, prices, or details that appear verbatim in the "
        "provided lookup results. Never invent, guess, or autocomplete a product name, "
        "model number, or brand that is not in the data. If the lookup results contain "
        "no products, do not name any product. "
        "Use the previous messages when they are provided. Do not greet the buyer "
        "as a new conversation if the recent context already contains prior turns."
    )


def _result(
    incoming: dict[str, Any],
    status: str,
    reason: Optional[str] = None,
    product_lookup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "page_id": incoming.get("page_id", ""),
        "sender_id": incoming.get("sender_id", ""),
        "message_id": incoming.get("message_id", ""),
        "text": incoming.get("text", ""),
        "image_count": len(incoming.get("image_urls") or []),
        "attachment_count": int(incoming.get("attachment_count") or 0),
        "attachment_types": incoming.get("attachment_types") or [],
    }
    if reason:
        result["reason"] = reason
    if product_lookup:
        result["product_lookup"] = _product_lookup_diagnostic(product_lookup)
    return result


def _already_processed(message_id: str) -> bool:
    _clear_old_message_ids()
    return message_id in _processed_message_ids


def _mark_processed(message_id: str) -> None:
    _processed_message_ids[message_id] = time.monotonic()


def _unmark_processed(message_ids: list[str]) -> None:
    # Release reserved ids so a Meta webhook retry can re-deliver the message
    # after a transient processing failure.
    for message_id in message_ids:
        if message_id:
            _processed_message_ids.pop(message_id, None)


def _clear_old_message_ids() -> None:
    threshold = time.monotonic() - DEDUP_WINDOW_SECONDS
    expired = [
        message_id
        for message_id, processed_at in _processed_message_ids.items()
        if processed_at < threshold
    ]
    for message_id in expired:
        _processed_message_ids.pop(message_id, None)


def _context_key(incoming: dict[str, str]) -> str:
    return f"{incoming['page_id']}:{incoming['sender_id']}"


def _get_context_messages(context_key: str) -> list[dict[str, str]]:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    if not context:
        return []
    context["updated_at"] = time.monotonic()
    return list(context["messages"])


def _append_context_message(context_key: str, role: str, content: str) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context["messages"].append({"role": role, "content": content})
    context["messages"] = context["messages"][-MAX_CONTEXT_MESSAGES:]


def _clear_old_conversation_contexts() -> None:
    threshold = time.monotonic() - CONTEXT_WINDOW_SECONDS
    expired = [
        context_key
        for context_key, context in _conversation_contexts.items()
        if context["updated_at"] < threshold
    ]
    for context_key in expired:
        _conversation_contexts.pop(context_key, None)


async def _maybe_handle_order_flow(
    context_key: str,
    integration: dict[str, Any],
    incoming_text: str,
    context_messages: list[dict[str, str]],
    product_lookup: Optional[dict[str, Any]],
) -> Optional[str]:
    _clear_old_order_states()
    state = _order_states.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "details": {}},
    )
    state["updated_at"] = time.monotonic()
    wants_order = _is_order_intent(incoming_text)
    confirms_order = _is_confirmation(incoming_text)
    existing_order = _has_active_order_state(state)
    expected_fields = _missing_order_fields(state) if state.get("product") else []
    continues_order_details = bool(state.get("product")) and _looks_like_order_contact_details(
        incoming_text, expected_fields
    )
    should_handle_order = (
        wants_order
        or confirms_order
        or existing_order
        or continues_order_details
    )

    if not should_handle_order:
        return None

    if product_lookup and product_lookup.get("status") == "found":
        product = (
            None
            if product_lookup.get("requires_confirmation")
            else _first_available_product(product_lookup)
        )
        if product:
            state["product"] = product
            state["quantity"] = _extract_quantity(incoming_text) or state.get("quantity") or 1
            variant = _match_variant(product, incoming_text)
            if variant:
                state["variant"] = variant
                _remember_selected_variant(context_key, variant)

    remembered_product = _get_selected_product(context_key)
    if remembered_product and not state.get("product"):
        state["product"] = remembered_product

    if state.get("product"):
        variant = _match_variant(state["product"], incoming_text)
        if variant:
            state["variant"] = variant
            _remember_selected_variant(context_key, variant)
        elif not state.get("variant"):
            remembered_variant = _get_selected_variant(context_key)
            if remembered_variant:
                state["variant"] = remembered_variant

    extracted_quantity = _extract_quantity(incoming_text)
    if extracted_quantity:
        state["quantity"] = extracted_quantity
    elif not state.get("quantity"):
        state["quantity"] = 1
    # If we previously told the buyer only N are in stock, a plain "yes" means
    # "order that many".
    pending_stock_limit = state.get("awaiting_stock_adjustment")
    if (
        pending_stock_limit is not None
        and extracted_quantity is None
        and _is_confirmation(incoming_text)
    ):
        state["quantity"] = pending_stock_limit
        state.pop("awaiting_stock_adjustment", None)
    adjusting_quantity = extracted_quantity is not None or pending_stock_limit is not None

    expected_fields = _missing_order_fields(state) if state.get("product") else []
    _merge_order_details(state, incoming_text, context_messages, expected_fields)
    continues_order_details = bool(state.get("product")) and _looks_like_order_contact_details(
        incoming_text, expected_fields
    )

    if (
        not wants_order
        and not confirms_order
        and not state.get("awaiting_confirmation")
        and not continues_order_details
        and not adjusting_quantity
    ):
        return None

    if not state.get("product"):
        if product_lookup and product_lookup.get("requires_confirmation"):
            return None
        if _refers_to_visual_product(incoming_text):
            return None
        return "Which product would you like to order? Please send the product name."

    if (
        state.get("awaiting_confirmation")
        and not confirms_order
        and not wants_order
        and not continues_order_details
        and _looks_like_general_question(incoming_text)
    ):
        return None

    stock_reply = _stock_limit_reply(state)
    if stock_reply is not None:
        state["awaiting_confirmation"] = False
        return stock_reply

    missing = _missing_order_fields(state)
    if missing:
        state["awaiting_confirmation"] = False
        return _missing_order_reply(missing)

    if confirms_order or state.get("awaiting_confirmation") and _is_confirmation(incoming_text):
        try:
            order = await _create_shopify_order_from_state(integration, state)
        except ShopifyAPIError as exc:
            # Phone errors get a tailored reply from the outer handler so the buyer
            # can resend a valid number; surface everything else here instead of
            # failing silently and leaving the buyer with no response.
            if _is_shopify_phone_error(str(exc)):
                raise
            logger.warning("Shopify order creation failed: %s", exc)
            state["awaiting_confirmation"] = True
            return (
                "Sorry, I couldn't place the order just now because the store "
                "rejected it. Please double-check your details and try again, or "
                "our team will follow up to complete it."
            )
        _order_states.pop(context_key, None)
        order_name = order.get("name") or f"#{order.get('id')}"
        return (
            f"Your order has been created successfully. Order {order_name}. "
            "Our team will contact you shortly for the next steps."
        )

    state["awaiting_confirmation"] = True
    return _confirmation_reply(state)


def _has_active_order_state(state: dict[str, Any]) -> bool:
    details = state.get("details") if isinstance(state.get("details"), dict) else {}
    return bool(
        state.get("awaiting_confirmation")
        or state.get("product")
        or details.get("name")
        or details.get("phone")
        or details.get("address")
    )


async def _maybe_lookup_products(
    integration: dict[str, Any],
    buyer_text: str,
    context_key: str,
    image_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    image_urls = image_urls or []
    remembered_product = _get_selected_product(context_key)

    # The buyer is confirming a medium-confidence visual guess we asked about on
    # the previous turn ("This looks like X. Is that right?" -> "yes"). Commit it.
    if not image_urls and not remembered_product and _is_confirmation(buyer_text):
        pending_product = _get_pending_product(context_key)
        if pending_product:
            _remember_selected_product(context_key, pending_product)
            return _remembered_product_lookup(buyer_text, pending_product)

    order_state = _order_states.get(context_key)
    expected_fields = (
        _missing_order_fields(order_state)
        if order_state and order_state.get("product")
        else []
    )
    if remembered_product and not image_urls and _looks_like_order_contact_details(buyer_text, expected_fields):
        return _remembered_product_lookup(buyer_text, remembered_product)

    lookup_text = buyer_text
    if not image_urls and not _needs_product_lookup(buyer_text):
        return None

    product_reference = _looks_like_product_reference(lookup_text)
    if remembered_product and not image_urls and not product_reference:
        return _remembered_product_lookup(lookup_text, remembered_product)

    store_domain = integration.get("shopify_store_domain")
    access_token = integration.get("shopify_access_token")
    if not has_value(store_domain) or not has_value(access_token):
        return {
            "status": "unavailable",
            "reason": "Shopify is not connected for this merchant.",
            "products": [],
        }

    access_token = await _refresh_shopify_token_if_needed(integration)
    if image_urls:
        _forget_product_selection(context_key)
        product_lookup = await _lookup_products_from_image(
            store_domain=store_domain,
            access_token=access_token,
            image_url=image_urls[0],
            buyer_text=buyer_text,
        )
        logger.info(
            "Image product lookup confidence=%s status=%s products=%s requires_confirmation=%s",
            product_lookup.get("confidence"),
            product_lookup.get("status"),
            len(product_lookup.get("products") or []),
            bool(product_lookup.get("requires_confirmation")),
        )
        if product_lookup.get("status") == "needs_clarification":
            _set_awaiting_image_clarification(context_key, True)
            return product_lookup
        _set_awaiting_image_clarification(context_key, False)
    else:
        product_lookup = await search_product_summaries(
            store_domain=store_domain,
            access_token=access_token,
            buyer_text=lookup_text,
            limit=3,
        )

    if product_lookup.get("status") == "found":
        product = _first_available_product(product_lookup)
        if product and not product_lookup.get("requires_confirmation"):
            _remember_selected_product(context_key, product)
        elif product and product_lookup.get("confidence") == "medium":
            _remember_pending_product(context_key, product)
    elif remembered_product and not image_urls and not product_reference:
        return _remembered_product_lookup(buyer_text, remembered_product)
    return product_lookup


async def _lookup_products_from_image(
    *,
    store_domain: str,
    access_token: str,
    image_url: str,
    buyer_text: str,
) -> dict[str, Any]:
    """Vision-first image search for a buyer screenshot.

    Flow: Gemini vision describes the product in the image -> catalog text search
    with that description (``search_product_summaries`` today; Meilisearch will
    replace its internals in the next step without changing this call) -> an
    optional visual comparison fallback when the text search misses. If the image
    alone is too ambiguous to search, returns a ``needs_clarification`` result
    carrying a follow-up question to ask the buyer.
    """
    buyer_text = buyer_text or ""
    try:
        vision = await identify_product_from_image(image_url, buyer_text)
    except (GeminiAPIError, GeminiConfigurationError) as exc:
        logger.warning("Vision product identification failed: %s", exc)
        return {
            "status": "not_found",
            "source": "image",
            "query": buyer_text,
            "products": [],
            "requires_confirmation": True,
        }

    vision_block = vision.get("vision") if isinstance(vision.get("vision"), dict) else {}
    vision_query = (vision.get("text") or "").strip()
    vision_confidence = str(vision.get("confidence") or "").lower()
    follow_up_question = str(vision_block.get("follow_up_question") or "").strip()
    needs_more_info = bool(vision_block.get("needs_more_info"))

    # The photo is too ambiguous to search and the buyer's own text does not name
    # a product. Ask one proactive question instead of guessing.
    if needs_more_info and follow_up_question and not _looks_like_product_reference(buyer_text):
        return {
            "status": "needs_clarification",
            "source": "image",
            "query": buyer_text,
            "vision_query": vision_query,
            "vision_confidence": vision_confidence,
            "follow_up_question": follow_up_question,
            "products": [],
            "requires_confirmation": True,
        }

    search_query = "\n".join(part for part in [buyer_text.strip(), vision_query] if part)
    products: list[dict[str, Any]] = []
    if search_query.strip():
        search = await search_product_summaries(
            store_domain=store_domain,
            access_token=access_token,
            buyer_text=search_query,
            limit=settings.shopify_image_match_top_k,
        )
        products = list(search.get("products") or [])

    match_confidence = vision_confidence or "medium"
    match_source = "image_text_search"
    if not products:
        fallback = await _visual_fallback_match(store_domain, access_token, image_url)
        if fallback:
            products = [fallback["product"]]
            match_confidence = fallback["confidence"]
            match_source = "image_visual_match"

    if not products:
        return {
            "status": "not_found",
            "source": "image",
            "query": buyer_text,
            "vision_query": vision_query,
            "vision_confidence": vision_confidence,
            "products": [],
            "requires_confirmation": True,
        }

    return {
        "status": "found",
        "source": "image",
        "query": buyer_text,
        "image_query": vision_query,
        "vision_query": vision_query,
        "vision_confidence": vision_confidence,
        "match_source": match_source,
        "confidence": match_confidence,
        # Only auto-select on a high-confidence visual identification; otherwise
        # name our best guess and ask the buyer to confirm.
        "requires_confirmation": match_confidence != "high",
        "products": products,
    }


async def _visual_fallback_match(
    store_domain: str,
    access_token: str,
    image_url: str,
) -> Optional[dict[str, Any]]:
    """Compare the buyer image to catalog product images when text search misses."""
    try:
        candidates = await list_product_summaries(
            store_domain=store_domain,
            access_token=access_token,
            limit=settings.shopify_image_match_top_k,
            with_images_only=True,
        )
    except ShopifyAPIError as exc:
        logger.warning("Visual fallback candidate fetch failed: %s", exc)
        return None
    if not candidates:
        return None

    try:
        choice = await choose_product_from_image_candidates(image_url, candidates)
    except (GeminiAPIError, GeminiConfigurationError) as exc:
        logger.warning("Visual fallback image comparison failed: %s", exc)
        return None

    candidate = choice.get("candidate")
    if not candidate:
        return None
    confidence = str(choice.get("confidence") or "").lower() or "medium"
    return {"product": candidate, "confidence": confidence}


def _incoming_text_for_context(
    incoming: dict[str, Any],
    product_lookup: Optional[dict[str, Any]],
) -> str:
    text = incoming.get("text") or ""
    if product_lookup and product_lookup.get("image_query"):
        image_text = f"[Buyer sent an image. Image product clue: {product_lookup['image_query']}]"
        return "\n".join(part for part in [text, image_text] if part)
    if incoming.get("image_urls"):
        return text or "[Buyer sent an image.]"
    return text


async def _refresh_shopify_token_if_needed(integration: dict[str, Any]) -> str:
    access_token = integration["shopify_access_token"]
    if not _shopify_token_needs_refresh(integration):
        return access_token

    refresh_token = integration.get("shopify_refresh_token")
    if not has_value(refresh_token):
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


def _shopify_token_needs_refresh(integration: dict[str, Any]) -> bool:
    expires_at = integration.get("shopify_access_token_expires_at")
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (expiry - now).total_seconds() < 300


def _needs_product_lookup(text: str) -> bool:
    if _looks_like_product_reference(text):
        return True
    if _looks_like_order_contact_details(text):
        return False
    # Generic shopping intent — phrased questions like "do you have/sell/carry X"
    # or "looking for X" should hit the catalog regardless of what category the
    # item is. The catalog search is the source of truth; we do not enumerate
    # product types here.
    if re.search(
        r"\b(do you (?:have|sell|carry|stock|offer)|"
        r"(?:looking|searching) for|interested in|show me|got any|"
        r"i (?:want|need|am looking for)|"
        r"price|cost|how much|available|availability|stock|in stock|buy|order)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _remembered_product_lookup(
    buyer_text: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "found",
        "query": buyer_text,
        "source": "conversation_memory",
        "products": [product],
    }


def _get_selected_product(context_key: str) -> Optional[dict[str, Any]]:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    if not context:
        return None
    product = context.get("selected_product")
    return dict(product) if isinstance(product, dict) else None


def _get_selected_variant(context_key: str) -> Optional[dict[str, Any]]:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    if not context:
        return None
    variant = context.get("selected_variant")
    return dict(variant) if isinstance(variant, dict) else None


def _remember_selected_product(context_key: str, product: dict[str, Any]) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context["selected_product"] = product
    context.pop("pending_product", None)


def _remember_recent_image_urls(context_key: str, image_urls: list[str]) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context["recent_image_urls"] = list(image_urls[:3])


def _set_awaiting_image_clarification(context_key: str, awaiting: bool) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    if awaiting:
        context["awaiting_image_clarification"] = True
    else:
        context.pop("awaiting_image_clarification", None)


def _is_awaiting_image_clarification(context_key: str) -> bool:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    return bool(context and context.get("awaiting_image_clarification"))


def _get_recent_image_urls(context_key: str) -> list[str]:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    if not context:
        return []
    image_urls = context.get("recent_image_urls")
    return list(image_urls) if isinstance(image_urls, list) else []


def _forget_product_selection(context_key: str) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context.pop("selected_product", None)
    context.pop("selected_variant", None)
    context.pop("pending_product", None)


def _remember_pending_product(context_key: str, product: dict[str, Any]) -> None:
    # A medium-confidence visual guess we asked the buyer to confirm. Promote it
    # to the committed selection only when they say yes.
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context.pop("selected_product", None)
    context.pop("selected_variant", None)
    context["pending_product"] = product


def _get_pending_product(context_key: str) -> Optional[dict[str, Any]]:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.get(context_key)
    if not context:
        return None
    product = context.get("pending_product")
    return dict(product) if isinstance(product, dict) else None


def _remember_selected_variant(context_key: str, variant: dict[str, Any]) -> None:
    _clear_old_conversation_contexts()
    context = _conversation_contexts.setdefault(
        context_key,
        {"updated_at": time.monotonic(), "messages": []},
    )
    context["updated_at"] = time.monotonic()
    context["selected_variant"] = variant


def _looks_like_product_reference(text: str) -> bool:
    normalized = text.strip()
    if len(normalized) < 3 or len(normalized) > 400:
        return False
    if re.search(r'["“”\'‘’][^"“”\'‘’]{3,}["“”\'‘’]', normalized):
        return True

    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", normalized.lower())
        if token not in _PRODUCT_REFERENCE_STOP_WORDS
    ]
    if not tokens:
        return False

    if _has_product_category_token(tokens):
        return True
    if _looks_like_order_contact_details(normalized):
        return False
    if len(tokens) >= 2 and any(any(char.isdigit() for char in token) for token in tokens):
        return True
    if 2 <= len(tokens) <= 8 and any(token[0].isdigit() for token in tokens):
        return True
    return False


def _has_product_category_token(tokens: list[str]) -> bool:
    product_category_tokens = {
        "snowboard",
        "skateboard",
        "board",
        "shirt",
        "tshirt",
        "t",
        "shoe",
        "shoes",
        "bag",
        "cap",
        "hat",
        "watch",
        "case",
    }
    return any(token in product_category_tokens for token in tokens)


def _looks_like_order_contact_details(
    text: str,
    expected_fields: Optional[list[str]] = None,
) -> bool:
    if _extract_phone(text) and len(_non_empty_lines(text)) >= 2:
        return True
    expected = set(expected_fields or [])
    if expected:
        stripped = text.strip()
        if "phone number" in expected and _extract_phone(stripped):
            return True
        if "name" in expected and _looks_like_person_name(stripped):
            return True
        if "delivery address" in expected and _looks_like_possible_address(stripped):
            return True
    return bool(
        re.search(
            r"\b(delivery address|address|location|area|cash on delivery|cod|quantity|qty)\b|"
            r"\b(?:phone|mobile|number)\s*(?:is|:|-)?\s*\+?\d",
            text,
            flags=re.IGNORECASE,
        )
    )


def _may_contain_order_details(
    text: str,
    expected_fields: Optional[list[str]] = None,
) -> bool:
    return bool(
        _looks_like_order_contact_details(text, expected_fields)
        or _is_order_intent(text)
        or re.search(
            r"\b(my name is|i am|i'm|this is|name\s*(?:is|:|-)|deliver to|delivery at|ship to|send to)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


_PRODUCT_REFERENCE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "could",
    "do",
    "for",
    "hello",
    "hey",
    "hi",
    "i",
    "is",
    "it",
    "me",
    "my",
    "of",
    "please",
    "product",
    "tell",
    "thanks",
    "thank",
    "the",
    "this",
    "to",
    "what",
    "you",
    "your",
}


def _is_order_intent(text: str) -> bool:
    return bool(
        re.search(
            r"\b(order|buy|purchase|checkout|place order|confirm order|take it|i want it|book it|send it|deliver it|cash on delivery|cod)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _refers_to_visual_product(text: str) -> bool:
    return bool(
        re.search(
            r"\b(this|thuis|that|attached|shown|photo|picture|image|pic)\b.{0,40}\b(product|item|one|snowboard)\b|"
            r"\b(product|item|one|snowboard)\b.{0,40}\b(this|thuis|that|attached|shown|photo|picture|image|pic)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _refers_to_recent_image_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(image|photo|picture|pic|screenshot|shown)\b|"
            r"\b(provided|sent)\s+(?:the\s+)?(?:image|photo|picture|screenshot)\b|"
            r"\btry again\b|"
            r"\bdo\s+you\s+have\s+(?:this|thuis|that)\s+(?:product|item|one)\b|"
            r"\bwhat\s+about\s+(?:this|thuis|that)\s+(?:product|item|one)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _should_fetch_meta_attachments(incoming: dict[str, Any], context_key: str) -> bool:
    text = incoming.get("text", "")
    if int(incoming.get("attachment_count") or 0) > 0:
        return True
    if _refers_to_visual_product(text) or _refers_to_recent_image_request(text):
        return True
    if _get_selected_product(context_key) or _get_pending_product(context_key):
        return False
    return _looks_like_product_lookup_without_product_name(text)


def _requires_image_attachment_reply(incoming: dict[str, Any], context_key: str) -> bool:
    text = incoming.get("text", "")
    if int(incoming.get("attachment_count") or 0) > 0:
        return True
    if _refers_to_visual_product(text) or _refers_to_recent_image_request(text):
        return True
    return (
        not _get_selected_product(context_key)
        and not _get_pending_product(context_key)
        and _looks_like_product_lookup_without_product_name(text)
    )


def _looks_like_product_lookup_without_product_name(text: str) -> bool:
    if not _needs_product_lookup(text):
        return False
    if _looks_like_product_reference(text):
        return False
    return True


def _is_confirmation(text: str) -> bool:
    return bool(
        re.search(
            r"\b(yes|confirmed|confirm|correct|right|looks good|ok|okay|sure|go ahead|proceed|place it)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_general_question(text: str) -> bool:
    return bool(
        "?" in text
        or re.search(
            r"\b(when|where|how long|delivery|deliver|shipping|receive|arrive|payment|cod|cash on delivery|return|warranty)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _first_available_product(product_lookup: dict[str, Any]) -> Optional[dict[str, Any]]:
    for product in product_lookup.get("products", []):
        if product.get("available"):
            return product
    return product_lookup.get("products", [None])[0]


def _merge_order_details(
    state: dict[str, Any],
    incoming_text: str,
    context_messages: list[dict[str, str]],
    expected_fields: Optional[list[str]] = None,
) -> None:
    if not _may_contain_order_details(incoming_text, expected_fields):
        return

    details = state.setdefault("details", {})
    text = "\n".join([message.get("content", "") for message in context_messages] + [incoming_text])

    if not details.get("phone"):
        phone = _extract_phone(incoming_text) or _extract_phone(text)
        if phone:
            details["phone"] = phone
    if not details.get("address"):
        address = _extract_address(incoming_text) or _extract_address(text)
        if address:
            details["address"] = address
    if not details.get("name"):
        name = _extract_name(incoming_text) or _extract_name(text)
        if name:
            details["name"] = name

    if _looks_like_order_contact_details(incoming_text, expected_fields):
        fallback_details = _extract_plain_order_details(incoming_text, expected_fields)
        if not details.get("phone") and fallback_details.get("phone"):
            details["phone"] = fallback_details["phone"]
        if not details.get("address") and fallback_details.get("address"):
            details["address"] = fallback_details["address"]
        if not details.get("name") and fallback_details.get("name"):
            details["name"] = fallback_details["name"]


def _missing_order_fields(state: dict[str, Any]) -> list[str]:
    missing = []
    details = state.get("details", {})
    if not details.get("name"):
        missing.append("name")
    if not details.get("phone"):
        missing.append("phone number")
    if not details.get("address"):
        missing.append("delivery address")
    return missing


def _missing_order_reply(missing: list[str]) -> str:
    return "To create your order, please send your " + ", ".join(missing) + "."


def _confirmation_reply(state: dict[str, Any]) -> str:
    details = state["details"]
    product = state["product"]
    variant = state.get("variant")
    quantity = state.get("quantity", 1)
    return (
        f"Please confirm your order: {quantity} x {_order_item_title(product, variant)} "
        f"at {product.get('price')} each. Delivery address: {details['address']}. "
        f"Phone: {details['phone']}. Reply yes to confirm."
    )


async def _create_shopify_order_from_state(
    integration: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    access_token = await _refresh_shopify_token_if_needed(integration)
    product = state["product"]
    variant = state.get("variant") or _first_available_variant(product)
    if not variant or not variant.get("id"):
        raise ShopifyAPIError("No available Shopify variant was found for this order")

    details = state["details"]
    first_name, last_name = _split_name(details["name"])
    shopify_phone = _normalize_phone_for_shopify(details["phone"])
    order_payload = {
        "line_items": [
            {
                "variant_id": variant["id"],
                "quantity": int(state.get("quantity", 1)),
            }
        ],
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
        },
        "shipping_address": {
            "first_name": first_name,
            "last_name": last_name,
            "address1": details["address"],
            "phone": shopify_phone,
        },
        "financial_status": "pending",
        "send_receipt": False,
        "send_fulfillment_receipt": False,
        # Without this Shopify defaults to "bypass" and never touches stock. This
        # decrements inventory while respecting each variant's oversell policy, so
        # a "deny" variant cannot be pushed below zero.
        "inventory_behaviour": "decrement_obeying_policy",
        "note": f"Created from Bingasys Messenger bot. Buyer phone: {shopify_phone}.",
    }
    return await create_order(
        store_domain=integration["shopify_store_domain"],
        access_token=access_token,
        order=order_payload,
    )


def _purchasable_quantity(variant: Optional[dict[str, Any]]) -> Optional[int]:
    """Units that can be ordered for a variant. None means effectively unlimited.

    Returns None when the variant does not track inventory or allows overselling
    (policy "continue") — in those cases Shopify will accept any quantity.
    """
    if not variant or not variant.get("tracks_inventory"):
        return None
    if variant.get("inventory_policy") == "continue":
        return None
    quantity = variant.get("inventory_quantity")
    if quantity is None:
        return None
    try:
        return max(0, int(quantity))
    except (TypeError, ValueError):
        return None


def _stock_limit_reply(state: dict[str, Any]) -> Optional[str]:
    """Reply when the requested quantity exceeds what is in stock.

    Returns None when the order is within stock (and clears any pending
    adjustment), so the caller can continue placing the order.
    """
    product = state.get("product")
    if not product:
        return None
    variant = state.get("variant") or _first_available_variant(product)
    available = _purchasable_quantity(variant)
    if available is None:
        state.pop("awaiting_stock_adjustment", None)
        return None

    requested = int(state.get("quantity") or 1)
    if 0 < requested <= available:
        state.pop("awaiting_stock_adjustment", None)
        return None

    state["awaiting_stock_adjustment"] = available
    variant_for_title = state.get("variant")
    title = _order_item_title(product, variant_for_title)
    if available <= 0:
        return f"Sorry, {title} is out of stock right now, so I can't place that order."
    units = "unit" if available == 1 else "units"
    return (
        f"I can only order {available} {units} of {title} right now — that's all we "
        f"have in stock. Reply 'yes' to order {available}, or tell me a smaller quantity."
    )


def _first_available_variant(product: dict[str, Any]) -> Optional[dict[str, Any]]:
    variants = product.get("variants") or []
    for variant in variants:
        if variant.get("available"):
            return variant
    return variants[0] if variants else None


def _match_variant(product: dict[str, Any], text: str) -> Optional[dict[str, Any]]:
    text_tokens = set(_order_tokenize(text))
    if not text_tokens:
        return None
    for variant in product.get("variants") or []:
        title = str(variant.get("title") or "")
        if title.lower() == "default title":
            continue
        variant_tokens = set(_order_tokenize(title))
        if variant_tokens and variant_tokens <= text_tokens:
            return variant
    for variant in product.get("variants") or []:
        title = str(variant.get("title") or "")
        if title.lower() == "default title":
            continue
        variant_tokens = set(_order_tokenize(title))
        if variant_tokens and variant_tokens & text_tokens:
            return variant
    return None


def _order_item_title(product: dict[str, Any], variant: Optional[dict[str, Any]]) -> str:
    product_title = str(product.get("title") or "the product")
    variant_title = str((variant or {}).get("title") or "")
    if not variant_title or variant_title.lower() == "default title":
        return product_title
    return f"{product_title} - {variant_title}"


def _extract_phone(text: str) -> Optional[str]:
    # Match within a single line only. A phone number and a following address line
    # such as "233, ..." must never merge into one bogus number (the regex class
    # for digit/space/hyphen would otherwise span the newline and swallow the
    # house number), which Shopify then rejects.
    for line in text.splitlines() or [text]:
        match = re.search(r"(\+?\d[\d\s-]{6,}\d)", line)
        if not match:
            continue
        normalized = re.sub(r"[\s-]+", "", match.group(1))
        digit_count = len(normalized.lstrip("+"))
        if 7 <= digit_count <= 15:
            return normalized
    return None


def _normalize_phone_for_shopify(phone: str) -> str:
    value = re.sub(r"[^\d+]", "", phone.strip())
    if value.startswith("+"):
        return value
    if value.startswith("00") and len(value) > 4:
        return f"+{value[2:]}"
    if value.startswith("880"):
        return f"+{value}"
    if value.startswith("01") and len(value) == 11:
        return f"+88{value}"
    return value


def _is_shopify_phone_error(error_message: str) -> bool:
    return bool(
        re.search(
            r"phone(?:_number)?|customer\.phone",
            error_message,
            flags=re.IGNORECASE,
        )
    )


def _forget_order_phone(context_key: str) -> None:
    state = _order_states.get(context_key)
    if not state:
        return
    details = state.get("details")
    if isinstance(details, dict):
        details.pop("phone", None)
    state["awaiting_confirmation"] = False


def _extract_address(text: str) -> Optional[str]:
    patterns = [
        r"(?:delivery\s+address|address|location|area)\s*(?:is|:|-)?\s*([^\n]+?)(?:\s+(?:and\s+)?(?:phone|number|mobile)\b|$)",
        r"(?:deliver|delivery|ship|send)\s+(?:it\s+)?(?:to|at)\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = _clean_address_value(match.group(1))
            if value:
                return value
    return None


def _extract_name(text: str) -> Optional[str]:
    patterns = [
        r"(?:my\s+name\s+is|name\s*(?:is|:|-))\s*([A-Za-z][A-Za-z .'-]{1,60}?)(?:[.,!]\s+|(?:\s+(?:address|phone|number|mobile)\b)|$)",
        r"(?:i\s+am|i'm|this\s+is)\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?:[.,!]\s+|(?:\s+(?:address|phone|number|mobile)\b)|$)",
        r"Thanks,\s*([^!.\n]{2,60})[!.]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .,!") 
            if value and _looks_like_person_name(value):
                return value
    return None


def _extract_quantity(text: str) -> Optional[int]:
    word_quantities = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
    }
    match = re.search(r"\b(?:qty|quantity)\s*(?:is|:|-)?\s*(\d{1,3})\b", text, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    # "order 10", "buy 5", "I want 3" — small counts only so we never mistake a
    # phone number or house number for a quantity.
    match = re.search(
        r"\b(?:order|buy|want|need|take|get)\s+(\d{1,2})\b", text, flags=re.IGNORECASE
    )
    if match:
        return max(1, int(match.group(1)))
    match = re.search(r"\bx\s*(\d{1,3})\b|\b(\d{1,3})\s*x\b", text, flags=re.IGNORECASE)
    if match:
        return max(1, int(next(group for group in match.groups() if group)))
    # "2pc", "2 pcs", "3 pieces", "2 units", "1 pair" — note the optional trailing
    # "s" so the common shorthand "2pc" is recognized.
    match = re.search(
        r"\b(\d{1,3})\s*(?:pcs?|pieces?|items?|pairs?|units?)\b", text, flags=re.IGNORECASE
    )
    if match:
        return max(1, int(match.group(1)))
    match = re.search(
        r"\b(one|two|three|four|five)\s*(?:pc|pcs|piece|pieces|item|items)\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return word_quantities[match.group(1).lower()]
    return None


def _extract_plain_order_details(
    text: str,
    expected_fields: Optional[list[str]] = None,
) -> dict[str, str]:
    details: dict[str, str] = {}
    expected = set(expected_fields or [])
    phone = _extract_phone(text)
    if phone:
        details["phone"] = phone

    lines = [
        line
        for line in _non_empty_lines(text)
        if not _extract_phone(line) and not _looks_like_order_instruction(line)
    ]
    if not lines:
        return details

    if (not expected or "name" in expected) and _looks_like_person_name(lines[0]):
        details["name"] = lines[0].strip(" .,!:")
        lines = lines[1:]

    address_lines = [
        line.strip(" .,!:")
        for line in lines
        if line.strip(" .,!:") and not _looks_like_person_name(line)
    ]
    if (not expected or "delivery address" in expected) and address_lines:
        details["address"] = ", ".join(address_lines)
    elif (not expected or "delivery address" in expected) and lines and not details.get("address"):
        details["address"] = lines[0].strip(" .,!:")
    return details


def _clean_address_value(value: str) -> str:
    cleaned = re.split(
        r"(?:\.\s*)?\b(?:qty|quantity|one|two|three|four|five|\d{1,3})\s*"
        r"(?:pc|pcs|piece|pieces|item|items)\b|"
        r"\b(?:cash on delivery|cod|phone|mobile|number)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cleaned.strip(" .,!:")


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _looks_like_order_instruction(text: str) -> bool:
    return bool(
        re.search(
            r"\b(please|create|order|quantity|qty|piece|cash on delivery|cod|possible)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_possible_address(text: str) -> bool:
    value = text.strip(" .,!:")
    if not value or _is_confirmation(value) or _looks_like_person_name(value):
        return False
    if re.fullmatch(r"x\s*\d{1,3}|\d{1,3}\s*x", value, flags=re.IGNORECASE):
        return False
    if "," in value:
        return True
    if any(char.isdigit() for char in value) and len(value) >= 5 and re.search(r"[A-Za-z]", value):
        return True
    return bool(
        re.search(
            r"\b(road|rd|street|st|avenue|ave|lane|ln|house|flat|apt|apartment|block|sector|area|para|parbata|dhaka|chittagong)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_person_name(text: str) -> bool:
    value = text.strip(" .,!:")
    if _is_confirmation(value):
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,60}", value):
        return False
    lowered = value.lower()
    tokens = set(re.findall(r"[a-z]+", lowered))
    if tokens & {
        "buy",
        "checkout",
        "need",
        "order",
        "product",
        "purchase",
        "snowboard",
        "variant",
        "want",
    }:
        return False
    if lowered.startswith(("i need ", "i want ", "need ", "want ")):
        return False
    if any(word in lowered.split() for word in {"street", "road", "avenue", "lane", "address"}):
        return False
    return not any(char.isdigit() for char in value)


def _order_tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in {"the", "one", "variant", "option", "color", "colour"}
    ]


def _split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split()
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _clear_old_order_states() -> None:
    threshold = time.monotonic() - ORDER_STATE_WINDOW_SECONDS
    expired = [
        context_key
        for context_key, state in _order_states.items()
        if state["updated_at"] < threshold
    ]
    for context_key in expired:
        _order_states.pop(context_key, None)


def _product_lookup_context(product_lookup: dict[str, Any]) -> str:
    if product_lookup.get("status") == "not_found":
        if product_lookup.get("source") in {"image", "image_embedding"}:
            return (
                "Shopify image lookup result: the buyer sent an image, but no confident "
                "catalog match was found. Ask for the product name or a clearer product "
                "photo. Do not name any product. Do not ask for order details yet."
            )
        return (
            "Shopify product lookup result: no matching product was found. "
            "Ask the buyer to confirm the product name. Do not name any product."
        )
    if product_lookup.get("status") == "unavailable":
        return (
            "Shopify product lookup result: Shopify is not connected. "
            "Tell the buyer the team will confirm price and availability."
        )
    if product_lookup.get("requires_confirmation"):
        rendered = _render_lookup_products(product_lookup)
        if not rendered:
            return (
                "Shopify image lookup result: the buyer sent an image, but no confident "
                "catalog match was found. Ask for the product name or a clearer product "
                "photo. Do not name any product. Do not ask for order details yet."
            )
        return (
            "Shopify image lookup result: the image produced possible catalog matches, "
            "but it is not confident enough to choose one product. Do not say these are "
            "definitely the pictured product. Do not ask for order details yet. Only refer "
            "to the products listed below by their exact names; do not invent any other "
            "product. Briefly say you found possible matches and ask the buyer to choose "
            "one or send a clearer image/product name. Candidate products:\n"
            f"{rendered}"
        )

    rendered = _render_lookup_products(product_lookup)
    if not rendered:
        return (
            "Shopify product lookup result: no matching product was found. "
            "Ask the buyer to confirm the product name. Do not name any product."
        )
    return (
        "Shopify product lookup result. Use only these products in your reply, by their "
        "exact names, and be concise. Do not mention any product not listed here:\n"
        f"{rendered}"
    )


def _render_lookup_products(product_lookup: dict[str, Any]) -> str:
    """Render only the real product fields for the LLM prompt.

    Excludes vision/search internals (image_query, search_query, etc.) that the
    model could otherwise turn into a hallucinated product name.
    """
    lines = []
    for product in product_lookup.get("products") or []:
        title = product.get("title")
        if not title:
            continue
        parts = [f"- {title}"]
        price = product.get("price")
        if price:
            parts.append(f"price: {price}")
        availability = "available" if product.get("available") else "not available"
        parts.append(availability)
        variant_titles = [
            str(variant.get("title"))
            for variant in (product.get("variants") or [])
            if variant.get("title") and str(variant.get("title")).lower() != "default title"
        ]
        if variant_titles:
            parts.append("options: " + ", ".join(variant_titles[:8]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _image_clarification_reply(product_lookup: Optional[dict[str, Any]]) -> Optional[str]:
    """Proactive question to send when the image is too ambiguous to search."""
    if not product_lookup or product_lookup.get("status") != "needs_clarification":
        return None
    question = str(product_lookup.get("follow_up_question") or "").strip()
    return question or (
        "I can see your photo, but I need a little more detail to find the right "
        "product. Could you tell me the brand, color, or model?"
    )


def _image_lookup_uncertain_reply(product_lookup: Optional[dict[str, Any]]) -> Optional[str]:
    if not product_lookup or product_lookup.get("source") not in {"image", "image_embedding"}:
        return None
    if not product_lookup.get("requires_confirmation"):
        return None

    products = product_lookup.get("products") or []

    # A single medium-confidence visual match: name our best guess and ask the
    # buyer to confirm, rather than listing it as one of several or giving up.
    if (
        product_lookup.get("confidence") == "medium"
        and product_lookup.get("requires_confirmation")
        and len(products) == 1
        and products[0].get("title")
    ):
        product = products[0]
        price = product.get("price")
        return (
            f"This looks like {product['title']}"
            + (f" ({price})" if price else "")
            + ". Is that the product you mean? Reply yes to confirm, or send the "
            "product name or a clearer photo if it's different."
        )

    if product_lookup.get("requires_confirmation") and products:
        product_lines = []
        for product in products[:3]:
            title = product.get("title")
            price = product.get("price")
            if not title:
                continue
            product_lines.append(f"- {title}" + (f" at {price}" if price else ""))
        if product_lines:
            return (
                "I found a few possible matches, but I cannot confirm the exact "
                "product yet:\n"
                + "\n".join(product_lines)
                + "\n\nWhich one is it? You can also send the product name or a clearer photo."
            )

    return (
        "I can see the image, but I could not match it confidently to a product in "
        "the catalog yet. Please send the product name or a clearer product photo."
    )


def _missing_image_attachment_reply(
    incoming: dict[str, Any],
    product_lookup: Optional[dict[str, Any]],
) -> Optional[str]:
    if product_lookup or incoming.get("image_urls"):
        return None
    text = incoming.get("text") or ""
    has_attachment = int(incoming.get("attachment_count") or 0) > 0
    refers_to_image = _refers_to_visual_product(text) or _refers_to_recent_image_request(text)
    if not (has_attachment or refers_to_image or _looks_like_product_lookup_without_product_name(text)):
        return None
    if not has_attachment and not refers_to_image:
        return "Please send the product name or a product photo so I can check availability."
    return (
        "I could not receive the photo attachment for this message. Please send the "
        "photo again as a new image message, or send the product name."
    )


def _product_lookup_diagnostic(product_lookup: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": product_lookup.get("status"),
        "source": product_lookup.get("source"),
        "query": product_lookup.get("query"),
        "confidence": product_lookup.get("confidence"),
        "top_score": product_lookup.get("top_score"),
        "vision_query": product_lookup.get("vision_query"),
        "vision_confidence": product_lookup.get("vision_confidence"),
        "requires_confirmation": bool(product_lookup.get("requires_confirmation")),
        "matches": [
            {
                "shopify_product_id": match.get("shopify_product_id"),
                "shopify_variant_id": match.get("shopify_variant_id"),
                "score": match.get("score"),
                "source": match.get("source"),
            }
            for match in (product_lookup.get("matches") or [])[:5]
        ],
        "product_titles": [
            product.get("title")
            for product in (product_lookup.get("products") or [])[:5]
            if product.get("title")
        ],
    }


def _raw_webhook_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id") or "")
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            attachments = message.get("attachments") or []
            if isinstance(attachments, dict):
                attachments = [attachments]
            attachment_summaries = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                payload_value = attachment.get("payload") or {}
                url = payload_value.get("url")
                attachment_summaries.append(
                    {
                        "type": attachment.get("type"),
                        "payload_keys": sorted(payload_value.keys()),
                        "has_url": isinstance(url, str) and bool(url.strip()),
                        "url_hint": _url_hint(url) if isinstance(url, str) else None,
                    }
                )
            summaries.append(
                {
                    "page_id": page_id,
                    "sender_id": str((event.get("sender") or {}).get("id") or ""),
                    "message_id": str(message.get("mid") or ""),
                    "has_text": isinstance(message.get("text"), str)
                    and bool(message.get("text", "").strip()),
                    "attachment_count": len(attachments),
                    "attachments": attachment_summaries,
                }
            )
    return summaries


def _url_hint(url: str) -> str:
    return url.split("?", 1)[0][-80:]


def _fallback_reply() -> str:
    return "Thanks for your message. How can I help you today?"


def _sanitize_llm_reply(text: str) -> str:
    first_person_claim = re.search(
        r"\b(?:i|we|the team|our team)\b.{0,40}\b(created|placed|confirmed|finalized)\b.{0,40}\b(order|purchase)\b",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    direct_order_claim = re.search(
        r"\b(?:your|the)\s+(order|purchase)\b.{0,40}\b(has been|is|was|already)\s+\b(created|placed|confirmed|finalized)\b",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    conditional_order_phrase = re.search(
        r"\b(?:after|once|when|before)\s+(?:your|the)\s+(order|purchase)\b.{0,40}\b(created|placed|confirmed|finalized)\b",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if first_person_claim or (direct_order_claim and not conditional_order_phrase):
        return (
            "I can help create the order, but I need to complete the checkout steps first. "
            "Please send the product name, your name, phone number, and delivery address."
        )
    return text


def _record_webhook_result(response: dict[str, Any]) -> None:
    _recent_webhook_results.append(
        {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "received_messages": response["received_messages"],
            "replied": response["replied"],
            "ignored": response["ignored"],
            "failed": response["failed"],
            "results": response["results"],
        }
    )
    del _recent_webhook_results[:-RECENT_RESULT_LIMIT]

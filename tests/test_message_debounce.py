"""Tests for per-buyer debounce/merge of Meta webhook events.

Facebook delivers a caption and its image as separate events; these verify they
are merged into one logical message and answered with a single reply.

    .venv/bin/python -m unittest tests.test_message_debounce -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.messaging_service as ms


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


INTEGRATION = {
    "user_id": 1,
    "meta_page_name": "Test Store",
    "meta_access_token": "page-token-xyz",
    "shopify_store_domain": "test-store.myshopify.com",
    "shopify_access_token": "shpat_testtoken_123456",
}


def _text_event(mid: str, text: str = "", images=None) -> dict[str, Any]:
    return {
        "page_id": "PAGE1",
        "recipient_id": "PAGE1",
        "sender_id": "BUYER1",
        "message_id": mid,
        "text": text,
        "image_urls": images or [],
    }


class MergeGroupTests(unittest.TestCase):
    def test_merges_text_and_images_dedup(self) -> None:
        merged = ms._merge_incoming_group(
            [
                _text_event("m1", text="Do you have this product?"),
                _text_event("m2", images=["https://img/a.jpg"]),
                _text_event("m3", images=["https://img/a.jpg", "https://img/b.jpg"]),
            ]
        )
        self.assertEqual(merged["text"], "Do you have this product?")
        self.assertEqual(merged["image_urls"], ["https://img/a.jpg", "https://img/b.jpg"])
        self.assertEqual(merged["message_id"], "m3")
        self.assertEqual(merged["merged_message_ids"], ["m1", "m2", "m3"])


class DebounceWebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ms._conversation_contexts.clear()
        ms._order_states.clear()
        ms._processed_message_ids.clear()
        ms._pending_message_buffers.clear()
        ms._recent_webhook_results.clear()

    def _payload(self, mid: str, text: str = "", images=None) -> dict[str, Any]:
        message: dict[str, Any] = {"mid": mid}
        if text:
            message["text"] = text
        if images:
            message["attachments"] = [
                {"type": "image", "payload": {"url": url}} for url in images
            ]
        return {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE1",
                    "messaging": [
                        {
                            "sender": {"id": "BUYER1"},
                            "recipient": {"id": "PAGE1"},
                            "message": message,
                        }
                    ],
                }
            ],
        }

    async def test_separate_text_then_image_yields_single_reply(self) -> None:
        send_mock = mock.AsyncMock(return_value={"message_id": "out"})
        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=send_mock), \
             mock.patch.object(ms, "identify_product_from_image", new=mock.AsyncMock(return_value={"text": "snowboard", "confidence": "low", "vision": {}})), \
             mock.patch.object(ms, "search_product_summaries", new=mock.AsyncMock(return_value={"status": "not_found", "query": "Do you have this product?\nsnowboard", "products": []})), \
             mock.patch.object(ms, "list_product_summaries", new=mock.AsyncMock(return_value=[])), \
             mock.patch.object(ms, "choose_product_from_image_candidates", new=mock.AsyncMock(return_value={"selected_index": None, "confidence": "low", "candidate": None})), \
             mock.patch.object(ms, "MESSAGE_DEBOUNCE_SECONDS", 0.15):
            # Event 1: the text caption.
            ack1 = await ms.handle_meta_message_webhook(self._payload("m1", text="Do you have this product?"))
            # Event 2: the image, arriving shortly after.
            await asyncio.sleep(0.05)
            ack2 = await ms.handle_meta_message_webhook(self._payload("m2", images=["https://img/board.jpg"]))

            self.assertEqual(ack1["status"], "accepted")
            self.assertEqual(ack2["buffered"], 1)
            # Wait for the debounce flush to fire.
            await asyncio.sleep(0.4)

        self.assertEqual(send_mock.await_count, 1, "expected exactly one reply for the merged message")
        # The single reply went to the right buyer.
        _, kwargs = send_mock.await_args
        self.assertEqual(kwargs["recipient_id"], "BUYER1")

    async def test_duplicate_message_id_not_buffered_twice(self) -> None:
        send_mock = mock.AsyncMock(return_value={"message_id": "out"})
        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=send_mock), \
             mock.patch.object(ms, "search_product_summaries", new=mock.AsyncMock(return_value={"status": "not_found", "query": "hello", "products": []})), \
             mock.patch.object(ms, "generate_sales_reply", new=mock.AsyncMock(return_value={"text": "Hi there!"})), \
             mock.patch.object(ms, "MESSAGE_DEBOUNCE_SECONDS", 0.1):
            await ms.handle_meta_message_webhook(self._payload("dup1", text="hello"))
            ack2 = await ms.handle_meta_message_webhook(self._payload("dup1", text="hello"))
            self.assertEqual(ack2["ignored"], 1)
            self.assertEqual(ack2["buffered"], 0)
            await asyncio.sleep(0.3)

        self.assertEqual(send_mock.await_count, 1)

    async def test_failed_processing_releases_id_for_retry(self) -> None:
        # If the reply pipeline fails, the message id must be released so a Meta
        # webhook retry can re-deliver and the buyer still gets an answer.
        send_mock = mock.AsyncMock(return_value={"message_id": "out"})
        good_reply = mock.AsyncMock(return_value={"text": "Hello!"})
        flaky_reply = mock.AsyncMock(side_effect=ms.GeminiAPIError("upstream down"))
        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=send_mock), \
             mock.patch.object(ms, "search_product_summaries", new=mock.AsyncMock(return_value={"status": "not_found", "query": "hi", "products": []})), \
             mock.patch.object(ms, "MESSAGE_DEBOUNCE_SECONDS", 0.1):
            # First delivery: Gemini fails.
            with mock.patch.object(ms, "generate_sales_reply", new=flaky_reply):
                await ms.handle_meta_message_webhook(self._payload("retry1", text="hi"))
                await asyncio.sleep(0.3)
            self.assertEqual(send_mock.await_count, 0)
            self.assertNotIn("retry1", ms._processed_message_ids)

            # Meta retries the same event; this time Gemini works.
            with mock.patch.object(ms, "generate_sales_reply", new=good_reply):
                ack = await ms.handle_meta_message_webhook(self._payload("retry1", text="hi"))
                self.assertEqual(ack["buffered"], 1)  # not dropped as duplicate
                await asyncio.sleep(0.3)

        self.assertEqual(send_mock.await_count, 1)

    async def test_flush_now_drains_buffer(self) -> None:
        send_mock = mock.AsyncMock(return_value={"message_id": "out"})
        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=send_mock), \
             mock.patch.object(ms, "search_product_summaries", new=mock.AsyncMock(return_value={"status": "not_found", "query": "hi", "products": []})), \
             mock.patch.object(ms, "generate_sales_reply", new=mock.AsyncMock(return_value={"text": "Hello!"})), \
             mock.patch.object(ms, "MESSAGE_DEBOUNCE_SECONDS", 100):  # long, so auto-flush won't fire
            await ms.handle_meta_message_webhook(self._payload("slow1", text="hi"))
            # Cancel the long-running debounce task so flush_now owns the events.
            buffer = ms._pending_message_buffers.get("PAGE1:BUYER1")
            if buffer and buffer.get("task"):
                buffer["task"].cancel()
            await ms.flush_pending_messages_now()

        self.assertEqual(send_mock.await_count, 1)
        self.assertEqual(len(ms._pending_message_buffers), 0)


if __name__ == "__main__":
    unittest.main()

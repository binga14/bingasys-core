"""Tests for the Messenger -> Shopify order flow.

Run with:

    .venv/bin/python -m unittest tests.test_order_flow -v
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
    "meta_access_token": "page_tok",
    "shopify_store_domain": "test-store.myshopify.com",
    "shopify_access_token": "shpat_x",
}
PRODUCT = {
    "id": "111",
    "title": "The Collection Snowboard: Liquid",
    "available": True,
    "price": "749.95",
    "variants": [
        {"id": "9001", "title": "Default Title", "price": "749.95", "available": True}
    ],
    "images": [],
}


def _msg(mid: str, text: str) -> dict[str, Any]:
    return {
        "page_id": "PAGE1",
        "sender_id": "BUYER1",
        "message_id": mid,
        "text": text,
        "image_urls": [],
        "attachment_count": 0,
        "attachment_types": [],
    }


class PhoneExtractionTests(unittest.TestCase):
    def test_phone_does_not_swallow_next_line_house_number(self) -> None:
        # Regression: a phone line followed by an address line starting with a
        # number must not merge into one invalid number.
        text = "Mobile: 01766813937\n233, Shenpara Parbata"
        self.assertEqual(ms._extract_phone(text), "01766813937")

    def test_phone_with_address_keyword_line(self) -> None:
        text = "Mobile: 01766813937\nAddress: 233, Shenpara Parbata"
        self.assertEqual(ms._extract_phone(text), "01766813937")

    def test_phone_with_internal_spaces(self) -> None:
        self.assertEqual(ms._extract_phone("call 01766 813 937"), "01766813937")

    def test_address_only_line_has_no_phone(self) -> None:
        self.assertIsNone(ms._extract_phone("233, Shenpara Parbata"))


class QuantityParsingTests(unittest.TestCase):
    def test_pc_shorthand(self) -> None:
        self.assertEqual(ms._extract_quantity("2pc of The revenge t-shirt"), 2)

    def test_various_quantity_phrasings(self) -> None:
        self.assertEqual(ms._extract_quantity("I need 2 t-shirts"), 2)
        self.assertEqual(ms._extract_quantity("order 10"), 10)
        self.assertEqual(ms._extract_quantity("3 pieces"), 3)
        self.assertEqual(ms._extract_quantity("2 x The revenge t-shirt"), 2)
        self.assertEqual(ms._extract_quantity("qty: 4"), 4)

    def test_no_quantity_returns_none(self) -> None:
        self.assertIsNone(ms._extract_quantity("Address: 233, Shenpara Parbata"))


class OrderCreationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ms._conversation_contexts.clear()
        ms._order_states.clear()

    async def test_order_places_with_valid_phone(self) -> None:
        created: list[dict[str, Any]] = []

        async def fake_create_order(store_domain, access_token, order):
            created.append(order)
            return {"id": 5001, "name": "#1001"}

        async def fake_search(store_domain, access_token, buyer_text, limit=3):
            if "snowboard" in buyer_text.lower() or "liquid" in buyer_text.lower():
                return {"status": "found", "query": buyer_text, "products": [PRODUCT]}
            return {"status": "not_found", "products": []}

        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=mock.AsyncMock(return_value={"message_id": "out"})), \
             mock.patch.object(ms, "search_product_summaries", new=fake_search), \
             mock.patch.object(ms, "create_order", new=fake_create_order), \
             mock.patch.object(ms, "_refresh_shopify_token_if_needed", new=mock.AsyncMock(return_value="shpat_x")):
            for mid, text in [
                ("m1", "Do you have The Collection Snowboard: Liquid ?"),
                ("m2", "I want to order it"),
                ("m3", "My name is Kamrul\nMobile: 01766813937\nAddress: 233, Shenpara Parbata"),
                ("m4", "yes"),
            ]:
                await ms._process_incoming_message(_msg(mid, text))

        self.assertEqual(len(created), 1)
        order = created[0]
        self.assertEqual(order["shipping_address"]["phone"], "+8801766813937")
        self.assertEqual(order["line_items"][0]["variant_id"], "9001")

    async def test_order_sends_inventory_behaviour_to_decrement_stock(self) -> None:
        created: list[dict[str, Any]] = []

        async def fake_create_order(store_domain, access_token, order):
            created.append(order)
            return {"id": 5001, "name": "#1001"}

        async def fake_search(store_domain, access_token, buyer_text, limit=3):
            if "snowboard" in buyer_text.lower() or "liquid" in buyer_text.lower():
                return {"status": "found", "query": buyer_text, "products": [PRODUCT]}
            return {"status": "not_found", "products": []}

        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=mock.AsyncMock(return_value={"message_id": "out"})), \
             mock.patch.object(ms, "search_product_summaries", new=fake_search), \
             mock.patch.object(ms, "create_order", new=fake_create_order), \
             mock.patch.object(ms, "_refresh_shopify_token_if_needed", new=mock.AsyncMock(return_value="shpat_x")):
            for mid, text in [
                ("m1", "Do you have The Collection Snowboard: Liquid ?"),
                ("m2", "I want to order it"),
                ("m3", "My name is Kamrul\nMobile: 01766813937\nAddress: 233, Shenpara Parbata"),
                ("m4", "yes"),
            ]:
                await ms._process_incoming_message(_msg(mid, text))

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["inventory_behaviour"], "decrement_obeying_policy")

    async def test_order_over_stock_is_acknowledged_not_placed(self) -> None:
        # Tracked product, deny policy, only 5 in stock.
        product = {
            "id": "111",
            "title": "The revenge t-shirt",
            "available": True,
            "price": "20.00",
            "variants": [
                {
                    "id": "v1",
                    "title": "Default Title",
                    "price": "20.00",
                    "inventory_quantity": 5,
                    "inventory_policy": "deny",
                    "tracks_inventory": True,
                    "available": True,
                }
            ],
        }
        created: list[dict[str, Any]] = []
        replies: list[str] = []

        async def fake_create_order(store_domain, access_token, order):
            created.append(order)
            return {"id": 1, "name": "#1"}

        async def fake_search(store_domain, access_token, buyer_text, limit=3):
            if "shirt" in buyer_text.lower() or "revenge" in buyer_text.lower():
                return {"status": "found", "query": buyer_text, "products": [product]}
            return {"status": "not_found", "products": []}

        async def capture_send(page_id, page_access_token, recipient_id, text):
            replies.append(text)
            return {"message_id": "out"}

        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=capture_send), \
             mock.patch.object(ms, "search_product_summaries", new=fake_search), \
             mock.patch.object(ms, "create_order", new=fake_create_order), \
             mock.patch.object(ms, "_refresh_shopify_token_if_needed", new=mock.AsyncMock(return_value="shpat_x")):
            await ms._process_incoming_message(_msg("m1", "Do you have the revenge t-shirt?"))
            await ms._process_incoming_message(_msg("m2", "I want to order 10"))
            # The bot must acknowledge the stock limit, not place an order for 10.
            self.assertEqual(len(created), 0)
            self.assertIn("only order 5", replies[-1])

            # Accepting the limit places the order for the available quantity.
            await ms._process_incoming_message(_msg("m3", "yes"))
            await ms._process_incoming_message(
                _msg("m4", "My name is Kamrul\nMobile: 01766813937\nAddress: 233, Shenpara Parbata")
            )
            await ms._process_incoming_message(_msg("m5", "yes"))

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["line_items"][0]["quantity"], 5)

    async def test_over_stock_warns_before_asking_for_contact_details(self) -> None:
        # Reproduces the "2pc, only 1 in stock" report: the bot must warn about
        # stock at the order request, not ask for name/phone/address first.
        product = {
            "id": "111",
            "title": "The revenge t-shirt",
            "available": True,
            "price": "18.00",
            "variants": [
                {
                    "id": "v1",
                    "title": "Default Title",
                    "price": "18.00",
                    "inventory_quantity": 1,
                    "inventory_policy": "deny",
                    "tracks_inventory": True,
                    "available": True,
                }
            ],
        }
        replies: list[str] = []
        created: list[dict[str, Any]] = []

        async def fake_search(store_domain, access_token, buyer_text, limit=3):
            if "shirt" in buyer_text.lower() or "revenge" in buyer_text.lower():
                return {"status": "found", "query": buyer_text, "products": [product]}
            return {"status": "not_found", "products": []}

        async def capture_send(page_id, page_access_token, recipient_id, text):
            replies.append(text)
            return {"message_id": "out"}

        async def fake_create_order(store_domain, access_token, order):
            created.append(order)
            return {"id": 1, "name": "#1"}

        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=capture_send), \
             mock.patch.object(ms, "search_product_summaries", new=fake_search), \
             mock.patch.object(ms, "create_order", new=fake_create_order), \
             mock.patch.object(ms, "_refresh_shopify_token_if_needed", new=mock.AsyncMock(return_value="shpat_x")):
            await ms._process_incoming_message(
                _msg("m1", "please place an order for me. 2pc of The revenge t-shirt")
            )

        self.assertEqual(len(created), 0)
        self.assertIn("only order 1", replies[-1])
        self.assertNotIn("send your name", replies[-1])

    async def test_non_phone_shopify_failure_replies_instead_of_silence(self) -> None:
        send_mock = mock.AsyncMock(return_value={"message_id": "out"})

        async def fake_search(store_domain, access_token, buyer_text, limit=3):
            if "snowboard" in buyer_text.lower() or "liquid" in buyer_text.lower():
                return {"status": "found", "query": buyer_text, "products": [PRODUCT]}
            return {"status": "not_found", "products": []}

        async def failing_create_order(store_domain, access_token, order):
            raise ms.ShopifyAPIError("Shopify API returned 422: line_items invalid")

        with mock.patch.object(ms, "find_integration_by_meta_page_id", return_value=INTEGRATION), \
             mock.patch.object(ms, "send_messenger_text_reply", new=send_mock), \
             mock.patch.object(ms, "search_product_summaries", new=fake_search), \
             mock.patch.object(ms, "create_order", new=failing_create_order), \
             mock.patch.object(ms, "_refresh_shopify_token_if_needed", new=mock.AsyncMock(return_value="shpat_x")):
            for mid, text in [
                ("m1", "Do you have The Collection Snowboard: Liquid ?"),
                ("m2", "I want to order it"),
                ("m3", "My name is Kamrul\nMobile: 01766813937\nAddress: 233, Shenpara Parbata"),
                ("m4", "yes"),
            ]:
                await ms._process_incoming_message(_msg(mid, text))

        last_reply = send_mock.await_args_list[-1].kwargs["text"]
        self.assertIn("couldn't place the order", last_reply)


if __name__ == "__main__":
    unittest.main()

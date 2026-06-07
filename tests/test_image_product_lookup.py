"""Tests for image -> product visual matching in messaging_service.

Run with the project venv:

    .venv/bin/python -m unittest tests.test_image_product_lookup -v

These avoid network calls by monkeypatching the AI and Shopify service
functions that messaging_service imports.
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
    "shopify_store_domain": "test-store.myshopify.com",
    "shopify_access_token": "shpat_testtoken_123456",
}

SNOWBOARD = {
    "id": 111,
    "title": "The Collection Snowboard: Liquid",
    "handle": "collection-snowboard-liquid",
    "available": True,
    "price": "749.95",
    "variants": [{"id": 9001, "title": "Default Title", "price": "749.95", "available": True}],
    "images": ["https://cdn.example.com/liquid.jpg"],
}
OTHER_BOARD = {
    "id": 222,
    "title": "The Hidden Snowboard",
    "handle": "hidden-snowboard",
    "available": True,
    "price": "749.95",
    "variants": [{"id": 9002, "title": "Default Title", "price": "749.95", "available": True}],
    "images": ["https://cdn.example.com/hidden.jpg"],
}


class ImageProductLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts with clean module-level conversation state.
        ms._conversation_contexts.clear()
        ms._order_states.clear()
        self.context_key = "page1:buyer1"

    def _lookup(self, buyer_text: str = "Do you have this product?", image: bool = True):
        return _run(
            ms._maybe_lookup_products(
                integration=INTEGRATION,
                buyer_text=buyer_text,
                context_key=self.context_key,
                image_urls=["https://cdn.fb.com/buyer.jpg"] if image else [],
            )
        )

    def test_high_confidence_visual_match_auto_selects(self) -> None:
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "green snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "query": "green snowboard", "products": []}),
        ), mock.patch.object(
            ms, "list_product_summaries",
            new=mock.AsyncMock(return_value=[SNOWBOARD, OTHER_BOARD]),
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": 0, "confidence": "high", "reason": "clear match", "candidate": SNOWBOARD}),
        ):
            result = self._lookup()

        self.assertEqual(result["status"], "found")
        self.assertTrue(result.get("visual_match") is True)
        self.assertFalse(result.get("requires_confirmation"))
        self.assertEqual([p["title"] for p in result["products"]], [SNOWBOARD["title"]])
        # High match is committed to memory immediately.
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])
        # No reply-gate: high confidence lets the normal LLM path answer.
        self.assertIsNone(ms._image_lookup_uncertain_reply(result))

    def test_medium_confidence_asks_for_confirmation(self) -> None:
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "green snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "query": "green snowboard", "products": []}),
        ), mock.patch.object(
            ms, "list_product_summaries",
            new=mock.AsyncMock(return_value=[SNOWBOARD, OTHER_BOARD]),
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": 0, "confidence": "medium", "reason": "probable", "candidate": SNOWBOARD}),
        ):
            result = self._lookup()

        self.assertEqual(result["status"], "found")
        self.assertEqual(result.get("visual_match"), "medium")
        self.assertTrue(result.get("requires_confirmation"))
        # Surfaced as a single-product confirmation, not a dead end.
        reply = ms._image_lookup_uncertain_reply(result)
        self.assertIsNotNone(reply)
        self.assertIn(SNOWBOARD["title"], reply)
        self.assertIn("Is that the product", reply)
        # Held as pending, NOT yet committed.
        self.assertIsNone(ms._get_selected_product(self.context_key))
        self.assertEqual(ms._get_pending_product(self.context_key)["id"], SNOWBOARD["id"])

    def test_medium_then_yes_commits_pending_product(self) -> None:
        # First turn: medium guess -> pending.
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "green snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "query": "green snowboard", "products": []}),
        ), mock.patch.object(
            ms, "list_product_summaries",
            new=mock.AsyncMock(return_value=[SNOWBOARD, OTHER_BOARD]),
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": 0, "confidence": "medium", "reason": "probable", "candidate": SNOWBOARD}),
        ):
            self._lookup()

        # Second turn: buyer says "yes", no image. No AI/Shopify calls needed.
        result = self._lookup(buyer_text="yes", image=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["source"], "conversation_memory")
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])
        # Now committed; pending cleared.
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])
        self.assertIsNone(ms._get_pending_product(self.context_key))

    def test_no_visual_match_requires_confirmation_and_keeps_candidates(self) -> None:
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "green snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "query": "green snowboard", "products": []}),
        ), mock.patch.object(
            ms, "list_product_summaries",
            new=mock.AsyncMock(return_value=[SNOWBOARD, OTHER_BOARD]),
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": None, "confidence": "low", "reason": "no match", "candidate": None}),
        ):
            result = self._lookup()

        self.assertTrue(result.get("requires_confirmation"))
        self.assertNotEqual(result.get("visual_match"), True)
        self.assertIsNone(ms._get_selected_product(self.context_key))
        self.assertIsNone(ms._get_pending_product(self.context_key))

    def test_catalog_scanned_even_when_text_search_has_hits(self) -> None:
        # Text search returns a wrong board; catalog scan must still see the
        # right one and be allowed to win.
        list_mock = mock.AsyncMock(return_value=[SNOWBOARD, OTHER_BOARD])
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "found", "query": "snowboard", "products": [OTHER_BOARD]}),
        ), mock.patch.object(
            ms, "list_product_summaries", new=list_mock,
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": 0, "confidence": "high", "reason": "clear", "candidate": SNOWBOARD}),
        ):
            result = self._lookup()

        # Catalog list was consulted (the bug was that it was skipped on text hits).
        list_mock.assert_awaited()
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])

    def test_no_visual_match_does_not_list_arbitrary_catalog_products(self) -> None:
        # Bare "do you have this product?" + image, no visual match: must NOT
        # present unrelated catalog items (Gift Card, Ski Wax) as matches.
        gift_card = dict(SNOWBOARD, id=900, title="Gift Card", images=["https://x/gc.jpg"])
        ski_wax = dict(SNOWBOARD, id=901, title="Selling Plans Ski Wax", images=["https://x/wax.jpg"])
        with mock.patch.object(
            ms, "identify_product_from_image",
            new=mock.AsyncMock(return_value={"text": "snowboard", "confidence": "low", "vision": {}}),
        ), mock.patch.object(
            ms, "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "query": "Do you have this product?\nsnowboard", "products": []}),
        ), mock.patch.object(
            ms, "list_product_summaries",
            new=mock.AsyncMock(return_value=[gift_card, ski_wax]),
        ), mock.patch.object(
            ms, "choose_product_from_image_candidates",
            new=mock.AsyncMock(return_value={"selected_index": None, "confidence": "low", "reason": "no", "candidate": None}),
        ):
            result = self._lookup(buyer_text="Do you have this product?")

        # No fabricated candidate list leaks into products.
        self.assertEqual(result.get("products"), [])
        reply = ms._image_lookup_uncertain_reply(result)
        self.assertIsNotNone(reply)
        self.assertNotIn("Gift Card", reply)
        self.assertNotIn("Ski Wax", reply)
        self.assertIn("could not match", reply)

    def test_buyer_text_named_a_product(self) -> None:
        named = {"query": "do you have the collection snowboard\ngreen snowboard"}
        bare = {"query": "Do you have this product?\nsnowboard"}
        self.assertTrue(ms._buyer_text_named_a_product(named))
        self.assertFalse(ms._buyer_text_named_a_product(bare))

    def test_render_lookup_products_excludes_vision_internals(self) -> None:
        product_lookup = {
            "status": "found",
            "source": "image",
            "image_query": "green pixel snowboard winter",
            "image_vision": {"search_query": "Topcon RL-H5A rotary laser"},
            "products": [SNOWBOARD],
        }
        rendered = ms._render_lookup_products(product_lookup)
        self.assertIn(SNOWBOARD["title"], rendered)
        self.assertIn("749.95", rendered)
        # None of the vision/search internals may appear (hallucination source).
        self.assertNotIn("Topcon", rendered)
        self.assertNotIn("pixel", rendered)
        self.assertNotIn("image_query", rendered)

        context = ms._product_lookup_context(product_lookup)
        self.assertNotIn("Topcon", context)
        self.assertNotIn("image_vision", context)
        self.assertIn(SNOWBOARD["title"], context)

    def test_product_lookup_context_no_products_forbids_naming(self) -> None:
        context = ms._product_lookup_context(
            {"status": "not_found", "source": "image", "products": []}
        )
        self.assertIn("Do not name any product", context)


if __name__ == "__main__":
    unittest.main()

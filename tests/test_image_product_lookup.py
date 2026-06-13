"""Tests for Messenger image -> product lookup behavior.

Run with:

    .venv/bin/python -m unittest tests.test_image_product_lookup -v
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
    "id": "111",
    "title": "The Collection Snowboard: Liquid",
    "handle": "collection-snowboard-liquid",
    "available": True,
    "price": "749.95",
    "variants": [{"id": "9001", "title": "Default Title", "price": "749.95", "available": True}],
    "images": ["https://cdn.example.com/liquid.jpg"],
}
OTHER_BOARD = {
    "id": "222",
    "title": "The Hidden Snowboard",
    "handle": "hidden-snowboard",
    "available": True,
    "price": "749.95",
    "variants": [{"id": "9002", "title": "Default Title", "price": "749.95", "available": True}],
    "images": ["https://cdn.example.com/hidden.jpg"],
}


class ImageProductLookupTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def test_high_confidence_match_auto_selects(self) -> None:
        with mock.patch.object(
            ms,
            "_lookup_products_from_image",
            new=mock.AsyncMock(
                return_value={
                    "status": "found",
                    "source": "image",
                    "confidence": "high",
                    "top_score": 0.93,
                    "requires_confirmation": False,
                    "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.93}],
                    "products": [SNOWBOARD],
                }
            ),
        ):
            result = self._lookup()

        self.assertEqual(result["status"], "found")
        self.assertFalse(result.get("requires_confirmation"))
        self.assertEqual([p["title"] for p in result["products"]], [SNOWBOARD["title"]])
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])
        self.assertIsNone(ms._image_lookup_uncertain_reply(result))

    def test_medium_confidence_asks_for_confirmation(self) -> None:
        with mock.patch.object(
            ms,
            "_lookup_products_from_image",
            new=mock.AsyncMock(
                return_value={
                    "status": "found",
                    "source": "image",
                    "confidence": "medium",
                    "top_score": 0.76,
                    "requires_confirmation": True,
                    "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.76}],
                    "products": [SNOWBOARD],
                }
            ),
        ):
            result = self._lookup()

        self.assertEqual(result["status"], "found")
        self.assertTrue(result.get("requires_confirmation"))
        reply = ms._image_lookup_uncertain_reply(result)
        self.assertIsNotNone(reply)
        self.assertIn(SNOWBOARD["title"], reply)
        self.assertIn("Is that the product", reply)
        self.assertIsNone(ms._get_selected_product(self.context_key))
        self.assertEqual(ms._get_pending_product(self.context_key)["id"], SNOWBOARD["id"])

    def test_medium_then_yes_commits_pending_product(self) -> None:
        with mock.patch.object(
            ms,
            "_lookup_products_from_image",
            new=mock.AsyncMock(
                return_value={
                    "status": "found",
                    "source": "image",
                    "confidence": "medium",
                    "top_score": 0.76,
                    "requires_confirmation": True,
                    "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.76}],
                    "products": [SNOWBOARD],
                }
            ),
        ):
            self._lookup()

        result = self._lookup(buyer_text="yes", image=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["source"], "conversation_memory")
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])
        self.assertIsNone(ms._get_pending_product(self.context_key))

    def test_new_image_overrides_remembered_product(self) -> None:
        ms._remember_selected_product(self.context_key, OTHER_BOARD)
        matcher = mock.AsyncMock(
            return_value={
                "status": "found",
                "source": "image",
                "confidence": "high",
                "top_score": 0.93,
                "requires_confirmation": False,
                "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.93}],
                "products": [SNOWBOARD],
            }
        )

        with mock.patch.object(ms, "_lookup_products_from_image", new=matcher):
            result = self._lookup(buyer_text="What is the price of this product?")

        matcher.assert_awaited_once()
        self.assertEqual(result["source"], "image")
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])

    def test_medium_new_image_clears_old_selected_product(self) -> None:
        ms._remember_selected_product(self.context_key, OTHER_BOARD)
        matcher = mock.AsyncMock(
            return_value={
                "status": "found",
                "source": "image",
                "confidence": "medium",
                "top_score": 0.76,
                "requires_confirmation": True,
                "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.76}],
                "products": [SNOWBOARD],
            }
        )

        with mock.patch.object(ms, "_lookup_products_from_image", new=matcher):
            result = self._lookup(buyer_text="Is this available?")

        self.assertEqual(result["source"], "image")
        self.assertIsNone(ms._get_selected_product(self.context_key))
        self.assertEqual(ms._get_pending_product(self.context_key)["id"], SNOWBOARD["id"])

    def test_low_confidence_lists_only_retrieved_candidates(self) -> None:
        with mock.patch.object(
            ms,
            "_lookup_products_from_image",
            new=mock.AsyncMock(
                return_value={
                    "status": "found",
                    "source": "image",
                    "confidence": "low",
                    "top_score": 0.61,
                    "requires_confirmation": True,
                    "matches": [
                        {"shopify_product_id": SNOWBOARD["id"], "score": 0.61},
                        {"shopify_product_id": OTHER_BOARD["id"], "score": 0.58},
                    ],
                    "products": [SNOWBOARD, OTHER_BOARD],
                }
            ),
        ):
            result = self._lookup()

        self.assertTrue(result.get("requires_confirmation"))
        self.assertIsNone(ms._get_selected_product(self.context_key))
        reply = ms._image_lookup_uncertain_reply(result)
        self.assertIn(SNOWBOARD["title"], reply)
        self.assertIn(OTHER_BOARD["title"], reply)

    def test_recent_image_can_be_reused_on_try_again(self) -> None:
        ms._remember_recent_image_urls(self.context_key, ["https://cdn.fb.com/last.jpg"])
        self.assertEqual(ms._get_recent_image_urls(self.context_key), ["https://cdn.fb.com/last.jpg"])
        self.assertTrue(ms._refers_to_recent_image_request("can you try again?"))

    def test_missing_image_attachment_reply_is_deterministic(self) -> None:
        reply = ms._missing_image_attachment_reply(
            {"text": "I have provided the image", "image_urls": []},
            None,
        )
        self.assertIsNotNone(reply)
        self.assertIn("could not receive the photo attachment", reply)

    def test_render_lookup_products_excludes_vision_internals(self) -> None:
        product_lookup = {
            "status": "found",
            "source": "image",
            "vision_query": "Topcon RL-H5A rotary laser",
            "matches": [{"shopify_product_id": SNOWBOARD["id"], "score": 0.93}],
            "products": [SNOWBOARD],
        }
        rendered = ms._render_lookup_products(product_lookup)
        self.assertIn(SNOWBOARD["title"], rendered)
        self.assertIn("749.95", rendered)
        self.assertNotIn("Topcon", rendered)
        self.assertNotIn("vision_query", rendered)

        context = ms._product_lookup_context(product_lookup)
        self.assertNotIn("Topcon", context)
        self.assertIn(SNOWBOARD["title"], context)

    def test_product_lookup_context_no_products_forbids_naming(self) -> None:
        context = ms._product_lookup_context(
            {"status": "not_found", "source": "image", "products": []}
        )
        self.assertIn("Do not name any product", context)

    def test_needs_clarification_asks_proactive_question(self) -> None:
        with mock.patch.object(
            ms,
            "identify_product_from_image",
            new=mock.AsyncMock(
                return_value={
                    "text": "a shoe",
                    "confidence": "low",
                    "vision": {
                        "needs_more_info": True,
                        "follow_up_question": "What brand and color is the shoe?",
                    },
                }
            ),
        ):
            result = self._lookup(buyer_text="how much is this?")

        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["source"], "image")
        self.assertTrue(ms._is_awaiting_image_clarification(self.context_key))
        reply = ms._image_clarification_reply(result)
        self.assertEqual(reply, "What brand and color is the shoe?")
        self.assertIsNone(ms._get_selected_product(self.context_key))

    def test_vision_first_text_search_high_confidence_auto_selects(self) -> None:
        search = mock.AsyncMock(
            return_value={"status": "found", "query": "snowboard", "products": [SNOWBOARD]}
        )
        with mock.patch.object(
            ms,
            "identify_product_from_image",
            new=mock.AsyncMock(
                return_value={
                    "text": "the collection snowboard liquid",
                    "confidence": "high",
                    "vision": {"needs_more_info": False, "follow_up_question": ""},
                }
            ),
        ), mock.patch.object(ms, "search_product_summaries", new=search):
            result = self._lookup(buyer_text="how much is this?")

        search.assert_awaited_once()
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["source"], "image")
        self.assertFalse(result.get("requires_confirmation"))
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])
        self.assertEqual(ms._get_selected_product(self.context_key)["id"], SNOWBOARD["id"])
        self.assertFalse(ms._is_awaiting_image_clarification(self.context_key))

    def test_text_search_miss_uses_visual_fallback(self) -> None:
        with mock.patch.object(
            ms,
            "identify_product_from_image",
            new=mock.AsyncMock(
                return_value={
                    "text": "a snowboard",
                    "confidence": "low",
                    "vision": {"needs_more_info": False, "follow_up_question": ""},
                }
            ),
        ), mock.patch.object(
            ms,
            "search_product_summaries",
            new=mock.AsyncMock(return_value={"status": "not_found", "products": []}),
        ), mock.patch.object(
            ms,
            "list_product_summaries",
            new=mock.AsyncMock(return_value=[SNOWBOARD]),
        ), mock.patch.object(
            ms,
            "choose_product_from_image_candidates",
            new=mock.AsyncMock(
                return_value={"selected_index": 0, "confidence": "high", "candidate": SNOWBOARD}
            ),
        ):
            result = self._lookup(buyer_text="do you have this?")

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["match_source"], "image_visual_match")
        self.assertEqual(result["products"][0]["id"], SNOWBOARD["id"])


if __name__ == "__main__":
    unittest.main()

"""Tests for catalog text ranking (the live Shopify product matcher).

Run with:

    .venv/bin/python -m unittest tests.test_product_ranking -v

These guard against the "can't detect a listed product" failures: matching must
read product_type/tags/vendor (not just the title) and tolerate plurals, using
the merchant's own Shopify metadata rather than any hardcoded category words.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.shopify_service as ss


TSHIRT = {
    "id": "1",
    "title": "The revenge t-shirt",
    "handle": "the-revenge-t-shirt",
    "product_type": "T-Shirts",
    "vendor": "binga",
    "tags": "",
    "variants": [{"id": "v1", "title": "Default Title", "price": "20.00"}],
}
SNOWBOARD = {
    "id": "3",
    "title": "The Collection Snowboard: Liquid",
    "handle": "collection-liquid",
    "product_type": "snowboard",
    "vendor": "Hydrogen Vendor",
    "tags": "winter",
    "variants": [{"id": "v3", "title": "Default Title", "price": "749.95"}],
}
CATALOG = [TSHIRT, SNOWBOARD]


def _titles(products):
    return [p["title"] for p in products]


class ProductRankingTests(unittest.TestCase):
    def test_category_question_matches_via_product_type(self) -> None:
        # "t shirts" is nowhere in the title; it must match the T-Shirts type.
        self.assertIn(TSHIRT, ss._rank_products(CATALOG, "Do you have any t shirts?"))

    def test_plural_matches_singular_title_token(self) -> None:
        ranked = ss._rank_products(CATALOG, "do you have any snowboards?")
        self.assertIn(SNOWBOARD, ranked)

    def test_exact_name_in_quotes_matches(self) -> None:
        ranked = ss._rank_products(CATALOG, "Do you have the 'The revenge t-shirt' ?")
        self.assertEqual(_titles(ranked)[0], TSHIRT["title"])

    def test_unknown_product_returns_nothing(self) -> None:
        self.assertEqual(ss._rank_products(CATALOG, "do you sell mugs?"), [])

    def test_conversational_words_do_not_match_products(self) -> None:
        # Pure filler must not spuriously match any product.
        self.assertEqual(ss._rank_products(CATALOG, "do you have any?"), [])

    def test_prefix_token_helper(self) -> None:
        self.assertTrue(ss._tokens_match("shirt", "shirts"))
        self.assertTrue(ss._tokens_match("snowboards", "snowboard"))
        self.assertFalse(ss._tokens_match("you", "yellow"))


if __name__ == "__main__":
    unittest.main()

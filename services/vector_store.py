from __future__ import annotations

import math
from typing import Any, Optional

from psycopg2.extras import Json

from database import get_connection

_HAS_VECTOR_COLUMN: Optional[bool] = None


def embedding_exists(
    user_id: int,
    shopify_product_id: str,
    shopify_variant_id: Optional[str],
    image_url: str,
    embedding_model: str,
    embedding_dimension: int,
) -> bool:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM product_image_embeddings
                WHERE user_id = %s
                  AND shopify_product_id = %s
                  AND COALESCE(shopify_variant_id, '') = COALESCE(%s, '')
                  AND image_url = %s
                  AND embedding_model = %s
                  AND embedding_dimension = %s
                LIMIT 1
                """,
                (
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    image_url,
                    embedding_model,
                    embedding_dimension,
                ),
            )
            return cursor.fetchone() is not None


def upsert_image_embedding(
    user_id: int,
    shopify_product_id: str,
    shopify_variant_id: Optional[str],
    shopify_image_id: Optional[str],
    image_url: str,
    embedding: list[float],
    embedding_model: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    dimension = len(embedding)
    has_vector = has_pgvector_column()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM product_image_embeddings
                WHERE user_id = %s
                  AND shopify_product_id = %s
                  AND COALESCE(shopify_variant_id, '') = COALESCE(%s, '')
                  AND image_url = %s
                  AND embedding_model = %s
                  AND embedding_dimension = %s
                """,
                (
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    image_url,
                    embedding_model,
                    dimension,
                ),
            )
            if has_vector:
                cursor.execute(
                    """
                    INSERT INTO product_image_embeddings (
                        user_id,
                        shopify_product_id,
                        shopify_variant_id,
                        shopify_image_id,
                        image_url,
                        embedding_model,
                        embedding_dimension,
                        embedding,
                        embedding_values,
                        metadata,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, NOW())
                    """,
                    (
                        user_id,
                        shopify_product_id,
                        shopify_variant_id,
                        shopify_image_id,
                        image_url,
                        embedding_model,
                        dimension,
                        _pgvector_literal(embedding),
                        Json(embedding),
                        Json(metadata or {}),
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO product_image_embeddings (
                        user_id,
                        shopify_product_id,
                        shopify_variant_id,
                        shopify_image_id,
                        image_url,
                        embedding_model,
                        embedding_dimension,
                        embedding_values,
                        metadata,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        user_id,
                        shopify_product_id,
                        shopify_variant_id,
                        shopify_image_id,
                        image_url,
                        embedding_model,
                        dimension,
                        Json(embedding),
                        Json(metadata or {}),
                    ),
                )


def search_similar_image_embeddings(
    user_id: int,
    embedding: list[float],
    limit: int,
    embedding_model: str,
) -> list[dict[str, Any]]:
    if has_pgvector_column():
        return _search_with_pgvector(user_id, embedding, limit, embedding_model)
    return _search_with_jsonb(user_id, embedding, limit, embedding_model)


def has_pgvector_column() -> bool:
    global _HAS_VECTOR_COLUMN
    if _HAS_VECTOR_COLUMN is not None:
        return _HAS_VECTOR_COLUMN

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'product_image_embeddings'
                  AND column_name = 'embedding'
                LIMIT 1
                """
            )
            _HAS_VECTOR_COLUMN = cursor.fetchone() is not None
    return _HAS_VECTOR_COLUMN


def _search_with_pgvector(
    user_id: int,
    embedding: list[float],
    limit: int,
    embedding_model: str,
) -> list[dict[str, Any]]:
    vector_literal = _pgvector_literal(embedding)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    shopify_image_id,
                    image_url,
                    embedding_model,
                    embedding_dimension,
                    metadata,
                    1 - (embedding <=> %s::vector) AS score
                FROM product_image_embeddings
                WHERE user_id = %s
                  AND embedding_model = %s
                  AND embedding_dimension = %s
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    vector_literal,
                    user_id,
                    embedding_model,
                    len(embedding),
                    vector_literal,
                    limit,
                ),
            )
            rows = cursor.fetchall()
    return [dict(row) for row in rows]


def _search_with_jsonb(
    user_id: int,
    embedding: list[float],
    limit: int,
    embedding_model: str,
) -> list[dict[str, Any]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    user_id,
                    shopify_product_id,
                    shopify_variant_id,
                    shopify_image_id,
                    image_url,
                    embedding_model,
                    embedding_dimension,
                    embedding_values,
                    metadata
                FROM product_image_embeddings
                WHERE user_id = %s
                  AND embedding_model = %s
                  AND embedding_dimension = %s
                """,
                (user_id, embedding_model, len(embedding)),
            )
            rows = [dict(row) for row in cursor.fetchall()]

    scored = []
    for row in rows:
        stored = row.pop("embedding_values", [])
        score = _cosine_similarity(embedding, [float(value) for value in stored])
        row["score"] = score
        scored.append(row)

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def _pgvector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)

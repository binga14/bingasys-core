from __future__ import annotations

import base64
from typing import Any

import httpx

from config import settings
from services.ai_service import GeminiAPIError, GeminiConfigurationError


async def generate_image_embedding_from_url(image_url: str) -> dict[str, Any]:
    image_bytes, mime_type = await _download_image(image_url)
    return await generate_image_embedding_from_bytes(image_bytes, mime_type)


async def generate_image_embedding_from_bytes(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise GeminiConfigurationError("GEMINI_API_KEY is not configured")

    model = settings.gemini_embedding_model
    payload = {
        "model": f"models/{model}",
        "content": {
            "parts": [
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            ]
        },
        "embedContentConfig": {
            "outputDimensionality": settings.gemini_embedding_dimensions,
        },
    }
    url = (
        f"{settings.gemini_api_base_url.rstrip('/')}"
        f"/models/{model}:embedContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.gemini_api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GeminiAPIError(_format_gemini_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        raise GeminiAPIError(f"Gemini embedding request failed: {exc}") from exc

    data = response.json()
    values = ((data.get("embedding") or {}).get("values") or [])
    if not values:
        raise GeminiAPIError("Gemini embedding response did not include vector values")

    embedding = [float(value) for value in values]
    return {
        "embedding": embedding,
        "model": model,
        "dimension": len(embedding),
        "raw": data,
    }


async def _download_image(image_url: str) -> tuple[bytes, str]:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(image_url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GeminiAPIError(f"Image download failed: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise GeminiAPIError(f"Image download failed: {exc}") from exc

    content = response.content
    if len(content) > settings.product_image_download_max_bytes:
        raise GeminiAPIError("Image is too large to process")

    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    return content, mime_type


def _format_gemini_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"Gemini API returned {response.status_code}: {detail}"

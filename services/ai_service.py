from __future__ import annotations

import base64
import json
from typing import Any, Optional

import httpx

from config import settings


DEFAULT_SALES_ASSISTANT_INSTRUCTION = """
You are an AI sales assistant for one e-commerce business.
Help buyers over Messenger or Instagram DM with concise, friendly replies.
Identify product intent, ask for missing order details, and never claim an
order has been created unless the backend confirms it through a Shopify tool.
If inventory, pricing, shipping, or order creation requires live data, ask the
orchestration layer to use the proper tool instead of guessing.
""".strip()


class GeminiConfigurationError(RuntimeError):
    pass


class GeminiAPIError(RuntimeError):
    pass


async def generate_sales_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return await call_gemini(
        messages=messages,
        system_instruction=DEFAULT_SALES_ASSISTANT_INSTRUCTION,
    )


async def identify_product_from_image(
    image_url: str,
    buyer_text: str = "",
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise GeminiConfigurationError("GEMINI_API_KEY is not configured")

    image_bytes, mime_type = await _download_image(image_url)
    prompt = (
        "Identify the ecommerce product in this image for Shopify catalog matching. "
        "Return only compact JSON with these keys: product_type, colors, "
        "visual_features, readable_text, search_query, confidence. "
        "confidence must be one of high, medium, or low. "
        "The search_query should be a short phrase using only visible attributes. "
        "Do not invent a brand or exact Shopify product title unless text in the image "
        "clearly shows it."
    )
    if buyer_text.strip():
        prompt += f" Buyer message: {buyer_text.strip()}"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
        },
    }
    url = (
        f"{settings.gemini_api_base_url.rstrip('/')}"
        f"/models/{settings.gemini_vision_model}:generateContent"
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
        raise GeminiAPIError(f"Gemini image request failed: {exc}") from exc

    data = response.json()
    text = _extract_text(data)
    parsed = _parse_json_object(text)
    search_query = str(parsed.get("search_query") or text).strip()
    return {
        "text": search_query,
        "confidence": str(parsed.get("confidence") or "").lower(),
        "vision": parsed,
        "model": settings.gemini_vision_model,
        "raw": data,
    }


async def choose_product_from_image_candidates(
    buyer_image_url: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise GeminiConfigurationError("GEMINI_API_KEY is not configured")

    usable_candidates = [
        candidate
        for candidate in candidates[:6]
        if (candidate.get("images") or [None])[0]
    ]
    if not usable_candidates:
        return {"selected_index": None, "confidence": "low", "reason": "No candidate images"}

    buyer_bytes, buyer_mime = await _download_image(buyer_image_url)
    parts: list[dict[str, Any]] = [
        {
            "text": (
                "Compare the buyer image to these Shopify product candidate images. "
                "Return only JSON with selected_index, confidence, and reason. "
                "selected_index must be the zero-based index from the candidate list, "
                "or null if no candidate clearly matches. confidence must be high, "
                "medium, or low. Select high only when the product image clearly matches."
            )
        },
        {
            "text": "Buyer image:"
        },
        {
            "inlineData": {
                "mimeType": buyer_mime,
                "data": base64.b64encode(buyer_bytes).decode("ascii"),
            }
        },
    ]

    for index, candidate in enumerate(usable_candidates):
        image_url = (candidate.get("images") or [None])[0]
        try:
            image_bytes, mime_type = await _download_image(str(image_url))
        except GeminiAPIError:
            continue
        parts.extend(
            [
                {"text": f"Candidate {index}: {candidate.get('title')}"},
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                },
            ]
        )

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 128,
            "responseMimeType": "application/json",
        },
    }
    url = (
        f"{settings.gemini_api_base_url.rstrip('/')}"
        f"/models/{settings.gemini_vision_model}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.gemini_api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GeminiAPIError(_format_gemini_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        raise GeminiAPIError(f"Gemini image candidate request failed: {exc}") from exc

    parsed = _parse_json_object(_extract_text(response.json()))
    selected_index = parsed.get("selected_index")
    if not isinstance(selected_index, int) or selected_index < 0 or selected_index >= len(usable_candidates):
        selected_index = None
    return {
        "selected_index": selected_index,
        "confidence": str(parsed.get("confidence") or "").lower(),
        "reason": str(parsed.get("reason") or ""),
        "candidate": usable_candidates[selected_index] if selected_index is not None else None,
    }


async def call_gemini(
    messages: list[dict[str, Any]],
    system_instruction: Optional[str] = None,
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise GeminiConfigurationError("GEMINI_API_KEY is not configured")

    message_system_instructions, contents = _build_gemini_contents(messages)
    system_instructions = [
        instruction
        for instruction in [system_instruction, *message_system_instructions]
        if instruction
    ]

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": settings.gemini_temperature,
            "maxOutputTokens": settings.gemini_max_output_tokens,
        },
    }
    if system_instructions:
        payload["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_instructions)}],
        }

    url = (
        f"{settings.gemini_api_base_url.rstrip('/')}"
        f"/models/{settings.gemini_model}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.gemini_api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GeminiAPIError(_format_gemini_error(exc.response)) from exc
    except httpx.HTTPError as exc:
        raise GeminiAPIError(f"Gemini request failed: {exc}") from exc

    data = response.json()
    return {
        "text": _extract_text(data),
        "model": settings.gemini_model,
        "raw": data,
    }


def _build_gemini_contents(
    messages: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    system_instructions: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role", "user")).lower()
        text = _message_text(message)
        if not text:
            continue

        if role == "system":
            system_instructions.append(text)
            continue

        contents.append(
            {
                "role": "model" if role in {"assistant", "model"} else "user",
                "parts": [{"text": text}],
            }
        )

    if not contents:
        raise ValueError("At least one non-system message is required")

    return system_instructions, contents


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", message.get("text", message.get("message", "")))
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts).strip()
    return str(content).strip() if content is not None else ""


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""

    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "".join(text_parts).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        clean = clean.removeprefix("json").strip()
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            value = json.loads(clean[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _format_gemini_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"Gemini API returned {response.status_code}: {detail}"


async def _download_image(image_url: str) -> tuple[bytes, str]:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(image_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GeminiAPIError(f"Image download failed: {exc}") from exc

    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    return response.content, mime_type

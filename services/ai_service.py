from __future__ import annotations

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


def _format_gemini_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return f"Gemini API returned {response.status_code}: {detail}"

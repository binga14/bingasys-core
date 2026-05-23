from __future__ import annotations

from typing import Any, Optional


def verify_webhook(
    mode: Optional[str],
    verify_token: Optional[str],
    expected_token: Optional[str],
) -> bool:
    return mode == "subscribe" and verify_token is not None and verify_token == expected_token


async def handle_webhook_placeholder(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "status": "received",
        "message": "Meta webhook handling will be implemented later.",
    }

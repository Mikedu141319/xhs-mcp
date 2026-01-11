"""Helpers for persisting Chrome cookies to disk."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Any, Dict

from loguru import logger

from src.config import COOKIES_FILE


def persist_cookies(cookies: Iterable[Mapping[str, object]], target_path: Path = COOKIES_FILE) -> Optional[str]:
    """
    Write the cookies returned from Chrome DevTools to ``data/cookies.json``.

    Args:
        cookies: Raw cookie dictionaries returned by ``Network.getAllCookies``.
        target_path: Override output path (primarily useful for tests).

    Returns:
        Absolute string path when data is written, else None.
    """
    cookie_list = list(cookies)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(cookie_list),
        "cookies": cookie_list,
    }

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        resolved = str(target_path.resolve())
        logger.info("Persisted {} cookies to {}", len(cookie_list), resolved)
        return resolved
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist cookies: {}", exc)
        return None


def load_cookies(source_path: Path = COOKIES_FILE) -> Optional[Dict[str, Any]]:
    """
    Read previously exported cookies from disk.

    Returns:
        Parsed payload with ``cookies`` list, or None if unavailable/invalid.
    """
    if not source_path.exists():
        return None
    try:
        with source_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        cookies = data.get("cookies") or []
        if not isinstance(cookies, list):
            logger.warning("Cookie payload malformed: {}", source_path)
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load cookies from {}: {}", source_path, exc)
        return None

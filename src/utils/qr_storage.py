"""Helper utilities to persist QR code images."""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from src.config import QR_DIR


def save_qr_image_from_base64(data_url: str) -> Optional[str]:
    """
    Persist a base64 encoded QR image to disk.

    Args:
        data_url: Base64 payload, optionally prefixed with ``data:image/...``.

    Returns:
        The absolute file path if the image was written successfully, else None.
    """
    try:
        if "," in data_url:
            _, base64_payload = data_url.split(",", 1)
        else:
            base64_payload = data_url

        image_bytes = base64.b64decode(base64_payload)

        timestamp = int(time.time())
        filename = f"qr_{timestamp}.png"
        file_path = QR_DIR / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "wb") as f:
            f.write(image_bytes)

        logger.info(f"QR image saved to {file_path}")
        return str(file_path.resolve())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to persist QR image: {exc}")
        return None

"""Pydantic models for login responses."""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class LoginStatus(BaseModel):
    state: str = Field(..., description="logged_in / needs_qr_scan / captcha_gate / browser_offline / unknown")
    message: str
    qr_image_url: Optional[str] = None
    qr_base64: Optional[str] = None
    qr_code_file: Optional[str] = Field(
        default=None,
        description="Path to the saved QR image on disk, if generated",
    )
    captcha_screenshot_file: Optional[str] = Field(
        default=None,
        description="Path to the saved captcha screenshot, if captured",
    )
    full_page_screenshot_file: Optional[str] = Field(
        default=None,
        description="Full-page screenshot saved when verification is required",
    )
    next_actions: List[str] = Field(
        default_factory=list,
        description="Suggested next actions for the operator/user",
    )
    diagnostics: List[str] = Field(default_factory=list)


class LoginStatusResponse(BaseModel):
    success: bool
    status: LoginStatus


class LoginAssistantResponse(BaseModel):
    """Structured guidance returned by the conversational login helper."""

    success: bool
    state: str = Field(description="Latest login state detected")
    message: str
    next_hint: str
    captcha_file: Optional[str] = None
    captcha_file_url: Optional[str] = None
    qr_code_file: Optional[str] = None
    qr_code_file_url: Optional[str] = None
    qr_base64: Optional[str] = Field(
        default=None,
        description="Base64 encoded QR code image for direct display in N8N"
    )
    full_page_file: Optional[str] = None
    full_page_file_url: Optional[str] = None
    diagnostics: List[str] = Field(default_factory=list)

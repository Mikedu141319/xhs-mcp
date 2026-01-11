"""Schemas for the one-click automation workflow."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from src.schemas.login import LoginStatus
from src.schemas.note import NoteDetailBatchResponse
from src.schemas.search import (
    LocationScope,
    NoteType,
    PublishTime,
    SearchRequest,
    SearchResponse,
    SearchScope,
    SortOption,
)


AutoWorkflowStage = Literal["login", "search", "collect", "complete"]


class AutoWorkflowRequest(BaseModel):
    """User input for the automatic prepare + collect workflow."""

    keyword: str = Field(..., description="Keyword entered into Xiaohongshu search bar")
    sort_by: SortOption = Field(default="comprehensive", description="Sorting preference")
    note_type: NoteType = Field(default="all", description="Note type filter")
    publish_time: PublishTime = Field(default="any", description="Publish time filter")
    search_scope: SearchScope = Field(default="all", description="Search scope filter")
    location: LocationScope = Field(default="all", description="Location filter")
    note_limit: int = Field(default=20, ge=1, le=200, description="Number of notes to collect")
    login_retry_limit: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum retries for waiting on QR scan / captcha verification",
    )
    login_retry_interval: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Seconds between login re-checks when waiting for manual action",
    )
    auto_retry_after_login: bool = Field(
        default=True,
        description="Whether to re-run search/collect after a fresh login attempt",
    )

    def to_search_request(self) -> SearchRequest:
        """Translate to the existing SearchRequest schema."""
        return SearchRequest(
            keyword=self.keyword,
            sort_by=self.sort_by,
            note_type=self.note_type,
            publish_time=self.publish_time,
            search_scope=self.search_scope,
            location=self.location,
            note_limit=self.note_limit,
        )


class AutoWorkflowResponse(BaseModel):
    """Aggregated output of the automatic workflow."""

    success: bool
    stage: AutoWorkflowStage = Field(description="Stage where the workflow completed or failed")
    message: str
    login_status: Optional[LoginStatus] = None
    search_result: Optional[SearchResponse] = None
    note_result: Optional[NoteDetailBatchResponse] = None
    diagnostics: list[str] = Field(default_factory=list)

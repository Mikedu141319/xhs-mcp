"""Pydantic models for author-related operations."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from src.schemas.note import NoteDetail


class AuthorInfo(BaseModel):
    """Basic author information extracted from profile page."""
    author_id: str = Field(..., description="Unique author ID")
    author_name: str = Field("", description="Author display name")
    author_avatar: Optional[str] = Field(default=None, description="Author avatar URL")
    total_notes: int = Field(0, description="Total number of notes displayed on profile")
    followers: int = Field(0, description="Follower count")
    following: int = Field(0, description="Following count")
    likes_and_collects: int = Field(0, description="Total likes and collects received")


class AuthorNotesRequest(BaseModel):
    """Request model for collecting author notes."""
    author_url: str = Field(..., description="Author profile URL")
    skip_note_ids: List[str] = Field(default_factory=list, description="Note IDs to skip (already collected)")
    note_limit: int = Field(50, description="Maximum notes to collect in this batch")


class AuthorNotesResponse(BaseModel):
    """Response model for author notes collection."""
    success: bool = Field(..., description="Whether the operation was successful")
    message: str = Field("", description="Status message")
    
    # Author info
    author_id: str = Field("", description="Author ID extracted from URL")
    author_name: str = Field("", description="Author display name")
    author_avatar: Optional[str] = Field(default=None, description="Author avatar URL")
    
    # Progress tracking
    total_notes_on_page: int = Field(0, description="Total notes shown on author's profile")
    already_collected: int = Field(0, description="Number of notes already collected (from skip_note_ids)")
    new_collected: int = Field(0, description="Number of new notes collected in this batch")
    skipped_count: int = Field(0, description="Number of notes skipped due to deduplication")
    has_more: bool = Field(False, description="Whether there are more notes to collect")
    
    # Collected notes
    notes: List[NoteDetail] = Field(default_factory=list, description="Collected note details")
    
    # Diagnostics
    diagnostics: List[str] = Field(default_factory=list, description="Diagnostic info for debugging")

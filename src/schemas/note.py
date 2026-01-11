"""Pydantic models for Xiaohongshu note content."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class Comment(BaseModel):
    id: str = Field(..., description="Comment ID")
    user_id: str = Field(..., description="User ID of the commenter")
    nickname: str = Field(..., description="Nickname of the commenter")
    content: str = Field(..., description="Comment content")
    likes: int = Field(0, description="Like count")
    create_time: int = Field(0, description="Creation timestamp (ms)")
    sub_comments: List[Comment] = Field(default_factory=list, description="Replies to this comment")
    parent_id: Optional[str] = Field(default=None, description="Parent comment ID if this is a reply")


class NoteDetail(BaseModel):
    note_id: str = Field(..., description="Unique ID of the note")
    title: str = Field(..., description="Note title")
    author: str = Field(..., description="Author nickname")
    author_id: Optional[str] = Field(default=None, description="Author ID if available")
    content: str = Field("", description="Plain text body extracted from the note")
    images: List[str] = Field(default_factory=list, description="Image URLs (max 50)")
    videos: List[str] = Field(default_factory=list, description="Video URLs (max 50)")
    like_count: int = Field(0, description="Number of likes")
    collect_count: int = Field(0, description="Number of collections")
    comment_count: int = Field(0, description="Number of comments")
    share_count: int = Field(0, description="Number of shares/forwards")
    publish_time: Optional[str] = Field(default=None, description="Publish date in YYYY-MM-DD")
    location: Optional[str] = Field(default=None, description="IP/location label when present")
    tags: List[str] = Field(default_factory=list, description="Hashtags detected in the note")
    note_url: str = Field(..., description="Canonical note URL (with parameters)")
    comments: List[Comment] = Field(default_factory=list, description="Collected comments")
    hot_comments_summary: Optional[str] = Field(default=None, description="Summary of top comments for AI")
    captured_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when the note data was extracted",
    )


class NoteDetailBatchResponse(BaseModel):
    success: bool
    message: str
    notes: List[NoteDetail] = Field(default_factory=list)
    diagnostics: List[str] = Field(default_factory=list)

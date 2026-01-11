"""Schemas for search and filter operations."""
from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field


SortOption = Literal["comprehensive", "latest", "most_liked", "most_collected", "most_commented", "most_comment", "comment_count"]
NoteType = Literal["all", "video", "image"]
PublishTime = Literal["any", "day", "week", "half_year", "within_a_week"]
SearchScope = Literal["all", "seen", "unseen", "following"]
LocationScope = Literal["all", "same_city", "nearby"]


class SearchRequest(BaseModel):
    keyword: str = Field(..., description="Keyword entered into Xiaohongshu search bar")
    sort_by: SortOption = Field(default="comprehensive", description="Sorting preference in the filter panel")
    note_type: NoteType = Field(default="all", description="笔记类型：不限/视频/图文")
    publish_time: PublishTime = Field(default="any", description="发布时间范围：不限/一天内/一周内/半年内")
    search_scope: SearchScope = Field(default="all", description="搜索范围：不限/已看过/未看过/已关注")
    location: LocationScope = Field(default="all", description="位置距离：不限/同城/附近")
    note_limit: int = Field(default=20, ge=1, le=200, description="后续需要抓取的笔记数量（本步骤不会抓取内容）")


class SearchResponse(BaseModel):
    success: bool
    message: str
    page_url: Optional[str] = None
    applied_filters: Dict[str, str] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)

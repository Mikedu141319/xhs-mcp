"""Service responsible for collecting notes from a specific author's profile page."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import List, Optional, Set

from loguru import logger

from src.clients.chrome_devtools import ChromeDevToolsClient
from src.config import chrome_entry_url
from src.schemas.author import AuthorInfo, AuthorNotesResponse
from src.schemas.note import NoteDetail, Comment


# Timing constants (similar to note_service)
PANEL_SETTLE_DELAY_SECONDS = 0.5
NOTE_OPEN_DELAY_RANGE = (1.5, 2.5)
NOTE_RETURN_DELAY_RANGE = (1.5, 2.5)
SCROLL_DELAY_RANGE = (1.5, 2.5)


def extract_author_id(url: str) -> Optional[str]:
    """Extract author_id from a Xiaohongshu profile URL.
    
    Examples:
        https://www.xiaohongshu.com/user/profile/6879c40600000001d02ce9c7?xsec_token=...
        -> 6879c40600000001d02ce9c7
    """
    if not url:
        return None
    
    # Match /user/profile/{author_id} pattern
    match = re.search(r'/user/profile/([a-f0-9]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


# JavaScript to extract author info from profile page
AUTHOR_INFO_SCRIPT = """
(() => {
    const data = {
        author_name: '',
        author_avatar: '',
        total_notes: 0,
        followers: 0,
        following: 0,
        likes_and_collects: 0
    };
    
    try {
        // Author name
        const nameEl = document.querySelector('.user-name, .username, .info-part .name');
        if (nameEl) data.author_name = nameEl.innerText.trim();
        
        // Avatar
        const avatarEl = document.querySelector('.avatar img, .user-avatar img, .info-part img');
        if (avatarEl) data.author_avatar = avatarEl.src;
        
        // Stats container (usually has follower/following/notes counts)
        const statsEls = document.querySelectorAll('.user-info .info-number, .data-info .count, .info-part .count');
        const parseCount = (text) => {
            if (!text) return 0;
            text = text.trim();
            if (text.includes('万')) {
                return Math.floor(parseFloat(text.replace('万', '')) * 10000);
            }
            return parseInt(text) || 0;
        };
        
        // Try to find specific stats
        const followingEl = document.querySelector('[data-type="following"] .count, .following .count');
        const followersEl = document.querySelector('[data-type="followers"] .count, .followers .count');
        const likesEl = document.querySelector('[data-type="likes"] .count, .likes .count');
        
        if (followingEl) data.following = parseCount(followingEl.innerText);
        if (followersEl) data.followers = parseCount(followersEl.innerText);
        if (likesEl) data.likes_and_collects = parseCount(likesEl.innerText);
        
        // Alternative: parse from text like "1 关注 695 粉丝 9.3万 获赞与收藏"
        const statsText = document.querySelector('.user-info, .data-info')?.innerText || '';
        const followingMatch = statsText.match(/([\d.]+万?)\s*关注/);
        const followersMatch = statsText.match(/([\d.]+万?)\s*粉丝/);
        const likesMatch = statsText.match(/([\d.]+万?)\s*获赞/);
        
        if (followingMatch && !data.following) data.following = parseCount(followingMatch[1]);
        if (followersMatch && !data.followers) data.followers = parseCount(followersMatch[1]);
        if (likesMatch && !data.likes_and_collects) data.likes_and_collects = parseCount(likesMatch[1]);
        
        // Total notes - look for tab with "笔记" and a number
        const noteTabEl = document.querySelector('.tabs [data-type="note"], .tab-item.active, .tabs .tab:first-child');
        if (noteTabEl) {
            const tabText = noteTabEl.innerText;
            const noteMatch = tabText.match(/笔记\\s*(?:\\(?(\\d+)\\)?)?/);
            if (noteMatch) {
                data.total_notes = parseInt(noteMatch[1]) || 0;
            }
        }
        
        // Alternative: check for count near "笔记" tab
        const allTabs = document.querySelectorAll('.tabs .tab, .tab-item');
        for (const tab of allTabs) {
            const text = tab.innerText;
            if (text.includes('笔记')) {
                const numMatch = text.match(/(\\d+)/);
                if (numMatch) {
                    data.total_notes = parseInt(numMatch[1]) || 0;
                    break;
                }
            }
        }
        
    } catch(e) {
        console.error('Error extracting author info:', e);
    }
    
    return data;
})()
"""


# JavaScript to collect note cards from author profile page
def _build_author_note_cards_script(limit: int) -> str:
    return f"""
(() => {{
    const limit = {limit};
    const seen = new Set();
    const notes = [];
    
    // Author profile uses different selectors than search results
    const cards = Array.from(document.querySelectorAll(
        'section.note-item, ' +
        'div[class*="note-item"], ' +
        '.note-card, ' +
        'a[href*="/explore/"]'
    ));
    
    for (const card of cards) {{
        // Find the link with note ID
        let link = card;
        if (card.tagName !== 'A') {{
            link = card.querySelector('a[href*="/explore/"]') || 
                   card.querySelector('a[href*="/search_result/"]');
        }}
        
        if (!link) continue;
        
        const href = link.getAttribute('href');
        if (!href) continue;
        
        // Extract note ID from href
        const match = href.match(/(?:explore|search_result)\\/([0-9a-z]+)/i);
        if (!match) continue;
        
        const noteId = match[1];
        if (seen.has(noteId)) continue;
        seen.add(noteId);
        
        // Get position for clicking
        const rect = link.getBoundingClientRect();
        const x = rect.left + rect.width / 2 + window.scrollX;
        const y = rect.top + rect.height / 2 + window.scrollY;
        
        // Mark element for later selection
        const marker = `data-mcp-author-${{Date.now()}}-${{notes.length}}`;
        link.setAttribute('data-mcp-author', marker);
        const selector = `[data-mcp-author="${{marker}}"]`;
        
        // Build canonical URL
        const canonicalUrl = `https://www.xiaohongshu.com/explore/${{noteId}}`;
        
        notes.push({{
            noteId: noteId,
            selector: selector,
            url: canonicalUrl,
            x: x,
            y: y
        }});
        
        if (notes.length >= limit) break;
    }}
    
    return notes;
}})()
"""


class AuthorService:
    """Service for collecting notes from a specific author's profile page."""
    
    def __init__(self) -> None:
        self.entry_url = chrome_entry_url()
    
    async def collect_author_notes(
        self,
        author_url: str,
        skip_note_ids: List[str] = None,
        note_limit: int = 50,
    ) -> AuthorNotesResponse:
        """
        Collect notes from an author's profile page.
        
        Args:
            author_url: The author's profile URL
            skip_note_ids: List of note IDs to skip (already collected)
            note_limit: Maximum number of notes to collect in this batch
        
        Returns:
            AuthorNotesResponse with collected notes and progress info
        """
        skip_note_ids = skip_note_ids or []
        skip_set: Set[str] = set(skip_note_ids)
        diagnostics: List[str] = []
        notes: List[NoteDetail] = []
        skipped_count = 0
        
        # Extract author_id from URL
        author_id = extract_author_id(author_url)
        if not author_id:
            return AuthorNotesResponse(
                success=False,
                message=f"无法从URL中提取作者ID: {author_url}",
                diagnostics=["invalid_author_url"]
            )
        
        diagnostics.append(f"author_id={author_id}")
        diagnostics.append(f"skip_note_ids_count={len(skip_note_ids)}")
        diagnostics.append(f"note_limit={note_limit}")
        
        # Initialize Chrome client - target author profile page
        client = ChromeDevToolsClient(initial_url="https://www.xiaohongshu.com/user/profile")
        
        try:
            # Inject stealth script
            await client.send("Page.enable")
            await client.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en']
                    });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });
                    window.chrome = { runtime: {} };
                """
            })
            
            # Navigate to author profile page
            logger.info(f"Navigating to author profile: {author_url}")
            await client.navigate(author_url)
            await asyncio.sleep(3)
            await client.wait_for_ready(timeout=15)
            
            # Verify we're on the correct page
            current_url = await self._safe_evaluate(client, "window.location.href")
            diagnostics.append(f"current_url={current_url}")
            
            if not current_url or "/user/profile" not in current_url:
                return AuthorNotesResponse(
                    success=False,
                    message=f"导航失败，当前页面不是作者主页: {current_url}",
                    author_id=author_id,
                    diagnostics=diagnostics
                )
            
            # Extract author info
            author_info_raw = await self._safe_evaluate(client, AUTHOR_INFO_SCRIPT, raw=True)
            author_name = author_info_raw.get("author_name", "") if author_info_raw else ""
            author_avatar = author_info_raw.get("author_avatar") if author_info_raw else None
            total_notes_on_page = author_info_raw.get("total_notes", 0) if author_info_raw else 0
            
            diagnostics.append(f"author_name={author_name}")
            diagnostics.append(f"total_notes_on_page={total_notes_on_page}")
            
            logger.info(f"Author: {author_name}, Total notes: {total_notes_on_page}")
            
            # Collection loop
            visited_ids: Set[str] = set()
            consecutive_no_new_cards = 0
            max_no_new_cards = 5
            
            while len(notes) < note_limit:
                # Get visible note cards
                cards_script = _build_author_note_cards_script(limit=50)
                cards = await self._safe_evaluate(client, cards_script, raw=True)
                
                if not isinstance(cards, list):
                    cards = []
                
                diagnostics.append(f"visible_cards={len(cards)}")
                
                # Find unvisited and unskipped cards
                new_cards_found = False
                for card in cards:
                    note_id = card.get("noteId")
                    if not note_id:
                        continue
                    
                    # Skip if already visited in this session
                    if note_id in visited_ids:
                        continue
                    
                    visited_ids.add(note_id)
                    new_cards_found = True
                    
                    # Skip if in skip_note_ids (already collected in previous sessions)
                    if note_id in skip_set:
                        skipped_count += 1
                        diagnostics.append(f"skipped_existing={note_id}")
                        continue
                    
                    # Check if we've reached the limit
                    if len(notes) >= note_limit:
                        break
                    
                    # Collect this note
                    logger.info(f"Collecting note {note_id} ({len(notes) + 1}/{note_limit})")
                    
                    try:
                        # Click on the note card
                        clicked = await self._click_note_card(client, card)
                        if not clicked:
                            diagnostics.append(f"click_failed={note_id}")
                            continue
                        
                        # Wait for navigation
                        await asyncio.sleep(2.0)
                        
                        # Ensure we're on the note page
                        navigated = await self._ensure_note_page(client, note_id, card["url"])
                        if not navigated:
                            diagnostics.append(f"nav_failed={note_id}")
                            # Try to go back anyway
                            await client.evaluate("window.history.back();")
                            await asyncio.sleep(2.0)
                            continue
                        
                        # Extract note details
                        note = await self._extract_note_detail(client, note_id, card["url"])
                        
                        if note:
                            notes.append(note)
                            diagnostics.append(f"collected={note_id}")
                            logger.info(f"Collected note {note_id}, total: {len(notes)}")
                        else:
                            diagnostics.append(f"extract_failed={note_id}")
                        
                        # Random delay
                        await asyncio.sleep(random.uniform(*NOTE_OPEN_DELAY_RANGE))
                        
                        # Return to author profile
                        await client.evaluate("window.history.back();")
                        await asyncio.sleep(random.uniform(*NOTE_RETURN_DELAY_RANGE))
                        await client.wait_for_ready(timeout=15)
                        
                    except Exception as e:
                        logger.warning(f"Error collecting note {note_id}: {e}")
                        diagnostics.append(f"error_{note_id}={str(e)}")
                        # Try to recover by going back
                        try:
                            await client.evaluate("window.history.back();")
                            await asyncio.sleep(2.0)
                        except:
                            pass
                
                # Check if we should continue scrolling
                if len(notes) >= note_limit:
                    break
                
                if not new_cards_found:
                    consecutive_no_new_cards += 1
                    if consecutive_no_new_cards >= max_no_new_cards:
                        logger.info("No new cards found after multiple scrolls, stopping")
                        diagnostics.append("no_more_cards")
                        break
                else:
                    consecutive_no_new_cards = 0
                
                # Scroll down to load more
                scroll_script = """
                (() => {
                    const scroller = document.scrollingElement || document.body;
                    const randomOffset = Math.floor(Math.random() * 400) + 600;
                    scroller.scrollBy({ top: randomOffset, behavior: 'smooth' });
                    return true;
                })()
                """
                await client.evaluate(scroll_script)
                await asyncio.sleep(random.uniform(*SCROLL_DELAY_RANGE))
            
            # Build response
            already_collected = len(skip_note_ids)
            has_more = (already_collected + len(notes) + skipped_count) < total_notes_on_page
            
            return AuthorNotesResponse(
                success=True,
                message=f"成功采集 {len(notes)} 篇笔记",
                author_id=author_id,
                author_name=author_name,
                author_avatar=author_avatar,
                total_notes_on_page=total_notes_on_page,
                already_collected=already_collected,
                new_collected=len(notes),
                skipped_count=skipped_count,
                has_more=has_more,
                notes=notes,
                diagnostics=diagnostics
            )
            
        except Exception as e:
            logger.error(f"Error in collect_author_notes: {e}")
            diagnostics.append(f"error={str(e)}")
            return AuthorNotesResponse(
                success=False,
                message=f"采集过程出错: {str(e)}",
                author_id=author_id,
                diagnostics=diagnostics
            )
        finally:
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                pass
    
    async def _click_note_card(self, client: ChromeDevToolsClient, card: dict) -> bool:
        """Click on a note card using CDP mouse events."""
        selector = card.get("selector")
        
        # Get fresh coordinates
        get_coords_script = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: 'center', behavior: 'instant'}});
            const rect = el.getBoundingClientRect();
            return {{
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                width: rect.width,
                height: rect.height
            }};
        }})()
        """
        
        coords = await self._safe_evaluate(client, get_coords_script, raw=True)
        if not coords:
            return False
        
        try:
            # Human-like mouse movement
            start_x = coords['x'] + random.randint(-30, 30)
            start_y = coords['y'] + random.randint(-30, 30)
            
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": start_x,
                "y": start_y,
                "buttons": 0
            })
            await asyncio.sleep(random.uniform(0.1, 0.2))
            
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": coords['x'],
                "y": coords['y'],
                "buttons": 0
            })
            await asyncio.sleep(random.uniform(0.1, 0.2))
            
            # Click
            await client.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": coords['x'],
                "y": coords['y'],
                "button": "left",
                "clickCount": 1
            })
            await asyncio.sleep(random.uniform(0.05, 0.15))
            
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": coords['x'],
                "y": coords['y'],
                "button": "left",
                "clickCount": 1
            })
            
            return True
            
        except Exception as e:
            logger.warning(f"CDP click failed: {e}, trying JS fallback")
            
            # Fallback: JS click
            click_script = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (el) {{
                    el.click();
                    return true;
                }}
                return false;
            }})()
            """
            result = await self._safe_evaluate(client, click_script, raw=True)
            return result is True
    
    async def _ensure_note_page(self, client: ChromeDevToolsClient, note_id: str, note_url: str) -> bool:
        """Ensure we're on the note detail page."""
        for _ in range(10):
            current = await self._safe_evaluate(client, "window.location.href")
            if isinstance(current, str) and note_id in current and "/explore/" in current:
                return True
            await asyncio.sleep(0.5)
        
        # Force navigate as fallback
        logger.warning(f"SPA navigation failed for {note_id}, forcing hard navigation")
        await client.navigate(note_url)
        await asyncio.sleep(1.0)
        
        for _ in range(10):
            current = await self._safe_evaluate(client, "window.location.href")
            if isinstance(current, str) and note_id in current and "/explore/" in current:
                return True
            await asyncio.sleep(0.5)
        
        return False
    
    async def _extract_note_detail(
        self,
        client: ChromeDevToolsClient,
        note_id: str,
        note_url: str,
    ) -> Optional[NoteDetail]:
        """Extract note details from the note page using __INITIAL_STATE__."""
        note_identifier = json.dumps(note_id)
        state_script = NOTE_STATE_TEMPLATE.replace("__NOTE_ID__", note_identifier)
        
        for attempt in range(30):
            result = await self._safe_evaluate(client, state_script, raw=True)
            
            if result and result.get("ready") and isinstance(result.get("payload"), dict):
                payload = result["payload"]
                return self._build_note_model(payload, note_url)
            
            await asyncio.sleep(0.3 if attempt < 4 else 0.6)
        
        logger.warning(f"Note {note_id} not ready in __INITIAL_STATE__")
        return None
    
    def _build_note_model(self, payload: dict, fallback_url: str) -> NoteDetail:
        """Build NoteDetail from payload."""
        note_id = payload.get("noteId") or ""
        title = (payload.get("title") or payload.get("name") or "").strip()
        desc = (payload.get("desc") or "").strip()
        
        user = payload.get("user") or {}
        interact_info = payload.get("interactInfo") or {}
        
        like_count = self._parse_count(interact_info.get("likedCount", 0))
        collect_count = self._parse_count(interact_info.get("collectedCount", 0))
        comment_count = self._parse_count(interact_info.get("commentCount", 0))
        share_count = self._parse_count(interact_info.get("shareCount", 0))
        
        publish_date = self._format_timestamp(payload.get("time") or payload.get("lastUpdateTime"))
        
        images = self._collect_images(payload.get("imageList") or [])
        videos = self._collect_videos(payload.get("video"), payload.get("imageList") or [])
        tags = self._collect_tags(payload.get("tagList") or [], desc)
        
        note_url = payload.get("fullUrl") or fallback_url
        location = payload.get("ipLocation")
        
        return NoteDetail(
            note_id=note_id,
            title=title,
            author=(user.get("nickname") or user.get("name") or ""),
            author_id=user.get("userId"),
            content=desc,
            images=images[:50],
            videos=videos[:50],
            like_count=like_count,
            collect_count=collect_count,
            comment_count=comment_count,
            share_count=share_count,
            publish_time=publish_date,
            location=location,
            tags=tags,
            note_url=note_url,
        )
    
    @staticmethod
    def _parse_count(value) -> int:
        """Parse count values like '1.2万' to integers."""
        if value in (None, "", "null"):
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            pass
        
        text_value = str(value).strip()
        multipliers = {"万": 10_000, "w": 10_000, "W": 10_000, "千": 1_000, "k": 1_000, "K": 1_000}
        
        for unit, factor in multipliers.items():
            if unit in text_value:
                try:
                    number = float(text_value.replace(unit, "").strip())
                    return int(number * factor)
                except ValueError:
                    return 0
        
        try:
            return int(float(text_value))
        except ValueError:
            return 0
    
    @staticmethod
    def _format_timestamp(value) -> Optional[str]:
        """Format timestamp to YYYY-MM-DD."""
        if not value:
            return None
        try:
            dt = datetime.fromtimestamp(value / 1000)
            return dt.strftime("%Y-%m-%d")
        except (OSError, ValueError):
            return None
    
    @staticmethod
    def _collect_images(image_list: list) -> List[str]:
        """Extract image URLs from imageList."""
        urls = []
        for img in image_list:
            if isinstance(img, dict):
                url = img.get("urlDefault") or img.get("url")
                if url:
                    urls.append(url)
            elif isinstance(img, str):
                urls.append(img)
        return urls
    
    @staticmethod
    def _collect_videos(video_entry: Optional[dict], image_list: list) -> List[str]:
        """Extract video URLs."""
        urls = []
        if video_entry and isinstance(video_entry, dict):
            media = video_entry.get("media") or {}
            stream = media.get("stream") or {}
            
            for key in ("h264", "h265"):
                candidates = stream.get(key) or []
                for entry in candidates:
                    if not isinstance(entry, dict):
                        continue
                    url = entry.get("masterUrl")
                    if url:
                        urls.append(url)
                        break
                    backups = entry.get("backupUrls") or []
                    if backups:
                        urls.append(backups[0])
                        break
                if urls:
                    break
        return urls
    
    @staticmethod
    def _collect_tags(tag_list: list, desc: str) -> List[str]:
        """Extract tags from tagList and description."""
        tags = []
        for tag in tag_list:
            if isinstance(tag, dict):
                name = tag.get("name") or tag.get("title")
            else:
                name = str(tag)
            if name:
                tags.append(name.strip())
        
        # Also extract hashtags from description
        hashtag_pattern = re.compile(r'#([^#\s]+)[\s#]?')
        matches = hashtag_pattern.findall(desc)
        for match in matches:
            if match and match not in tags:
                tags.append(match)
        
        return tags
    
    @staticmethod
    async def _safe_evaluate(
        client: ChromeDevToolsClient,
        expression: str,
        raw: bool = False,
    ):
        """Safely evaluate JavaScript expression."""
        try:
            return await client.evaluate(expression, raw=raw)
        except Exception as e:
            logger.warning(f"Evaluate failed: {e}")
            return None


# JavaScript template to extract note state from __INITIAL_STATE__
NOTE_STATE_TEMPLATE = """
(() => {
  const noteId = __NOTE_ID__;
  const state = window.__INITIAL_STATE__;
  if (!state) return { ready: false, reason: 'no_state' };
  
  const noteDetailMap = state.note?.noteDetailMap;
  if (!noteDetailMap) return { ready: false, reason: 'no_noteDetailMap' };
  
  const entry = noteDetailMap[noteId];
  if (!entry) return { ready: false, reason: 'noteId_not_found' };
  
  const note = entry.note;
  if (!note) return { ready: false, reason: 'no_note_in_entry' };
  
  return { ready: true, payload: note };
})()
"""

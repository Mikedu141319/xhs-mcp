"""Service responsible for collecting notes from a specific author profile page."""

from __future__ import annotations

import asyncio
import base64
import json
import os
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
from src.utils.browser_guard import BrowserGuard
from src.utils.cookie_storage import restore_cookies_to_client


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
        const followingMatch = statsText.match(/([\\d.]+万?)\\s*关注/);
        const followersMatch = statsText.match(/([\\d.]+万?)\\s*粉丝/);
        const likesMatch = statsText.match(/([\\d.]+万?)\\s*获赞/);
        
        if (followingMatch && !data.following) data.following = parseCount(followingMatch[1]);
        if (followersMatch && !data.followers) data.followers = parseCount(followersMatch[1]);
        if (likesMatch && !data.likes_and_collects) data.likes_and_collects = parseCount(likesMatch[1]);
        
        // Total notes - look for tab with "笔记" and a number
        const noteTabEl = document.querySelector('.tabs [data-type="note"], .tab-item.active, .tabs [data-type="note"]');
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
    
    // On author profile, the main elements are a[href*="/explore/"] links
    const links = Array.from(document.querySelectorAll('a[href*="/explore/"]'));
    
    for (const link of links) {{
        const href = link.getAttribute('href');
        if (!href) continue;
        
        // Extract note ID from href
        const match = href.match(/explore\\/([0-9a-zA-Z]+)/i);
        if (!match) continue;
        
        const noteId = match[1];
        if (seen.has(noteId)) continue;
        seen.add(noteId);
        
        // Use the noteId to construct a stable selector
        // This selector will work even if the page re-renders
        const selector = `a[href*="/explore/${{noteId}}"]`;
        
        // Get position for clicking
        const rect = link.getBoundingClientRect();
        const x = rect.left + rect.width / 2 + window.scrollX;
        const y = rect.top + rect.height / 2 + window.scrollY;
        
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
    """Service for collecting notes from a specific author profile page."""
    
    def __init__(self, browser_guard: BrowserGuard = None) -> None:
        self.entry_url = chrome_entry_url()
        if browser_guard is None:
            browser_guard = BrowserGuard()
        self.browser_guard = browser_guard
    
    async def collect_author_notes(
        self,
        author_url: str = None,
        xhs_id: str = None,
        skip_note_ids: List[str] = None,
        note_limit: int = 50,
        comment_limit: int = 10,
        deadline: float = None,
    ) -> AuthorNotesResponse:
        """
        Collect notes from an author profile page.
        
        Args:
            author_url: The author profile URL (optional if xhs_id provided)
            xhs_id: The author's Xiaohongshu ID (小红书号, e.g. "2686982542")
                   If provided, will navigate via search which is more human-like
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
        
        # Determine navigation mode
        use_search_navigation = bool(xhs_id)
        author_id = None
        
        if author_url:
            # Extract author_id from URL
            author_id = extract_author_id(author_url)
        
        if not xhs_id and not author_id:
            return AuthorNotesResponse(
                success=False,
                message=f"需要提供 xhs_id (小红书号) 或有效的 author_url",
                diagnostics=["missing_identifier"]
            )
        
        identifier = xhs_id or author_id
        import time as _time
        if deadline is None:
            deadline = _time.monotonic() + 540  # Default 9-min safety net
        diagnostics.append(f"identifier={identifier}")
        diagnostics.append(f"use_search_navigation={use_search_navigation}")
        diagnostics.append(f"skip_note_ids_count={len(skip_note_ids)}")
        diagnostics.append(f"note_limit={note_limit}")
        
        # Ensure Chrome is running (local mode with BrowserGuard)
        try:
            await self.browser_guard.ensure()
        except Exception as e:
            logger.warning(f"BrowserGuard.ensure failed: {e}, continuing anyway")
        
        # Initialize Chrome client using browserless
        client = ChromeDevToolsClient(initial_url=self.entry_url)
        
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
            
            # --- COOKIE RESTORATION ---
            try:
                cookies_restored = await restore_cookies_to_client(client)
                diagnostics.append(f"cookies_restored={cookies_restored}")
            except Exception as e:
                logger.warning(f"Cookie restoration failed: {e}")
                diagnostics.append(f"cookie_error={str(e)}")

            # Navigate to author profile - either via search or direct URL
            if use_search_navigation:
                # Use search to navigate to author profile (more human-like)
                nav_success = await self._navigate_via_search(client, xhs_id, diagnostics)
                if not nav_success:
                    return AuthorNotesResponse(
                        success=False,
                        message=f"通过搜索导航失败，无法找到博主: {xhs_id}",
                        author_id=xhs_id,
                        diagnostics=diagnostics
                    )
            else:
                # Direct URL navigation (may trigger anti-scraping)
                logger.info(f"Navigating directly to author profile: {author_url}")
                await client.navigate(author_url)
                await asyncio.sleep(3)
                await client.wait_for_ready(timeout=15)
            
            # Verify we're on the correct page (author profile)
            current_url = await client.get_current_url()
            diagnostics.append(f"current_url={current_url}")
            
            if not current_url or "/user/profile" not in current_url:
                return AuthorNotesResponse(
                    success=False,
                    message=f"导航失败，当前页面不是作者主页: {current_url}",
                    author_id=identifier,
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
            consecutive_all_in_skip = 0   # Counts scrolls where new cards are all in history
            # Dynamic threshold: each scroll loads ~15 notes, allow enough scrolls to pass
            # all previously collected notes (skip_set) plus a safety buffer.
            # e.g. skip_set=0→5, skip_set=150→18, skip_set=300→33
            max_all_in_skip = max(5, len(skip_set) // 10 + 3)
            
            # --- SMART INCREMENTAL STOP ---
            consecutive_skipped_count = 0 
            # If we skip 20 old notes in a row (and find no new ones in between), 
            # we assume we've reached the already-collected history.
            # This threshold handles pinned notes (usually 1-3) safely.
            SMART_STOP_THRESHOLD = 20

            # --- SETUP NETWORK INTERCEPTION FOR COMMENTS ---
            captured_comments: List[Comment] = []
            comment_api_pattern = re.compile(r"api/sns/web/v2/comment/page")
            pending_comment_requests = set()
            processed_comment_requests = set()
            
            async def on_response(event: dict) -> None:
                try:
                    params = event.get("params", {})
                    resp = params.get("response", {})
                    url = resp.get("url", "")
                    req_id = params.get("requestId")
                    status = resp.get("status", 0)
                    
                    if comment_api_pattern.search(url):
                        if status == 200 and req_id not in processed_comment_requests:
                            pending_comment_requests.add(req_id)
                except Exception: pass

            async def on_loading_finished(event: dict) -> None:
                try:
                    params = event.get("params", {})
                    req_id = params.get("requestId")
                    
                    if req_id in pending_comment_requests:
                        if req_id in processed_comment_requests:
                            pending_comment_requests.discard(req_id)
                            return

                        pending_comment_requests.discard(req_id)
                        processed_comment_requests.add(req_id)
                        
                        # Fetch body
                        try:
                            # Small delay
                            await asyncio.sleep(0.5)
                            body_data = await client.send("Network.getResponseBody", {"requestId": req_id}, timeout=5.0)
                            body_content = body_data.get("body")
                            is_base64 = body_data.get("base64Encoded", False)
                            
                            if body_content:
                                import base64
                                if is_base64:
                                    body_str = base64.b64decode(body_content).decode('utf-8')
                                else:
                                    body_str = body_content
                                    
                                comments = self._parse_comment_response(body_str)
                                if comments:
                                    logger.info(f"Captured {len(comments)} comments from network")
                                    captured_comments.extend(comments)
                        except Exception as e:
                            logger.debug(f"Failed to fetch comment body: {e}")
                except Exception: pass

            # Enable Network and attach listeners
            await client.send("Network.enable", {
                "maxResourceBufferSize": 100 * 1024 * 1024,
                "maxTotalBufferSize": 200 * 1024 * 1024
            })
            client.on("Network.responseReceived", on_response)
            client.on("Network.loadingFinished", on_loading_finished)
            
            # Track card IDs between scrolls to detect page changes
            last_card_ids = set()
            
            while len(notes) < note_limit:
                # Hard deadline check — stop cleanly and return partial results
                import time as _time
                if _time.monotonic() > deadline:
                    logger.warning(f"Deadline exceeded after {len(notes)} notes. Returning partial results.")
                    diagnostics.append("deadline_exceeded")
                    break
                # Get visible note cards
                cards_script = _build_author_note_cards_script(limit=50)
                cards = await self._safe_evaluate(client, cards_script, raw=True)
                
                if not isinstance(cards, list):
                    cards = []
                
                # Debug: Log coordinates for first few cards
                if cards:
                    debug_coords = []
                    for c in cards[:10]:
                        y_val = int(c.get('y', 0))
                        x_val = int(c.get('x', 0))
                        debug_coords.append(f"id={c.get('noteId')[-4:]}:({x_val},{y_val})[Bin:{y_val//50}]")
                    logger.debug(f"Card Coords: {', '.join(debug_coords)}")

                # CRITICAL FIX: Sort cards by visual position (Y then X)
                # Binning Y by 50px (approx 1/4 card height) to allow for slight misalignment
                cards.sort(key=lambda c: (int(c.get('y', 0)) // 50, int(c.get('x', 0))))
                
                diagnostics.append(f"visible_cards={len(cards)}")
                
                # Find the FIRST unvisited and unskipped card
                target_card = None
                
                for i, card in enumerate(cards):
                    note_id = card.get("noteId")
                    if not note_id:
                        continue
                    
                    # Logic Log: Card Discovery
                    logger.info(f"[Card Check {i+1}/{len(cards)}] ID={note_id} Pos=({card.get('x')},{card.get('y')})")
                    
                    # Skip if already visited in this session
                    if note_id in visited_ids:
                        logger.info(f"  -> Skipped (Reason: Already Visited in Session)")
                        continue
                    
                    # Skip if in skip_note_ids
                    if note_id in skip_set:
                        # Log but count as visited so we don't re-process in next scan
                        visited_ids.add(note_id)
                        skipped_count += 1
                        consecutive_skipped_count += 1
                        diagnostics.append(f"skipped_existing={note_id}")
                        logger.info(f"  -> Skipped (Reason: In Skip Set/History)")
                        
                        if consecutive_skipped_count >= SMART_STOP_THRESHOLD:
                             if consecutive_skipped_count % 20 == 0:
                                 logger.info(f"Skipping continuous block: {consecutive_skipped_count}...")
                        continue
                    
                    # Found a new note!
                    logger.info(f"  -> SELECTED as Target")
                    target_card = card
                    consecutive_skipped_count = 0
                    break
                
                if not target_card:
                    # No new targets in current view
                    logger.info("No new targets found in current view, scrolling...")
                    
                    if len(notes) >= note_limit:
                        break

                    # Check if page content actually changed by comparing card ID sets
                    current_card_ids = {card.get("noteId") for card in cards if card.get("noteId")}
                    has_new_ids = bool(current_card_ids - last_card_ids)
                    
                    if has_new_ids:
                        truly_new_ids = current_card_ids - last_card_ids
                        # Check if ALL new IDs are already in skip set or visited this session
                        all_new_in_skip = all(
                            nid in skip_set or nid in visited_ids
                            for nid in truly_new_ids
                        )
                        if all_new_in_skip:
                            consecutive_all_in_skip += 1
                            logger.info(f"Page changed: {len(truly_new_ids)} new IDs but ALL in history/skip "
                                        f"({consecutive_all_in_skip}/{max_all_in_skip}), may be reaching end")
                        else:
                            consecutive_all_in_skip = 0
                        consecutive_no_new_cards = 0
                    else:
                        # No new IDs appeared, page didn't change
                        consecutive_no_new_cards += 1
                        logger.info(f"Page content unchanged, no progress ({consecutive_no_new_cards}/{max_no_new_cards})")

                    if consecutive_no_new_cards >= max_no_new_cards:
                        logger.info("No new cards found after multiple scrolls, stopping")
                        diagnostics.append("no_more_cards")
                        break
                    
                    if consecutive_all_in_skip >= max_all_in_skip:
                        logger.info(f"All newly loaded notes are already collected after {consecutive_all_in_skip} scrolls, stopping")
                        diagnostics.append("all_collected")
                        break
                        
                    # Scroll down to load more
                    # Use window.scrollTo for aggressive infinite load triggering
                    # CRITICAL: wrap in setTimeout so the evaluate returns instantly and doesn't block
                    # Python for 10 seconds while Xiaohongshu React hydrator locks the Chrome thread
                    scroll_script = """
                    (() => {
                        setTimeout(() => {
                            const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                            window.scrollTo({ top: h + 1000, behavior: 'smooth' });
                        }, 0);
                        return true;
                    })()
                    """

                    await self._safe_evaluate(client, scroll_script)
                    await asyncio.sleep(random.uniform(*SCROLL_DELAY_RANGE))
                    
                    # Update last_card_ids for next iteration
                    last_card_ids = current_card_ids
                    
                    continue # Re-scan for targets
                
                # We have a target to process
                consecutive_no_new_cards = 0
                card = target_card
                note_id = card.get("noteId")
                
                # CRITICAL: Mark as visited so we don't pick it again in the next re-scan
                visited_ids.add(note_id) 
                
                # Log collection start
                logger.info(f"Processing note {len(notes)+1}/{note_limit}: {note_id}")
                
                # Check limit (redundant but safe)
                if len(notes) >= note_limit:
                    break
                
                # Clear capture vars
                captured_comments.clear()
                pending_comment_requests.clear()
                processed_comment_requests.clear()

                # Click the card with retry logic
                success = False
                method = None
                for retry in range(3):  # Try up to 3 times
                    success, method = await self._click_note_card(client, card)
                    if success:
                        break
                    if retry < 2:  # Don't wait after last attempt
                        logger.info(f"Click failed for {note_id}, retrying ({retry+1}/3)...")
                        await asyncio.sleep(1.0)  # Wait before retry

                if not success:
                    logger.warning(f"Failed to click card {note_id} after 3 attempts")
                    diagnostics.append(f"click_failed={note_id}")
                    continue
                
                # Wait for SPA navigation (wait longer for safety)
                await asyncio.sleep(2.0)

                note_url = card.get("url") or f"https://www.xiaohongshu.com/explore/{note_id}"
                
                if await self._ensure_note_page(client, note_id, note_url):
                    # Extract details
                    try:
                        note = await self._extract_note_detail(client, note_id, note_url)
                        
                        if note:
                            # --- Comment Collection & Merge ---
                            try:
                                dom_comments = await self._collect_comments(client, note_id, limit=comment_limit, expected_count=note.comment_count)
                                merged_list = dom_comments
                                if captured_comments:
                                    existing_ids = {c.id for c in merged_list}
                                    for c in captured_comments:
                                        if c.id not in existing_ids:
                                            merged_list.append(c)
                                note.comments = self._flatten_comments(merged_list)
                                # note.hot_comments_summary = self._summarize_hot_comments(note.comments)  # Method not implemented yet
                            except Exception as e:
                                logger.warning(f"Comment handling failed: {e}")
                            
                            notes.append(note)
                            logger.info(f"Successfully collected note: {note.title}")

                            # Add a small delay after successful collection to let the page stabilize
                            await asyncio.sleep(0.5)
                        else:
                            diagnostics.append(f"extract_failed={note_id}")
                    except Exception as e:
                        logger.error(f"Error processing note {note_id}: {e}")
                        diagnostics.append(f"process_error={note_id}")
                    
                    # Go back to author page (_safe_evaluate defaults to 10s)
                    await self._safe_evaluate(client, "window.history.back()")
                    
                    # Wait for author page via HTTP URL polling (avoids CDP blocking during back-nav)
                    # MUST land on /user/profile/; search_result is NOT acceptable
                    try:
                        _back_ok = False
                        for _ in range(30):  # up to 15 seconds
                            url = await client.get_current_url()
                            if url and "/user/profile/" in url:
                                _back_ok = True
                                break
                            await asyncio.sleep(0.5)
                        if not _back_ok:
                            curr = await client.get_current_url()
                            logger.info(f"Not on author profile after back() (url={curr}), re-navigating...")
                            if use_search_navigation:
                                await self._navigate_via_search(client, xhs_id, diagnostics)
                            else:
                                await client.navigate(self.entry_url)
                    except:
                        pass
                else:
                    logger.warning(f"Failed to navigate to note {note_id}")
                    diagnostics.append(f"nav_failed={note_id}")
                    # Navigate back to author profile explicitly (history.back is unreliable)
                    try:
                        if use_search_navigation:
                            await self._navigate_via_search(client, xhs_id, diagnostics)
                        else:
                            await client.navigate(self.entry_url)
                    except Exception as e:
                        logger.warning(f"Recovery navigation failed: {e}")
                        await client.evaluate("window.history.back()")
                        await asyncio.sleep(2.0)
                
                # Loop continues to re-scan
                pass

            
            # Build response
            already_collected = len(skip_note_ids)
            has_more = (already_collected + len(notes) + skipped_count) < total_notes_on_page
            
            # Fallback for author_id if missing (essential for Pydantic validation)
            if not author_id and notes:
                for note in notes:
                    if note.author_id:
                        author_id = note.author_id
                        logger.info(f"Recovered author_id from note {note.note_id}: {author_id}")
                        break
            
            return AuthorNotesResponse(
                success=True,
                message=f"成功采集 {len(notes)} 篇笔记",
                author_id=identifier,
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
            
            # Attempt recovery for author_id in error case too
            if not identifier and notes:
                 for note in notes:
                    if note.author_id:
                        identifier = note.author_id
                        break
            
            return AuthorNotesResponse(
                success=False,
                message=f"采集过程出错: {str(e)}",
                author_id=identifier,
                diagnostics=diagnostics
            )
        finally:
            # 1. 先关闭 DevTools 客户端连接
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                pass
            
            # 2. 关闭 Chrome 浏览器进程（关键！确保每轮采集后干净重启）
            try:
                logger.info("Shutting down Chrome browser after collection round")
                await self.browser_guard.shutdown()
            except Exception as e:
                logger.warning(f"Failed to shutdown browser: {e}")
    
    async def _navigate_via_search(self, client: ChromeDevToolsClient, xhs_id: str, diagnostics: List[str]) -> bool:
        """Navigate to author profile by searching for their Xiaohongshu ID.
        
        This is more human-like than directly navigating to the profile URL.
        
        Args:
            client: Chrome DevTools client
            xhs_id: Xiaohongshu ID (小红书号) to search for
            diagnostics: List to append diagnostic messages
            
        Returns:
            True if successfully navigated to author profile, False otherwise
        """
        # Step 1: Navigate to search results page
        # Use 'web_search_result_accounts' to surface the user profile card at the top
        search_url = f"https://www.xiaohongshu.com/search_result?keyword={xhs_id}&source=web_search_result_accounts"
        logger.info(f"Navigating to search page: {search_url}")
        
        await client.navigate(search_url)
        await asyncio.sleep(1)  # Short initial wait for navigation to start
        
        # Smart wait: poll until user profile links appear or 15s elapse
        is_ready = await client.wait_for_expression(
            "(document.querySelectorAll('a[href*=\"/user/profile/\"]').length > 0) || (document.querySelector('.note-item') !== null)",
            timeout=15
        )
        diagnostics.append(f"search_url={search_url}")
        
        if not is_ready:
            logger.warning(f"Search page failed to load or time out for {xhs_id}. Aborting search navigation.")
            diagnostics.append("search_page_load_timeout")
            # Capture screenshot on timeout for diagnosis
            try:
                screenshot_result = await client.send("Page.captureScreenshot", {})
                if screenshot_result and 'data' in screenshot_result:
                    from datetime import datetime as _dt
                    log_dir = os.environ.get('LOG_DIR', './logs')
                    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                    timeout_path = f"{log_dir}/search_timeout_{xhs_id}_{ts}.png"
                    with open(timeout_path, 'wb') as f:
                        f.write(base64.b64decode(screenshot_result['data']))
                    logger.info(f"Timeout screenshot saved: {timeout_path}")
                    diagnostics.append(f"timeout_screenshot={timeout_path}")
                # Quick page state check
                page_state = await client.evaluate("""
                    ({
                        url: window.location.href,
                        title: document.title,
                        hasLoginModal: !!document.querySelector('.login-container, .ld-modal, [class*="login"]'),
                        hasCaptcha: !!document.querySelector('[class*="captcha"], [class*="verify"], .slider-container'),
                        bodyLen: (document.body?.innerText || '').length
                    })
                """)
                if page_state:
                    logger.warning(f"Page state on timeout: {page_state}")
                    diagnostics.append(f"timeout_page_state={page_state}")
            except Exception as diag_err:
                logger.debug(f"Timeout diagnosis error: {diag_err}")
            return False
            
        
        # Step 2.5: Close login popup if present (common in Docker/headless)
        try:
            close_popup_script = """
            (function() {
                // Find and click close button on login popup
                const closeBtn = document.querySelector('.close, .ld-modal-close');
                if (closeBtn) {
                    closeBtn.click();
                    return {closed: true, method: 'close_button'};
                }
                
                // Find and click X button or overlay
                const overlay = document.querySelector('.ld-mask, .login-mask');
                if (overlay) {
                    overlay.click();
                    return {closed: true, method: 'overlay'};
                }
                
                return {closed: false};
            })()
            """
            popup_result = await self._safe_evaluate(client, close_popup_script, raw=True, timeout=15.0)
            if popup_result and popup_result.get('closed'):
                logger.info(f"Closed login popup using: {popup_result.get('method')}")
                await asyncio.sleep(0.3)  # Brief wait for popup animation to complete
        except Exception as e:
            logger.debug(f"Popup close attempt (non-critical): {e}")
        
        # Step 3: Find and click the author card (with retry for Docker/headless env) at the top of search results
        # The author card typically appears at the top when searching for a user ID
        # Selector based on the user screenshot - the author info card with avatar
        find_author_card_script = f"""
        (() => {{
            // Look for the author card - it contains the xhs_id and has a "关注" button
            const xhsId = "{xhs_id}".toLowerCase();
        
        // Find all elements that might be the author card, limit to 100 to prevent JS execution timeouts
        const allLinks = Array.from(document.querySelectorAll('a[href*="/user/profile/"]')).slice(0, 100);
        for (const link of allLinks) {{
            // EXCLUDE sidebar and header links (the logged-in user profile)
            const isSidebarOrHeader = link.closest('.side-bar, nav, header, #header, .layout-header, .layout-menu, .side-menu, .menu, .channel-scroll-box');
            if (isSidebarOrHeader) continue;

            // Traverse up to 4 levels to construct the logical "card" text
            // stop if the ancestor is too large or contains error text
            let current = link;
            let match = false;
            for(let i=0; i<4; i++) {{
                if (!current) break;
                
                // DANGER PREVENT: If we hit a core structural wrapper, evaluating textContent will freeze Chromium!
                if (current.tagName === 'BODY' || current.id === 'app') break;
                if (current.className && typeof current.className === 'string') {{
                    const cls = current.className.toLowerCase();
                    if (cls.includes('layout') || cls.includes('main') || cls.includes('container-')) break;
                }}
                
                const text = current.textContent || '';
                
                // Exceeded reasonable card size (real user cards are ~50 chars)
                if (text.length > 120) break; 
                
                // If it captured the "No results found for {id}" page message, abort immediately
                if (text.includes('没有找到') || text.includes('抱歉') || text.includes('找不到')) break;
                
                if (text.toLowerCase().includes(xhsId)) {{
                    match = true;
                    break;
                }}
                current = current.parentElement;
            }}
            
            // Match exact or partial ID within the broader card container
            if (match) {{
                // Found a text match! Safe to check bounds now. DOES NOT SCROLL YET.
                let rect = link.getBoundingClientRect();
                
                if (rect.width === 0 && link.children.length > 0) {{
                    rect = link.children[0].getBoundingClientRect();
                }}
                
                if (rect.width > 0 && rect.height > 0) {{
                    link.scrollIntoView({{block: 'center', behavior: 'instant'}});
                    // recalculate after scroll to get precise click coords
                    rect = link.getBoundingClientRect();
                    if (rect.width === 0 && link.children.length > 0) rect = link.children[0].getBoundingClientRect();
                    
                    link.target = "_self"; // CRITICAL: Stop Xiaohongshu from opening a new tab
                    return {{
                        found: true,
                        href: link.getAttribute('href'),
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2
                    }};
                }}
            }}
        }}
        
        // Removed fallback layout loop...
        
        var emptyState = false;
        const emptyNode = document.querySelector('.empty-container, .error-content, .no-result, .search-empty, [class*="empty"]');

        if (emptyNode) {{
            emptyState = true;
        }}
            
        return {{ 
            found: false,
            empty_state: emptyState,
            debug_info: {{
                link_count: allLinks.length,
                page_title: document.title || 'no-title'
            }}
        }};
    }})()
    """
        
        
        # Retry finding author card (important for Docker/headless environments)
        max_find_attempts = 3
        result = None
        
        for attempt in range(max_find_attempts):
            if attempt > 0:
                # Wait longer on each retry for elements to render
                wait_time = 2 + attempt  # 2s, 3s, 4s
                logger.info(f"Author card not found on attempt {attempt}/{max_find_attempts}, waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            
            # Debug: capture page state only when DEBUG_SCREENSHOT env var is set
            if attempt == 0 and os.environ.get("DEBUG_SCREENSHOT"):
                try:
                    # Take screenshot
                    screenshot_result = await client.send("Page.captureScreenshot", {})
                    if screenshot_result and 'data' in screenshot_result:
                        log_dir = os.environ.get('LOG_DIR', './logs')
                        screenshot_path = f"{log_dir}/search_page_debug.png"
                        with open(screenshot_path, 'wb') as f:
                            f.write(base64.b64decode(screenshot_result['data']))
                        logger.info(f"Screenshot saved: {screenshot_path}")
                    
                    # Check page HTML
                    html_check = await client.evaluate("""
                        ({
                            hasUserProfileLinks: document.querySelectorAll('a[href*="/user/profile/"]').length,
                            totalLinks: document.querySelectorAll('a').length,
                            title: document.title
                        })
                    """)
                    logger.info(f"Page state: {html_check}")
                except Exception as e:
                    logger.warning(f"Debug capture failed: {e}")
            
            result = await self._safe_evaluate(client, find_author_card_script, raw=True, timeout=15.0)
            
            if result and result.get("found"):
                logger.info(f"Author card found on attempt {attempt+1}/{max_find_attempts}")
                break
                
            # If not found, log the debug info immediately so we can see what goes wrong on each attempt
            if result and 'debug_info' in result:
                logger.debug(f"Card search debug (attempt {attempt+1}): {result['debug_info']}")
                
            # FAST FAIL if page explicitly says no results exist
            if result and result.get("empty_state"):
                logger.info("Search page explicitly indicates no results found. Fast failing.")
                break
        
        if not result or not result.get("found"):
            diagnostics.append("author_card_not_found")
            if result and 'debug_info' in result:
                 logger.warning(f"Author card search final debug: {result['debug_info']}")
                 diagnostics.append(f"debug_info={result['debug_info']}")
            # Always capture screenshot on failure for diagnosis
            try:
                screenshot_result = await client.send("Page.captureScreenshot", {})
                if screenshot_result and 'data' in screenshot_result:
                    from datetime import datetime as _dt
                    log_dir = os.environ.get('LOG_DIR', './logs')
                    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                    fail_path = f"{log_dir}/search_fail_{xhs_id}_{ts}.png"
                    with open(fail_path, 'wb') as f:
                        f.write(base64.b64decode(screenshot_result['data']))
                    logger.info(f"Failure screenshot saved: {fail_path}")
                    diagnostics.append(f"fail_screenshot={fail_path}")
                # Also capture page HTML snippet for diagnosis
                html_diag = await client.evaluate("""
                    ({
                        url: window.location.href,
                        title: document.title,
                        profileLinks: document.querySelectorAll('a[href*="/user/profile/"]').length,
                        totalLinks: document.querySelectorAll('a').length,
                        hasLoginModal: !!document.querySelector('.login-container, .ld-modal, [class*="login"]'),
                        hasCaptcha: !!document.querySelector('[class*="captcha"], [class*="verify"], .slider-container'),
                        bodyTextPreview: (document.body?.innerText || '').substring(0, 300)
                    })
                """)
                if html_diag:
                    logger.warning(f"Page diagnosis on failure: {html_diag}")
                    diagnostics.append(f"page_diagnosis={html_diag}")
            except Exception as diag_err:
                logger.debug(f"Failure diagnosis capture error: {diag_err}")
            logger.warning(f"Could not find author card for xhs_id: {xhs_id} after {max_find_attempts} attempts")
            return False
        
        diagnostics.append(f"author_card_found_href={result.get('href')}")
        
        # Step 4: Click the author card link
        # Use CDP mouse events to ensure SPA navigation. JS clicks lack isTrusted=true,
        # which triggers hard page reloads and gets caught by Xiaohongshu's anti-bot/captcha.
        target_href = result.get("href", "")
        click_x = result.get("x")
        click_y = result.get("y")
        
        logger.info(f"CDP clicking author card at ({click_x}, {click_y}) href: {target_href}")
        
        # Override window.open to prevent XHS SPA router from opening a new tab.
        # This forces the navigation to happen in the current tab context.
        await self._safe_evaluate(client, """
        window._originalOpen = window.open;
        window.open = function(url, name, features) {
            window.location.href = url;
            return null;
        };
        """)
        
        # Fallback JS setup
        js_click = f"""
        (() => {{
            const link = document.querySelector('a[href="{target_href}"]') || document.querySelector('a[href*="/user/profile/"]');
            if (link) link.target = "_self";
            return true;
        }})()
        """
        await self._safe_evaluate(client, js_click)

        if click_x is not None and click_y is not None:
            # CDP Click (isTrusted=true is required to bypass React's bot detection)
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": click_x,
                "y": click_y,
                "buttons": 0
            })
            await asyncio.sleep(0.1)
            await client.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": click_x,
                "y": click_y,
                "button": "left",
                "clickCount": 1
            })
            await asyncio.sleep(0.1)
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": click_x,
                "y": click_y,
                "button": "left",
                "clickCount": 1
            })
        
        # Poll for SPA navigation: check URL every 0.5s for up to 8s
        nav_done = await client.wait_for_expression(
            "window.location.href.includes('/user/profile/')",
            timeout=8,
            interval=0.5
        )
        current_url = await client.get_current_url()
        
        # Step 5: If click didn't navigate to author profile, use native URL assignment fallback
        if not nav_done or not current_url or "/user/profile" not in current_url:
            logger.info("CDP click did not navigate to profile, falling back to window.location.href (Referer preserved)")
            
            # Using window.location.href preserves the Referer header (search page),
            # which helps bypass Xiaohongshu Captcha triggers. client.navigate() often drops Referer.
            await self._safe_evaluate(client, f"window.location.href = '{target_href}'")
            await asyncio.sleep(2)
            await client.wait_for_ready(timeout=12)
            
            current_url = await client.get_current_url()
            if current_url and "/user/profile" in current_url:
                logger.info(f"Direct location assignment success: {current_url}")
                diagnostics.append("location_href_navigation_success")
                return True
                
            diagnostics.append(f"navigation_failed_url={current_url}")
            return False
        
        # SPA navigation succeeded
        logger.info(f"Successfully navigated to author profile: {current_url}")
        # Wait a bit longer for the profile page to finish rendering
        await asyncio.sleep(2.0)
        diagnostics.append("search_navigation_success")
        return True
    
    async def _click_note_card(self, client: ChromeDevToolsClient, card: dict) -> tuple[bool, str]:
        """
        Click on a note card using REAL CDP mouse coordinates to ensure SPA navigation.
        Programmatic JS clicks (element.click()) lack isTrusted=true, which bypasses 
        the SPA router and causes hard navigations (dropping the WebSocket).
        """
        note_id = card.get("noteId")
        if not note_id:
            return False, "no_note_id"
            
        logger.info(f"Processing note click: {note_id}")
        
        try:
            selector = f'a[href*="{note_id}"]'
            
            get_coords_script = f"""
            (() => {{
                const selector = {json.dumps(selector)};
                const elements = document.querySelectorAll(selector);
                if (!elements || elements.length === 0) return {{error: "element_not_found"}};
                
                function findRectForElement(el) {{
                    let rect = el.getBoundingClientRect();
                    // Basic visibility check
                    if (rect.width > 0 && rect.height > 0) return rect;
                    
                    // Try children
                    const img = el.querySelector('img');
                    if (img) {{
                         const r = img.getBoundingClientRect();
                         if (r.width > 0 && r.height > 0) return r;
                    }}
                    return null;
                }}

                let targetEl = null;
                let finalRect = null;
                
                // Find first visible matching element
                for (const el of elements) {{
                    const rect = findRectForElement(el);
                    if (rect) {{
                        targetEl = el;
                        finalRect = rect;
                        break;
                    }}
                }}
                
                if (!targetEl || !finalRect) return {{error: "no_visible_rect"}};
                
                // Ensure in viewport
                targetEl.scrollIntoView({{block: 'center', behavior: 'instant'}});
                
                // Re-calc after scroll
                finalRect = findRectForElement(targetEl); 
                
                return {{
                    x: finalRect.left + finalRect.width / 2,
                    y: finalRect.top + finalRect.height * 0.7, // Click lower part to avoid overlay icons
                    debug_selector: selector,
                    debug_rect: {{x: finalRect.x, y: finalRect.y, w: finalRect.width, h: finalRect.height}}
                }};
            }})()
            """
            
            coords = await self._safe_evaluate(client, get_coords_script, raw=True)
            
            if not coords or "error" in coords:
                 return False, f"cdp_coords_failed: {coords.get('error') if coords else 'None'}"
            
            # CRITICAL: Wait for scroll to complete!
            await asyncio.sleep(0.5)
            
            logger.info(f"CDP Target: selector={coords.get('debug_selector')}, rect={coords.get('debug_rect')}, click_at=({coords['x']}, {coords['y']})")
            
            # CDP Click with increased timeout for slow pages
            await client.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": coords['x'],
                "y": coords['y'],
                "button": "left",
                "clickCount": 1
            }, timeout=5.0)
            await asyncio.sleep(0.1)
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": coords['x'],
                "y": coords['y'],
                "button": "left",
                "clickCount": 1
            }, timeout=5.0)
            
            logger.info(f"CDP click successful for {note_id}")
            return True, "cdp_click_success"
            
        except Exception as e:
            logger.warning(f"CDP click exception: {e}")
            return False, f"cdp_exception: {str(e)}"
        


            



    
    async def _ensure_note_page(self, client: ChromeDevToolsClient, note_id: str, note_url: str) -> bool:
        """Ensure we are on the note detail page via SPA navigation only.
        
        IMPORTANT: We do NOT use hard navigation (client.navigate) as fallback
        because it triggers Xiaohongshu anti-scraping verification.
        If SPA navigation fails, we skip the note rather than risking detection.
        
        URL polling uses the Chrome HTTP debug endpoint (/json/list) rather than
        Runtime.evaluate('window.location.href'), because:
        - Chrome's HTTP debug endpoint always responds in <5ms, even while JS is busy
        - Runtime.evaluate BLOCKS for 30s during SPA transitions (CDP queue behind renderer)
        - Old polling approach: each 0.5s intended = 30-90s actual during SPA navigation
        """
        logger.debug(f"Waiting for SPA navigation to {note_id}...")
        # Poll up to 12 seconds (24 × 0.5s) to avoid massive 80s gaps if Chrome is stuck
        for attempt in range(24):
            url = await client.get_current_url()
            if url and note_id in url and "/explore/" in url:
                logger.info(f"SPA navigation successful for {note_id}")
                return True
            await asyncio.sleep(0.5)
        
        # SPA navigation failed - do NOT fallback to hard navigation
        # This would trigger anti-scraping verification
        logger.warning(f"SPA navigation failed for {note_id}, skipping to avoid anti-scraping detection")
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
            # Fast polling. _safe_evaluate handles 1.5s timeout gracefully so we don't block
            result = await self._safe_evaluate(client, state_script, raw=True, timeout=1.5)
            
            if result and result.get("ready") and isinstance(result.get("payload"), dict):
                payload = result["payload"]
                note = self._build_note_model(payload, note_url)
                
                # DOM Fallback for missing critical fields
                if not note.author or not note.publish_time or not note.title:
                    logger.info(f"Note {note_id} missing details, attempting DOM fallback...")
                    dom_details = await self._extract_note_dom_fallback(client)
                    if dom_details:
                        if not note.author and dom_details.get("author"):
                            note.author = dom_details["author"]
                        if not note.publish_time and dom_details.get("publish_time"):
                            note.publish_time = dom_details["publish_time"]
                        if not note.title and dom_details.get("title"):
                            note.title = dom_details["title"]
                        if not note.content and dom_details.get("content"):
                            note.content = dom_details["content"]
                        if not note.images and dom_details.get("images"):
                            note.images = dom_details["images"]
                        
                        # Fallback for counts if they are 0
                        if note.like_count == 0 and "like_count" in dom_details:
                            note.like_count = dom_details["like_count"]
                        if note.collect_count == 0 and "collect_count" in dom_details:
                            note.collect_count = dom_details["collect_count"]
                        if note.comment_count == 0 and "comment_count" in dom_details:
                            note.comment_count = dom_details["comment_count"]
                        if note.share_count == 0 and "share_count" in dom_details:
                            note.share_count = dom_details["share_count"]
                            
                return note
            
            # CRITICAL: Do not spam Chrome. If not ready, wait and poll again.
            await asyncio.sleep(0.5)
            
        logger.warning(f"Note {note_id} not ready in __INITIAL_STATE__, falling back to DOM entirely")
        
        # If __INITIAL_STATE__ completely fails or times out, we MUST try full DOM fallback
        note = NoteDetail(note_id=note_id, title="", author="", note_url=note_url)
        dom_details = await self._extract_note_dom_fallback(client)
        if dom_details:
            note.title = dom_details.get("title", "")
            note.content = dom_details.get("content", "")
            note.author = dom_details.get("author", "")
            note.publish_time = dom_details.get("publish_time", "")
            note.images = dom_details.get("images", [])
            note.like_count = dom_details.get("like_count", 0)
            note.collect_count = dom_details.get("collect_count", 0)
            note.comment_count = dom_details.get("comment_count", 0)
            note.share_count = dom_details.get("share_count", 0)
            
        return note

    async def _extract_note_dom_fallback(self, client: ChromeDevToolsClient) -> dict:
        """Extract note details directly from DOM as fallback."""
        script = """
        (() => {
            const data = {};
            try {
                // Title
                const titleEl = document.querySelector('#detail-title, .note-detail-mask .title, .note-container .title, .note-title');
                if (titleEl) data.title = titleEl.innerText.trim();
                
                // Content
                const contentEl = document.querySelector('#detail-desc, .desc, .note-desc, .content');
                if (contentEl) data.content = contentEl.innerText.trim();
                
                // Author
                const authorEl = document.querySelector('.author-name, .name, .user-name');
                if (authorEl) data.author = authorEl.innerText.trim();
                
                // Publish Time
                const dateEl = document.querySelector('.date, .publish-date, .bottom-container .time');
                if (dateEl) {
                    let dateText = dateEl.innerText.trim().replace('发布于 ', '');
                    data.publish_time = dateText; 
                }

                // Counts (Like, Collect, Comment, Share)
                const parseCount = (text) => {
                    if (!text) return 0;
                    text = text.trim();
                    if (text.includes('万')) {
                        return Math.floor(parseFloat(text.replace('万', '')) * 10000);
                    }
                    return parseInt(text) || 0;
                };

                // Like
                const likeEl = document.querySelector('.interact-container .like-wrapper .count');
                if (likeEl) data.like_count = parseCount(likeEl.innerText);

                // Collect
                const collectEl = document.querySelector('.interact-container .collect-wrapper .count');
                if (collectEl) data.collect_count = parseCount(collectEl.innerText);

                // Comment
                const commentEl = document.querySelector('.interact-container .chat-wrapper .count');
                if (commentEl) data.comment_count = parseCount(commentEl.innerText);
                
                // Share (often does not have count text, but check anyway)
                const shareEl = document.querySelector('.interact-container .share-wrapper .count');
                if (shareEl) data.share_count = parseCount(shareEl.innerText);

                // Images
                const images = [];
                // Look for images in common containers (slider, content)
                const imgEls = document.querySelectorAll('.note-content .swiper-slide img, .note-content img, .media-container img');
                imgEls.forEach(img => {
                    const src = img.getAttribute('src');
                    if (src && !src.includes('avatar') && !src.includes('profile')) {
                        // Avoid thumbnails if possible? No, src is usually fine.
                        images.push(src);
                    }
                });
                
                // If standard selectors fail, try background images? (Less common for XHS notes now)
                
                if (images.length > 0) data.images = images;

            } catch(e) {}
            return data;
        })()
        """
        
        for attempt in range(20):
            try:
                result = await self._safe_evaluate(client, script, raw=True, timeout=5.0)
                if result and result.get("title") and result.get("images"):
                    return result
                
                # If we got partial data and it's the last attempt, return it
                if attempt == 19 and result:
                    return result
                    
            except Exception as e:
                logger.warning(f"DOM fallback extraction attempt {attempt} failed: {e}")
                
            await asyncio.sleep(0.5)
            
        return {}

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
        """Extract video URLs from main video entry and live photos in image_list."""
        urls = []
        
        def extract_stream_url(entry: dict) -> Optional[str]:
            media = entry.get("media") or {}
            # Some formats have stream inside media, some have it directly on the entry
            stream = media.get("stream") or entry.get("stream") or {}
            for key in ("h264", "h265", "av1"):
                candidates = stream.get(key) or []
                for cand in candidates:
                    if not isinstance(cand, dict):
                        continue
                    url = cand.get("masterUrl")
                    if url: return url
                    backups = cand.get("backupUrls") or []
                    if backups: return backups[0]
            
            # Fallback for Direct videoId or url if stream parsing fails
            if media.get("videoId"):
                return f"https://sns-video-qc.xhscdn.com/{media.get('videoId')}"
            return None

        # 1. Main video
        if video_entry and isinstance(video_entry, dict):
            url = extract_stream_url(video_entry)
            if url: urls.append(url)
            
        # 2. Live Photos (found inside imageList)
        for img in image_list:
            if isinstance(img, dict):
                # Sometimes livePhoto is a nested dict
                live_video = img.get("livePhoto")
                if isinstance(live_video, dict):
                    url = extract_stream_url(live_video)
                    if url and url not in urls:
                        urls.append(url)
                
                # Sometimes stream is directly on the image object itself (livePhoto == true)
                if "stream" in img:
                    url = extract_stream_url(img)
                    if url and url not in urls:
                        urls.append(url)
                        
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
        timeout: float = 10.0
    ):
        """Safely evaluate JavaScript expression.
        
        Note: raw parameter is kept for API compatibility but ChromeDevToolsClient.evaluate
        always returns the value from the result.
        """
        try:
            return await client.evaluate(expression, timeout=timeout)
        except Exception as e:
            logger.warning(f"Evaluate failed: {e}")
            return None

    # --- Comment Collection Helper Methods ---

    async def _collect_comments(self, client: ChromeDevToolsClient, note_id: str, limit: int = 10, expected_count: Optional[int] = None) -> List[Comment]:
        """
        Scroll to trigger lazy loading of comments.
        Returns DOM-extracted comments as fallback.
        Note: The network listener should be active in the caller to capture network comments.
        """
        if expected_count == 0:
            logger.info(f"Skipping comment scroll for {note_id} because note states it has 0 comments.")
            return []
            
        logger.info(f"Scrolling to load comments for {note_id} (using JS scrollBy)...")
        
        # Use robust JS scrolling on the specific container
        scroll_script = """
        (() => {
            const scroller = document.querySelector('.note-scroller');
            if (scroller) {
                scroller.scrollBy({ top: scroller.clientHeight, behavior: 'smooth' });
                return true;
            }
            // Fallback for full page scrolling if note-scroller not found
            window.scrollBy({ top: window.innerHeight, behavior: 'smooth' });
            return true;
        })()
        """
        
        # Try to collect at least 'limit' comments
        prev_count = -1
        for _ in range(5):
            await self._safe_evaluate(client, scroll_script, timeout=3.0)
            await asyncio.sleep(0.8) # Slightly longer wait for images
            
            # Check count
            count_script = "document.querySelectorAll('.comment-item').length"
            count = await self._safe_evaluate(client, count_script, raw=True, timeout=1.0)
            if isinstance(count, int):
                if count >= limit or (expected_count is not None and count >= expected_count):
                    logger.info(f"Reached target comment count: {count} >= {limit} (or expected {expected_count})")
                    break
                if count == prev_count and count > 0:
                    break
                prev_count = count
            
        # Wait a bit for final responses
        await asyncio.sleep(1.0)

        # Fallback: Extract comments from DOM if network failed
        dom_comments = []
        try:
            extract_script = """
            (() => {
                const comments = [];
                const items = document.querySelectorAll('.comment-item');
                
                items.forEach(item => {
                    try {
                        const contentEl = item.querySelector('.comment-content, .content, .note-text');
                        const userEl = item.querySelector('.user-name, .name, .author-name');
                        const likeEl = item.querySelector('.like .count, .like-wrapper .count, .like-count');
                        
                        // Extract images from comment if any
                        const pictures = [];
                        const imgEls = item.querySelectorAll('.comment-picture img, .picture img');
                        imgEls.forEach(img => {
                            const src = img.getAttribute('src');
                            if (src) pictures.push(src);
                        });

                        if (contentEl && userEl) {
                            let likeCount = 0;
                            if (likeEl) {
                                let text = likeEl.innerText.trim();
                                if (text.includes('万')) {
                                    text = text.replace('万', '').trim();
                                    likeCount = parseInt(parseFloat(text) * 10000);
                                } else {
                                    likeCount = parseInt(text.replace(/[^0-9]/g, '') || '0');
                                }
                            }
                            
                            comments.push({
                                id: item.getAttribute('data-id') || '',
                                content: contentEl.innerText.trim(),
                                nickname: userEl.innerText.trim(),
                                likes: likeCount,
                                pictures: pictures
                            });
                        }
                    } catch(e) {}
                });
                return comments;
            })()
            """
            raw_data = await self._safe_evaluate(client, extract_script, raw=True)
            if isinstance(raw_data, list):
                for i, item in enumerate(raw_data):
                    # Use index as fallback ID if missing
                    c_id = item.get('id') or f"dom_{i}_{int(time.time())}"
                    
                    dom_comments.append(Comment(
                        id=c_id,
                        user_id='', 
                        nickname=item.get('nickname', 'Unknown'),
                        content=item.get('content', ''),
                        likes=item.get('likes', 0),
                        create_time=0,
                        pictures=item.get('pictures', [])
                    ))
                logger.info(f"Extracted {len(dom_comments)} comments from DOM as fallback")
        except Exception as e:
            logger.warning(f"DOM comment extraction failed: {e}")
            
        return dom_comments[:limit]

    def _parse_comment_response(self, body: str) -> List[Comment]:
        """Parse comments from the network response body."""
        try:
            data = json.loads(body)
            if "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            
            comments_data = []
            if "comments" in data:
                comments_data = data["comments"]
            elif "cursor_comments" in data:
                comments_data = data["cursor_comments"]
            elif isinstance(data, list):
                comments_data = data
                
            if not comments_data:
                return []

            results = []
            for item in comments_data:
                try:
                    results.append(self._parse_single_comment(item))
                except Exception as e:
                    continue
            return results
            
        except Exception as e:
            logger.warning(f"Error parsing comment response: {e}")
            return []

    def _parse_single_comment(self, item: dict) -> Comment:
        user_info = item.get("user_info", {})
        sub_comments = []
        for sub in item.get("sub_comments", []) or []:
            sub_comments.append(self._parse_single_comment(sub))
            
        # Extract pictures from network response
        # Usually in 'pictures' list of dicts: [{url: ...}, ...]
        pictures = []
        for pic in item.get("pictures", []) or []:
            if isinstance(pic, dict):
                url = pic.get("url_default") or pic.get("url")
                if url:
                    pictures.append(url)
            elif isinstance(pic, str):
                pictures.append(pic)
             
        return Comment(
            id=item.get("id", ""),
            user_id=user_info.get("user_id", ""),
            nickname=user_info.get("nickname", "Unknown"),
            content=item.get("content", ""),
            likes=int(item.get("like_count", item.get("likes", 0))),
            create_time=int(item.get("create_time", 0)),
            sub_comments=sub_comments,
            parent_id=item.get("target_comment_id"),
            pictures=pictures
        )

    def _flatten_comments(self, comments: List[Comment]) -> List[Comment]:
        """Flatten the nested comment structure."""
        flat_list = []
        def traverse(comment: Comment, parent_id: Optional[str] = None):
            if parent_id and not comment.parent_id:
                comment.parent_id = parent_id
            flat_list.append(comment)
            for sub in comment.sub_comments:
                traverse(sub, parent_id=comment.id)
        for c in comments:
            traverse(c)
        return flat_list


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

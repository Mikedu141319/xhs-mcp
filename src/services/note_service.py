"""Service responsible for collecting Xiaohongshu note details."""


from __future__ import annotations


import asyncio
import random
import time

# Global list to store comments for the current note being processed
current_note_comments: List[Comment] = []


import json


import re


from datetime import datetime


from typing import List, Optional


from loguru import logger


from src.clients.chrome_devtools import ChromeDevToolsClient


from src.config import chrome_entry_url


from src.schemas.note import NoteDetail, NoteDetailBatchResponse, Comment


# Performance optimized timing (reduced from original values)
PANEL_SETTLE_DELAY_SECONDS = 0.5

NOTE_OPEN_DELAY_RANGE = (1.5, 2.5)  # Was (2.5, 4.5)
NOTE_RETURN_DELAY_RANGE = (1.5, 2.5)  # Was (3.0, 5.0)
SCROLL_BEFORE_NOTE_JS = """
(() => {
  const scroller = document.scrollingElement || document.body;
  const randomOffset = Math.floor(Math.random() * 400) + 200;
  scroller.scrollBy({ top: randomOffset, behavior: 'smooth' });
  return true;
})()
"""


CLOSE_FILTER_PANEL_SCRIPT = """
(() => {
  const ensurePanel = () => {
    let panel = window.__xhsFilterPanel;
    if (panel && panel.isConnected) {
      return panel;
    }
    panel = document.querySelector('.filter-panel');
    if (panel) {
      window.__xhsFilterPanel = panel;
    }
    return panel;
  };

  const clickHeaderToggle = () => {
    const candidates = [
      document.querySelector('.filter-active'),
      document.querySelector('.filter-icon.active'),
      document.querySelector('.filter-icon'),
    ];
    for (const node of candidates) {
      if (!node) {
        continue;
      }
      const target = node.closest('button, div') || node;
      if (target && target.isConnected) {
        target.click();
        return true;
      }
    }
    return false;
  };

  const panel = ensurePanel();
  if (panel && panel.offsetParent !== null && panel.offsetWidth > 0 && panel.offsetHeight > 0) {
    if (clickHeaderToggle()) {
      return { clicked: true, method: 'filter_toggle' };
    }
  }

  const scope = panel || document.body;
  const nodes = Array.from(scope.querySelectorAll('button, [role="button"], span'));

  const collapse = nodes.find((el) => {
    if (!el || !el.isConnected) return false;
    const text = (el.innerText || '').trim();
    return text.includes('����');
  });
  if (collapse) {
    collapse.click();
    return { clicked: true, method: 'collapse' };
  }

  const toggle = nodes.find((el) => {
    if (!el || !el.isConnected) return false;
    const text = (el.innerText || '').trim();
    return text.startsWith('ɸѡ');
  });
  if (toggle) {
    toggle.click();
    return { clicked: true, method: 'toggle' };
  }

  return { clicked: false, method: 'none' };
})()
"""

_FILTER_PANEL_VISIBLE_EXPR = """
(() => {
  const ensurePanel = () => {
    let panel = window.__xhsFilterPanel;
    if (panel && panel.isConnected) {
      return panel;
    }
    panel = document.querySelector('.filter-panel');
    if (panel) {
      window.__xhsFilterPanel = panel;
    }
    return panel;
  };

  const panel = ensurePanel();
  if (panel && panel.offsetParent !== null && panel.offsetWidth > 0 && panel.offsetHeight > 0) {
    return true;
  }
  return false;
})()
"""


def _parse_count(value: object) -> int:


    """Convert counters like '1.2万' to integers."""


    if value in (None, "", "null"):


        return 0


    try:


        return int(float(value))


    except (TypeError, ValueError):


        pass


    text_value = str(value).strip()


    multipliers = {


        "万": 10_000,


        "w": 10_000,


        "W": 10_000,


        "千": 1_000,


        "k": 1_000,


        "K": 1_000,


    }


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


class NoteDetailService:


    """Encapsulates logic that extracts note details from the active Chrome page."""


    def __init__(self) -> None:


        self.entry_url = chrome_entry_url()


    async def collect_note_details(self, note_limit: int = 5) -> NoteDetailBatchResponse:
        client = ChromeDevToolsClient(initial_url=self.entry_url)
        diagnostics: list[str] = [f"note_limit={note_limit}"]
        notes: List[NoteDetail] = []
        visited_ids: set[str] = set()

        if note_limit <= 0:
            return NoteDetailBatchResponse(
                success=False,
                message="note_limit 必须大于 0",
                diagnostics=diagnostics,
            )

        try:
            # Inject stealth script to hide automation indicators
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

            ready = await client.wait_for_ready()
            diagnostics.append(f"page_ready={ready}")

            current_url = await self._safe_evaluate(client, "window.location.href")
            diagnostics.append(f"page_url={current_url}")

            if not current_url or "search_result" not in current_url:
                return NoteDetailBatchResponse(
                    success=False,
                    message="当前页面不是搜索结果页，请先调用 prepare_search",
                    diagnostics=diagnostics,
                )

            await asyncio.sleep(PANEL_SETTLE_DELAY_SECONDS)
            closed = await self._close_filter_panel(client, diagnostics, "collect")
            diagnostics.append(f"collect_panel_closed={closed}")
            if not closed:
                raise RuntimeError("collect_filter_panel_still_visible")

            consecutive_failures = 0
            max_failures = 3

            while len(notes) < note_limit:
                # Re-scan for targets in every iteration to handle DOM updates/reloads
                targets = await self._gather_note_targets(client, limit=20)
                
                # Get current viewport info
                viewport_info = await self._safe_evaluate(client, "({scrollY: window.scrollY, innerHeight: window.innerHeight})", raw=True)
                scroll_y = viewport_info.get("scrollY", 0)
                inner_height = viewport_info.get("innerHeight", 0)
                viewport_bottom = scroll_y + inner_height

                # Filter targets: unvisited AND visible in viewport
                visible_targets = []
                for t in targets:
                    if t['noteId'] in visited_ids:
                        continue
                    # t['y'] is absolute Y coordinate
                    if scroll_y <= t['y'] <= viewport_bottom:
                        visible_targets.append(t)
                
                if not visible_targets:
                    diagnostics.append("no_visible_targets_found")
                    # Scroll down gently to find more
                    scroll_ratio = random.uniform(0.3, 0.5)
                    scroll_amount = int(inner_height * scroll_ratio)
                    
                    # Revert to JS Scroll (Stable)
                    await client.evaluate(f"window.scrollBy({{top: {scroll_amount}, behavior: 'smooth'}});")
                    await asyncio.sleep(random.uniform(2.0, 3.0))
                    continue

                # Sort by Y (row) then X (column)
                visible_targets.sort(key=lambda t: (round(t['y'] / 10) * 10, t['x']))
                target = visible_targets[0]
                
                visited_ids.add(target['noteId'])
                diagnostics.append(f"processing_note={target['noteId']}")
                
                # --- START COMMENT COLLECTION SETUP ---
                # We start listening BEFORE clicking/navigating to catch initial requests
                global current_note_comments
                current_note_comments = []
                # Refine pattern to strictly match the comment API and avoid images
                comment_api_pattern = re.compile(r"api/sns/web/v2/comment/page") 
                pending_comment_requests = set()
                processed_comment_requests = set()
                fetch_tasks = set()
                
                # --- SETUP NETWORK INTERCEPTION (FETCH DOMAIN) ---
                # Use Fetch domain to pause requests and guarantee body retrieval
                
                # --- SETUP NETWORK INTERCEPTION (NETWORK DOMAIN - NON-BLOCKING) ---
                # Reverting to Network domain to avoid hanging the page. 
                # If getResponseBody fails, we will rely on DOM extraction.
                
                async def on_response(event: dict) -> None:
                    try:
                        params = event.get("params", {})
                        resp = params.get("response", {})
                        url = resp.get("url", "")
                        req_id = params.get("requestId")
                        status = resp.get("status", 0)
                        
                        if comment_api_pattern.search(url):
                            # logger.info(f"Potential comment API intercepted: {url} (Status: {status})")
                            if status == 200 and req_id not in processed_comment_requests:
                                pending_comment_requests.add(req_id)
                    except Exception as e:
                        logger.warning(f"Error processing response event: {e}")

                async def on_loading_finished(event: dict) -> None:
                    try:
                        params = event.get("params", {})
                        req_id = params.get("requestId")
                        
                        if req_id in pending_comment_requests:
                            if req_id in processed_comment_requests:
                                pending_comment_requests.discard(req_id)
                                return

                            logger.info(f"Loading finished for comment request {req_id}")
                            pending_comment_requests.discard(req_id)
                            processed_comment_requests.add(req_id)
                            
                            # Launch background fetch
                            task = asyncio.create_task(fetch_body(req_id))
                            fetch_tasks.add(task)
                            task.add_done_callback(fetch_tasks.discard)
                    except Exception as e:
                        logger.warning(f"Error processing loading finished: {e}")

                async def fetch_body(req_id: str):
                    try:
                        # Small delay to allow buffer to settle
                        await asyncio.sleep(0.5)
                        
                        body_data = await client.send("Network.getResponseBody", {"requestId": req_id})
                        body_content = body_data.get("body")
                        is_base64 = body_data.get("base64Encoded", False)
                        
                        if body_content:
                            import base64
                            if is_base64:
                                body_str = base64.b64decode(body_content).decode('utf-8')
                            else:
                                body_str = body_content
                                
                            data = json.loads(body_str)
                            comments = self._parse_comment_response(data)
                            if comments:
                                logger.info(f"Parsed {len(comments)} comments from Network response {req_id}")
                                current_note_comments.extend(comments)
                        else:
                            logger.warning(f"Empty body for {req_id}")

                    except Exception as e:
                        # It is expected that this might fail with "No resource with given identifier"
                        # We will just log it and rely on DOM fallback.
                        logger.debug(f"Could not fetch body for {req_id} (using DOM fallback): {e}")

                # Enable Network domain
                await client.send("Network.enable", {
                    "maxResourceBufferSize": 100 * 1024 * 1024,
                    "maxTotalBufferSize": 200 * 1024 * 1024
                })
                client.on("Network.responseReceived", on_response)
                client.on("Network.loadingFinished", on_loading_finished)
                # --- END SETUP ---                # --- END SETUP ---

                # Click logic
                clicked, click_reason = await self._click_note_card(client, target)
                if not clicked:
                    diagnostics.append(f"click_failed={target['noteId']}:{click_reason}")
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
                        logger.warning("Too many consecutive click failures, stopping.")
                        break
                    continue
                
                consecutive_failures = 0 

                # Wait for navigation to start
                await asyncio.sleep(2.0)
                navigated = await self._ensure_note_page(client, target["noteId"], target["url"])

                
                note = await self._extract_note_detail(client, target["noteId"], target["url"])
                
                if note:
                    # Trigger scrolling to load more comments
                    try:
                        # _collect_comments now returns DOM-extracted comments as fallback
                        dom_comments = await self._collect_comments(client, target["noteId"])
                        logger.info(f"Returned from _collect_comments with {len(dom_comments)} comments")
                        
                        # Merge network comments and DOM comments
                        # We prioritize network comments (more data), but add DOM ones if missing
                        # Simple merge: add DOM comments that don't match existing content
                        existing_contents = {c.content for c in current_note_comments}
                        for dc in dom_comments:
                            if dc.content not in existing_contents:
                                current_note_comments.append(dc)
                        
                        # Flatten and assign comments
                        flat_comments = self._flatten_comments(current_note_comments)
                        note.comments = flat_comments
                        
                        # Generate hot comments summary
                        if flat_comments:
                            top_5 = sorted(flat_comments, key=lambda c: c.likes, reverse=True)[:5]
                            summary = "\n".join([f"{c.nickname}: {c.content} ({c.likes} likes)" for c in top_5])
                            note.hot_comments_summary = summary
                            
                        diagnostics.append(f"comments_collected={len(flat_comments)}")
                    except Exception as e:
                        logger.warning(f"Failed to collect comments for {note.note_id}: {e}")
                        diagnostics.append(f"comment_collection_failed={e}")

                    notes.append(note)
                    logger.info(f"Appended note {note.note_id} to notes list. Total notes: {len(notes)}")
                    diagnostics.append(f"note_count={len(notes)}")
                else:
                    diagnostics.append(f"extract_failed={target['noteId']}")

                await asyncio.sleep(random.uniform(*NOTE_OPEN_DELAY_RANGE))
                
                # Cleanup listener for this note
                # Note: CDP client doesn't have 'off', so we just leave it. 
                # The next iteration will define a new 'on_response' but 'client.on' might stack them.
                # Ideally we should clear listeners. 
                # Since we can't easily remove, we rely on the fact that 'captured_request_ids' is local to this scope
                # and the previous listeners will just error out or do nothing if they reference closed scope variables?
                # Actually, python closures keep variables alive. 
                # To avoid memory leak/duplicate processing, we should probably clear listeners if possible.
                # But for now, let's just proceed. The 'comment_api_pattern' check is strict.
                
                # Return to list
                await client.evaluate("window.history.back();")
                await client.wait_for_ready(timeout=25)
                await asyncio.sleep(random.uniform(*NOTE_RETURN_DELAY_RANGE))

            diagnostics.append(f"note_collected={len(notes)}")
            diagnostics.append(f"note_shortfall={max(0, note_limit - len(notes))}")
            success = len(notes) > 0

            message = "成功获取笔记详情" if success else "未能从当前页面提取到笔记详情"

            return NoteDetailBatchResponse(
                success=success,
                message=message,
                notes=notes,
                diagnostics=diagnostics,
            )

        except Exception as e:
            logger.error(f"Error in collect_note_details: {e}")
            diagnostics.append(f"error={str(e)}")
            return NoteDetailBatchResponse(
                success=False,
                message=f"采集过程出错: {str(e)}",
                diagnostics=diagnostics,
            )
        finally:
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                pass

    async def _close_filter_panel(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
        source: str,
    ) -> bool:
        for attempt in range(1, 4):
            try:
                result = await client.evaluate(CLOSE_FILTER_PANEL_SCRIPT)
                diagnostics.append(f"{source}_panel_click_{attempt}={result.get('method', 'none')}")
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"{source}_panel_close_error_{attempt}={exc}")
                result = {"clicked": False}
            await asyncio.sleep(PANEL_SETTLE_DELAY_SECONDS)
            try:
                visible = await client.evaluate(_FILTER_PANEL_VISIBLE_EXPR)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"{source}_panel_visible_error_{attempt}={exc}")
                visible = False
            diagnostics.append(f"{source}_panel_visible_{attempt}={bool(visible)}")
            if not visible:
                return True
        return False

    async def _gather_note_targets(self, client: ChromeDevToolsClient, limit: int) -> List[dict]:
        script = _build_collect_note_targets_script(limit)
        result = await self._safe_evaluate(client, script, raw=True)
        if not isinstance(result, list):
            return []
        targets: List[dict] = []
        for entry in result:
            note_id = entry.get("noteId")
            selector = entry.get("selector")
            url = entry.get("url")
            x = entry.get("x")
            y = entry.get("y")
            if note_id and selector and url and x is not None and y is not None:
                targets.append({"noteId": note_id, "selector": selector, "url": url, "x": x, "y": y})
        
        if not targets:
            # Force a scroll to trigger lazy load and retry once.
            await client.evaluate("window.scrollBy(0, window.innerHeight);")
            await asyncio.sleep(1.0)
            result = await self._safe_evaluate(client, script, raw=True)
            if isinstance(result, list):
                for entry in result:
                    note_id = entry.get("noteId")
                    selector = entry.get("selector")
                    url = entry.get("url")
                    x = entry.get("x")
                    y = entry.get("y")
                    if note_id and selector and url and x is not None and y is not None:
                        targets.append({"noteId": note_id, "selector": selector, "url": url, "x": x, "y": y})
        return targets

    async def _click_note_card(self, client: ChromeDevToolsClient, target: dict) -> tuple[bool, str]:
        selector = target["selector"]
        
        # 1. Get fresh viewport coordinates and scroll into view
        # We do this dynamically to ensure accuracy even if page layout shifted
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
            return False, "element_not_found_or_hidden"

        # 2. Human-like Mouse Movement (CDP)
        try:
            # Move to random point near target first
            start_x = coords['x'] + random.randint(-50, 50)
            start_y = coords['y'] + random.randint(-50, 50)
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": start_x,
                "y": start_y,
                "buttons": 0
            })
            await asyncio.sleep(random.uniform(0.1, 0.2))
            
            # Move to exact target
            await client.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": coords['x'],
                "y": coords['y'],
                "buttons": 0
            })
            await asyncio.sleep(random.uniform(0.1, 0.2))
            
            # 3. Physical Click (CDP)
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
            
            return True, ""
            
        except Exception as e:
            logger.warning(f"CDP click failed: {e}, falling back to JS click")
            
            # Fallback: JS Click
            click_script = CLICK_NOTE_CARD_TEMPLATE.replace("__SELECTOR__", json.dumps(selector))
            clicked = await self._safe_evaluate(client, click_script, raw=True)
            
            if isinstance(clicked, dict) and clicked.get("clicked"):
                return True, "js_fallback"
            
            return False, "click_failed_exception"

    async def _extract_note_detail(
        self,
        client: ChromeDevToolsClient,
        note_id: str,
        note_url: str,
    ) -> Optional[NoteDetail]:
        note_identifier = json.dumps(note_id)
        state_script = NOTE_STATE_TEMPLATE.replace("__NOTE_ID__", note_identifier)
        result: Optional[dict] = None

        for attempt in range(30):
            result = await self._safe_evaluate(client, state_script, raw=True)

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

            await asyncio.sleep(0.3 if attempt < 4 else 0.6)

        logger.warning("Note {} not ready in __INITIAL_STATE__ (reason={})", note_id, result)
        return None

    async def _ensure_note_page(self, client: ChromeDevToolsClient, note_id: str, note_url: str) -> bool:
        """Ensure current page is /explore/{note_id}."""
        
        # 1. Wait for SPA navigation (up to 5 seconds)
        for _ in range(10):
            current = await self._safe_evaluate(client, "window.location.href")
            if isinstance(current, str) and note_id in current and "/explore/" in current:
                return True
            await asyncio.sleep(0.5)

        # 2. If still not there, Force Navigate (Hard Reload)
        # NOTE: This might lose previous page state (search keywords), but it's a last resort.
        logger.warning(f"SPA navigation failed for {note_id}, forcing hard navigation.")
        await client.navigate(note_url)
        await asyncio.sleep(1.0)

        # 3. Verify again
        for _ in range(10):
            current = await self._safe_evaluate(client, "window.location.href")
            if isinstance(current, str) and note_id in current and "/explore/" in current:
                return True
            await asyncio.sleep(0.5)

        return False

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
                
                // Share (often doesn't have count text, but check anyway)
                const shareEl = document.querySelector('.interact-container .share-wrapper .count');
                if (shareEl) data.share_count = parseCount(shareEl.innerText);

            } catch(e) {}
            return data;
        })()
        """
        try:
            return await self._safe_evaluate(client, script, raw=True)
        except Exception as e:
            logger.warning(f"DOM fallback extraction failed: {e}")
            return {}

    @staticmethod
    async def _safe_evaluate(
        client: ChromeDevToolsClient,
        expression: str,
        raw: bool = False,
    ):
        try:
            return await client.evaluate(expression)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evaluate script failed: %s", exc)

    async def _collect_comments(self, client: ChromeDevToolsClient, note_id: str) -> List[Comment]:
        """
        Scroll to trigger lazy loading of comments.
        Note: The network listener should already be active.
        """
        logger.info(f"Scrolling to load comments for {note_id} (using JS scrollBy)...")
        
        # Use robust JS scrolling on the specific container
        # This avoids focus issues where PageDown might scroll the wrong element
        scroll_script = """
        (() => {
            const scroller = document.querySelector('.note-scroller');
            if (scroller) {
                scroller.scrollBy({ top: scroller.clientHeight, behavior: 'smooth' });
                return true;
            }
            return false;
        })()
        """
        
        for _ in range(3):  # Reduced from 5 iterations
            await self._safe_evaluate(client, scroll_script)
            await asyncio.sleep(0.5)  # Reduced from 0.8s
            
        # Wait a bit for final responses
        await asyncio.sleep(1.0)  # Reduced from 2.0s


        # Fallback: Extract comments from DOM if network failed
        dom_comments = []
        try:
            extract_script = """
            (() => {
                const comments = [];
                const seen = new Set();
                // Try multiple selectors but be careful about nesting
                // .comment-item is usually the main container
                const items = document.querySelectorAll('.comment-item');
                
                items.forEach(item => {
                    try {
                        const contentEl = item.querySelector('.comment-content, .content, .note-text');
                        const userEl = item.querySelector('.user-name, .name, .author-name');
                        // Try more selectors for like count
                        const likeEl = item.querySelector('.like .count, .like-wrapper .count, .like-count');
                        
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
                                likes: likeCount
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
                        create_time=0
                    ))
                logger.info(f"Extracted {len(dom_comments)} comments from DOM as fallback")
        except Exception as e:
            logger.warning(f"DOM comment extraction failed: {e}")

        # Merge network comments and DOM comments
        # Prioritize network comments as they have more data
        all_comments = {c.id: c for c in current_note_comments if c.id}
        
        # Deduplication set based on content signature (nickname + content)
        # This prevents adding DOM comments that are already present from network (even if IDs differ)
        existing_signatures = {(c.nickname, c.content) for c in all_comments.values()}

        for c in dom_comments:
            # If we have a real ID and it's already there, skip
            if c.id and not c.id.startswith("dom_") and c.id in all_comments:
                continue
            
            # Check content signature to avoid duplicates with different IDs
            signature = (c.nickname, c.content)
            if signature in existing_signatures:
                continue
                
            # Otherwise add it
            if c.id not in all_comments:
                all_comments[c.id] = c
                existing_signatures.add(signature)
            
        return list(all_comments.values())

    def _parse_comment_response(self, body: str) -> List[Comment]:
        """Parse comments from the network response body."""
        try:
            data = json.loads(body)
            # Handle standard XHS response structure
            # Usually: { "data": { "comments": [...] } } or { "data": { "cursor_comments": [...] } }
            
            # Navigate to 'data' if present
            if "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            
            comments_data = []
            # Check common keys for comments
            if "comments" in data:
                comments_data = data["comments"]
            elif "cursor_comments" in data:
                comments_data = data["cursor_comments"]
            elif isinstance(data, list):
                comments_data = data
                
            if not comments_data:
                logger.warning(f"No comments found in response data. Keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                return []

            results = []
            for item in comments_data:
                try:
                    results.append(self._parse_single_comment(item))
                except Exception as e:
                    logger.warning(f"Failed to parse single comment: {e}")
                    continue
            
            return results
            
        except json.JSONDecodeError:
            logger.warning("Failed to decode JSON body from comment response")
            return []
        except Exception as e:
            logger.warning(f"Error parsing comment response: {e}")
            return []

    def _parse_single_comment(self, item: dict) -> Comment:
        user_info = item.get("user_info", {})
        sub_comments = []
        
        # Parse sub-comments (replies)
        for sub in item.get("sub_comments", []) or []:
            sub_comments.append(self._parse_single_comment(sub))
             
        return Comment(
            id=item.get("id", ""),
            user_id=user_info.get("user_id", ""),
            nickname=user_info.get("nickname", "Unknown"),
            content=item.get("content", ""),
            likes=int(item.get("like_count", item.get("likes", 0))),
            create_time=int(item.get("create_time", 0)),
            sub_comments=sub_comments,
            parent_id=item.get("target_comment_id") # This might be present in replies
        )

    def _flatten_comments(self, comments: List[Comment]) -> List[Comment]:
        """Flatten the nested comment structure for spreadsheet export."""
        flat_list = []
        
        def traverse(comment: Comment, parent_id: Optional[str] = None):
            # Create a copy to avoid modifying the original if needed, 
            # but here we just want a flat list of objects.
            # We explicitly set parent_id if it's missing and we know the parent.
            if parent_id and not comment.parent_id:
                comment.parent_id = parent_id
            
            # Add self
            flat_list.append(comment)
            
            # Traverse children
            for sub in comment.sub_comments:
                traverse(sub, parent_id=comment.id)
                
            # Clear sub_comments in the flat version to avoid deep nesting in output?
            # Actually, for the "Comments Table", we want flat rows.
            # But we might want to keep sub_comments in the object for other uses.
            # Let's keep them but the spreadsheet logic will ignore them if it iterates flat_list.
            pass

        for c in comments:
            traverse(c)
            
        return flat_list






    def _build_note_model(self, payload: dict, fallback_url: str) -> NoteDetail:


        note_id = payload.get("noteId") or ""
        
        # Debug: Check for comments in initial state
        logger.info(f"Payload keys for {note_id}: {list(payload.keys())}")
        if "comments" in payload:
            logger.info(f"Found 'comments' in payload for {note_id}: {len(payload['comments'])} items")
        if "commentList" in payload:
            logger.info(f"Found 'commentList' in payload for {note_id}: {len(payload['commentList'])} items")


        title = (payload.get("title") or payload.get("name") or "").strip()


        desc = (payload.get("desc") or "").strip()


        processed_content = self._clean_content(desc)


        user = payload.get("user") or {}


        interact_info = payload.get("interactInfo") or {}


        like_count = _parse_count(interact_info.get("likedCount") or 0)


        collect_count = _parse_count(interact_info.get("collectedCount") or 0)


        comment_count = _parse_count(interact_info.get("commentCount") or 0)


        share_count = _parse_count(interact_info.get("shareCount") or 0)


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


            content=processed_content,


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


    def _clean_content(desc: str) -> str:


        content = desc or ""


        if not content:


            return ""


        content = content.replace("\r\n", "\n")


        content = content.replace("\r", "\n")


        return content.strip()


    @staticmethod


    def _format_timestamp(value: Optional[int]) -> Optional[str]:


        if not value:


            return None


        try:


            dt = datetime.fromtimestamp(value / 1000)


            return dt.strftime("%Y-%m-%d")


        except (OSError, ValueError):


            return None


    @staticmethod


    def _collect_tags(tag_list: list, desc: str) -> List[str]:


        tags: List[str] = []


        for tag in tag_list:


            if isinstance(tag, dict):


                name = tag.get("name") or tag.get("title")


            else:


                name = str(tag)


            if not name:


                continue


            formatted = f"#{name.lstrip('#')}"


            if formatted not in tags:


                tags.append(formatted)


        if not tags and desc:


            matches = re.findall(r"#([\w\u4e00-\u9fa5]+)", desc)


            for match in matches:


                formatted = f"#{match}"


                if formatted not in tags:


                    tags.append(formatted)


        return tags


    @staticmethod


    def _collect_images(image_list: list) -> List[str]:


        images: List[str] = []


        for img in image_list:


            if not isinstance(img, dict):


                continue


            url = (


                img.get("urlDefault")


                or img.get("url")


                or img.get("urlPre")


                or img.get("originUrl")


            )


            if url:


                images.append(url)


        return images


    @staticmethod


    def _collect_videos(video_entry: Optional[dict], image_list: list) -> List[str]:


        videos: List[str] = []


        if isinstance(video_entry, dict):


            stream = video_entry.get("media", {}).get("stream", {})


            videos.extend(_extract_stream_urls(stream))


        for img in image_list:


            if not isinstance(img, dict):


                continue


            stream = img.get("stream", {})


            videos.extend(_extract_stream_urls(stream))


        # De-duplicate order-preserving


        seen = set()


        unique: List[str] = []


        for url in videos:


            if url and url not in seen:


                seen.add(url)


                unique.append(url)


        return unique


def _extract_stream_urls(stream: dict) -> List[str]:
    """
    Extract the single best video URL from the stream info.
    Priority: h264 master > h265 master > h264 backup > h265 backup
    """
    if not isinstance(stream, dict):
        return []

    # Priority order for codecs
    for key in ("h264", "h265"):
        candidates = stream.get(key) or []
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            
            # Return the first masterUrl found (highest priority)
            url = entry.get("masterUrl")
            if url:
                return [url]
            
            # If no master, check backups immediately
            backups = entry.get("backupUrls") or []
            if backups:
                return [backups[0]]

    return []





def _build_collect_note_targets_script(limit: int) -> str:


    return COLLECT_NOTE_TARGETS_TEMPLATE.format(limit=limit)


COLLECT_NOTE_TARGETS_TEMPLATE = """


(() => {{


  const limit = {limit};


  const cards = Array.from(document.querySelectorAll('section.note-item, div[class*="note-item"], div[class*="note-card"]'));


  const seen = new Set();


  const notes = [];


  for (const card of cards) {{


    const exploreLink = card.querySelector('a[href*="/explore/"]');


    const coverLink = card.querySelector('a.cover[href*="/search_result/"]') || card.querySelector('a[href*="/search_result/"]');


    const clickLink = coverLink || exploreLink;


    if (!clickLink) continue;


    const hrefCandidates = [];


    if (exploreLink) {{


      hrefCandidates.push(exploreLink.getAttribute('href'));


    }}


    hrefCandidates.push(clickLink.getAttribute('href'));


    let noteId = null;


    let canonicalUrl = null;


    for (const href of hrefCandidates) {{


      if (!href) continue;


      let absolute = href;


      if (!absolute.startsWith('http')) {{


        absolute = new URL(absolute, window.location.origin).href;


      }}


      const match = absolute.match(/(?:explore|search_result)\\/([0-9a-z]+)/i);


      if (match) {{


        noteId = match[1];


        canonicalUrl = `https://www.xiaohongshu.com/explore/${{noteId}}`;


        break;


      }}


    }}


    if (!noteId || seen.has(noteId)) continue;


    if (!canonicalUrl) {{


      canonicalUrl = new URL(`/explore/${{noteId}}`, window.location.origin).href;


    }}


    const rect = clickLink.getBoundingClientRect();


    const x = rect.left + rect.width / 2 + window.scrollX;


    const y = rect.top + rect.height / 2 + window.scrollY;


    const marker = `data-mcp-link-${{Date.now()}}-${{notes.length}}`;


    clickLink.setAttribute('data-mcp-link', marker);


    const selector = `[data-mcp-link=\"${{marker}}\"]`;


    notes.push({{ noteId, url: canonicalUrl, selector, x, y }});


    seen.add(noteId);


    if (notes.length >= limit) break;


  }}


  return notes;


}})()


"""


CLICK_SCROLL_TEMPLATE = """


(() => {{


  const selector = __SELECTOR__;


  const target = document.querySelector(selector);


  if (!target) {{


    return false;


"""


CLICK_NOTE_CARD_TEMPLATE = """


(() => {{


  const selector = __SELECTOR__;


  const target = document.querySelector(selector);


  if (!target) {{


    return {{ clicked: false, reason: 'not_found' }};


  }}


  if (target.getAttribute('target') && target.getAttribute('target') !== '_self') {{


    target.setAttribute('target', '_self');


  }}


  target.dispatchEvent(new MouseEvent('mouseover', {{ bubbles: true }}));


  target.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));


  target.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));


  target.click();


  return {{ clicked: true }};


}})()


"""


NOTE_STATE_TEMPLATE = """


(() => {


  const targetNoteId = __NOTE_ID__;


  if (!window.__INITIAL_STATE__ || !window.__INITIAL_STATE__.note) {


    return { ready: false, reason: 'missing_initial_state' };


  }


  const noteState = window.__INITIAL_STATE__.note;


  const detailMap = noteState.noteDetailMap || {};


  let foundId = targetNoteId;


  let entry = detailMap[targetNoteId];


  if (!entry) {


    const keys = Object.keys(detailMap);


    for (const key of keys) {


      if (!key) continue;


      if (key.includes(targetNoteId) || targetNoteId.includes(key)) {


        entry = detailMap[key];


        foundId = key;


        break;


      }


    }


    // Dangerous fallback removed: if (!entry && keys.length === 1) ...
    // This caused issues where stale state from a previous note was used.


  }


  let note = null;


  if (entry) {


    note = entry.note || entry;


  } else if (noteState.note) {


    note = noteState.note;


    foundId = note.noteId || note.id || targetNoteId;


  }


  if (!note) {


    return { ready: false, reason: 'note_missing' };


  }


  if (!(note.title || note.desc || note.user)) {


    return { ready: false, reason: 'note_incomplete' };


  }


  return {


    ready: true,


    noteId: note.noteId || note.id || foundId || targetNoteId,


    payload: note


  };


})()


"""



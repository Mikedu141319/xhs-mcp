"""Service driving Xiaohongshu search interactions."""



from __future__ import annotations







import asyncio



import json



from typing import Dict, Optional





import httpx



from loguru import logger



from websockets import exceptions as ws_exceptions







from src.clients.chrome_devtools import ChromeDevToolsClient



from src.config import chrome_entry_url



from src.schemas.search import SearchRequest, SearchResponse











FILTER_SETTLE_DELAY_SECONDS = 1.0





FILTER_TEXT = {

    "sort_by": {



        "comprehensive": "综合",



        "latest": "最新",



        "most_liked": "最多点赞",



        "most_collected": "最多收藏",



        "most_commented": "最多评论",
        "most_comment": "最多评论",
        "comment_count": "最多评论",



    },



    "note_type": {



        "all": None,



        "video": "视频",



        "image": "图文",



    },



    "publish_time": {
        "any": None,
        "day": "一天内",
        "week": "一周内",
        "half_year": "半年内",
        "within_a_week": "一周内",
    },



    "search_scope": {
        "all": None,



        "seen": "已看过",



        "unseen": "未看过",



        "following": "已关注",



    },



    "location": {



        "all": None,



        "same_city": "同城",



        "nearby": "附近",



    },



}











class SearchService:



    """Encapsulates keyword search and filter automation."""







    def __init__(self) -> None:



        self.entry_url = chrome_entry_url()







    async def run_search(self, request: SearchRequest) -> SearchResponse:



        client = ChromeDevToolsClient(initial_url=self.entry_url)



        diagnostics: list[str] = [f"keyword={request.keyword}", f"note_limit={request.note_limit}"]







        try:



            await client.navigate(self.entry_url)



            page_ready = await client.wait_for_ready()



            diagnostics.append(f"entry_ready={page_ready}")







            navigation = await self._submit_keyword(client, request.keyword)



            diagnostics.append(f"keyword_submitted={navigation.get('ok', False)}")



            if not navigation.get("ok"):



                raise RuntimeError(navigation.get("reason", "keyword_submit_failed"))







            url_info = await client.wait_for_expression(_SEARCH_RESULT_READY_EXPR, timeout=8)



            if not url_info:



                raise RuntimeError("search_result_navigation_timeout")



            diagnostics.append(f"page_url={url_info.get('url')}")







            applied_filters = await self._apply_filters(client, request, diagnostics)



            return SearchResponse(



                success=True,



                message="Search page prepared. Ready to open note details.",



                page_url=url_info.get("url"),



                applied_filters=applied_filters,



                diagnostics=diagnostics,



            )



        except httpx.HTTPError as exc:



            diagnostics.append(f"http_error={exc}")



            logger.error("Chrome DevTools HTTP error while searching: {}", exc)



            return SearchResponse(



                success=False,



                message="无法连接 Chrome DevTools，请确认浏览器已使用 --remote-debugging-port 启动。",



                diagnostics=diagnostics,



            )



        except (ws_exceptions.WebSocketException, OSError, RuntimeError) as exc:



            diagnostics.append(f"ws_error={exc}")



            logger.error("Chrome DevTools websocket error while searching: {}", exc)



            return SearchResponse(



                success=False,



                message="Chrome websocket 连接失败，请检查 9222 端口状态。",



                diagnostics=diagnostics,



            )



        except Exception as exc:  # noqa: BLE001



            diagnostics.append(f"unexpected_error={exc}")



            logger.exception("Unexpected error while executing search")



            return SearchResponse(



                success=False,



                message="搜索流程发生异常，请查看 logs/ 目录中的日志。",



                diagnostics=diagnostics,



            )



        finally:



            try:



                await asyncio.wait_for(client.close(), timeout=2)



            except Exception:



                pass







    async def _apply_filters(



        self,



        client: ChromeDevToolsClient,



        request: SearchRequest,



        diagnostics: list[str],



    ) -> Dict[str, str]:



        applied: Dict[str, str] = {}



        desired_options: Dict[str, Optional[str]] = {



            "sort_by": FILTER_TEXT["sort_by"][request.sort_by],



            "note_type": FILTER_TEXT["note_type"][request.note_type],



            "publish_time": FILTER_TEXT["publish_time"][request.publish_time],



            "search_scope": FILTER_TEXT["search_scope"][request.search_scope],



            "location": FILTER_TEXT["location"][request.location],



        }







        need_panel = any(option for option in desired_options.values())



        if not need_panel:



            diagnostics.append("filters=defaults")



            return applied







        open_result = await client.evaluate(_build_filter_button_click_script())



        diagnostics.append(f"filter_panel_open={open_result.get('ok', False)}")



        if not open_result.get("ok"):



            raise RuntimeError(open_result.get("reason", "failed_to_open_filter"))







        panel_ready = await client.wait_for_expression(_FILTER_PANEL_READY_EXPR, timeout=5)



        if not panel_ready:



            raise RuntimeError("filter_panel_timeout")







        for field, target_text in desired_options.items():



            if not target_text:



                continue



            result = await client.evaluate(_build_select_option_script(target_text))



            success = result.get("ok", False)



            diagnostics.append(f"{field}={target_text}:{success}")



            if not success:



                raise RuntimeError(result.get("reason", f"{field}_not_found"))



            applied[field] = target_text
            await asyncio.sleep(FILTER_SETTLE_DELAY_SECONDS)

            await asyncio.sleep(FILTER_SETTLE_DELAY_SECONDS)







        await asyncio.sleep(FILTER_SETTLE_DELAY_SECONDS)
        await asyncio.sleep(FILTER_SETTLE_DELAY_SECONDS)
        closed = await self._close_filter_panel(client, diagnostics, "search")
        diagnostics.append(f"search_panel_closed={closed}")
        if not closed:
            raise RuntimeError("filter_panel_still_visible")



        return applied







    async def _close_filter_panel(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
        source: str,
    ) -> bool:
        for attempt in range(1, 4):
            try:
                result = await client.evaluate(_CLOSE_FILTER_PANEL_SCRIPT)
                diagnostics.append(f"{source}_panel_click_{attempt}={result.get('method', 'none')}")
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"{source}_panel_close_error_{attempt}={exc}")
                result = {"clicked": False}
            await asyncio.sleep(FILTER_SETTLE_DELAY_SECONDS)
            try:
                visible = await client.evaluate(_FILTER_PANEL_VISIBLE_EXPR)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"{source}_panel_visible_error_{attempt}={exc}")
                visible = False
            diagnostics.append(f"{source}_panel_visible_{attempt}={bool(visible)}")
            if not visible:
                return True
        return False







    async def _submit_keyword(self, client: ChromeDevToolsClient, keyword: str) -> Dict[str, object]:



        script = _build_keyword_submit_script(keyword)



        return await client.evaluate(script)











def _build_filter_button_click_script() -> str:



    return """



(() => {



  const candidates = Array.from(document.querySelectorAll('button, [role="button"], span'));



  let toggle = candidates.find((el) => {



    if (!el || !el.isConnected) return false;



    const text = (el.innerText || '').trim();



    return text.startsWith('筛选');



  });



  const indicator = document.querySelector('span, button[aria-label*="筛选"]');



  const alreadyOpen = indicator && indicator.innerText.includes('已筛选');



  if (!toggle && indicator) {



    toggle = indicator;



  }



  if (!toggle) {



    return { ok: false, reason: 'filter_button_missing' };



  }



  if (alreadyOpen) {



    toggle.click();



  }



  toggle.click();



  return { ok: true };



})()



"""











_FILTER_PANEL_READY_EXPR = """
(() => {
  const panel = document.querySelector('.filter-panel');
  if (panel) {
    window.__xhsFilterPanel = panel;
    return true;
  }
  return false;
})()
"""











def _build_select_option_script(option_text: str) -> str:



    escaped = json.dumps(option_text)



    return f"""



(() => {{



  const targetText = {escaped};



  const normalizedTarget = targetText.replace(/\\s+/g, '');



  const ensurePanel = () => {{
    let panel = window.__xhsFilterPanel;
    if (panel && panel.isConnected) {{
      return panel;
    }}
    panel = document.querySelector('.filter-panel');
    if (panel) {{
      window.__xhsFilterPanel = panel;
    }}
    return panel;
  }};

  let panel = ensurePanel();
  if (!panel) {{
    panel = document.body;
  }}

  const nodes = Array.from(panel.querySelectorAll('button, [role="button"], span'));



  const match = nodes.find((el) => {{



    if (!el || !el.isConnected || el.offsetParent === null) return false;



    const text = (el.innerText || '').trim();



    if (!text) return false;



    const normalized = text.replace(/\\s+/g, '');



    return text === targetText || normalized === normalizedTarget || text.includes(targetText);



  }});



  if (!match) {{



    return {{ ok: false, reason: `option_not_found_${{targetText}}` }};



  }}



  match.click();



  return {{ ok: true }};



}})()



"""











_CLOSE_FILTER_PANEL_SCRIPT = """
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





def _build_keyword_submit_script(keyword: str) -> str:



    escaped = json.dumps(keyword)



    return f"""



(() => {{



  const value = {escaped};



  const input = document.querySelector('input[placeholder*="搜索"], input[placeholder*="关键词"]');



  if (!input) {{



    return {{ ok: false, reason: 'search_input_missing' }};



  }}



  input.focus();



  input.value = value;



  input.dispatchEvent(new Event('input', {{ bubbles: true }}));



  const enterDown = new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }});



  input.dispatchEvent(enterDown);



  const enterUp = new KeyboardEvent('keyup', {{ key: 'Enter', bubbles: true }});



  input.dispatchEvent(enterUp);



  const enterPress = new KeyboardEvent('keypress', {{ key: 'Enter', bubbles: true }});



  input.dispatchEvent(enterPress);



  const searchButton = document.querySelector('button[type="submit"], [data-search-button]');



  if (searchButton) {{



    searchButton.click();



  }} else {{



    const form = input.closest('form');



    if (form) {{



      form.dispatchEvent(new Event('submit', {{ bubbles: true, cancelable: true }}));



    }}



  }}



  return {{ ok: true }};



}})()



"""











_SEARCH_RESULT_READY_EXPR = """



(() => {



  const url = window.location.href;



  if (!url.includes('search_result')) {



    return null;



  }



  const candidates = Array.from(document.querySelectorAll('button, span, [role="button"]'));



  const filterButton = candidates.find((el) => {



    const text = (el.innerText || '').trim();



    return text.startsWith('筛选');



  });



  if (!filterButton) {



    return null;



  }



  return { url };



})()



"""




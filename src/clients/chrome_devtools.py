"""Async client controlling Chrome via DevTools protocol."""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.client import WebSocketClientProtocol
from loguru import logger

from src.config import CHROME_REMOTE_URL


class ChromeDevToolsClient:
    def __init__(
        self,
        base_url: str | None = None,
        initial_url: str = "about:blank",
    ) -> None:
        self.base_url = base_url or CHROME_REMOTE_URL
        self.initial_url = initial_url
        self.session: Optional[WebSocketClientProtocol] = None
        self.target_id: Optional[str] = None
        self._reused_existing = False
        self._msg_id = 0
        self._lock = asyncio.Lock()
        self._allowed_host = self._derive_host(initial_url)
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._event_handlers: Dict[str, list] = {}
        self._read_task: Optional[asyncio.Task] = None

    async def _create_target(self) -> Tuple[str, str]:
        """
        Create (or reuse) a DevTools target.

        Chrome 142 对 /json/new 的要求改变，部分环境会禁止 GET 或 POST。
        因此我们优先尝试从 /json/list 中复用已有 page；若没有可用 page，再
        回退到老的 create 流程（GET /json/new?url → 405 时改为
        POST /json/new 并在 body 里带 {"url": ...}）。
        """
        async with httpx.AsyncClient(trust_env=False) as client:
            # 1) 先看当前是否已有可用 page
            list_resp = await client.get(f"{self.base_url}/json/list", timeout=5)
            list_resp.raise_for_status()
            targets = list_resp.json()

            fallback_entry: Optional[dict[str, Any]] = None
            for entry in targets:
                if entry.get("type") != "page" or not entry.get("webSocketDebuggerUrl"):
                    continue
                if not fallback_entry:
                    fallback_entry = entry
                if self._can_reuse_target(entry.get("url", "")):
                    return entry["webSocketDebuggerUrl"], "existing"

            # 2) 没有的话再创建新 target
            encoded = quote(self.initial_url, safe=":/?=&%")
            get_url = f"{self.base_url}/json/new?{encoded}"

            try:
                resp = await client.get(get_url, timeout=5)
                if resp.status_code == 405:
                    resp = await client.post(
                        f"{self.base_url}/json/new",
                        json={"url": self.initial_url},
                        timeout=5,
                    )

                resp.raise_for_status()
                data = resp.json()
                return data["webSocketDebuggerUrl"], data["id"]
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 405 and fallback_entry:
                    logger.warning(
                        "Chrome /json/new blocked (405). Reusing existing target {} ({})",
                        fallback_entry.get("title") or fallback_entry.get("url"),
                        fallback_entry.get("id"),
                    )
                    return fallback_entry["webSocketDebuggerUrl"], fallback_entry.get("id", "existing")
                if status == 405:
                    raise RuntimeError(
                        "Chrome 拒绝创建新的 DevTools target。请先在该调试 profile 中手动打开 https://www.xiaohongshu.com 页面后重试。"
                    ) from exc
                raise

    def _derive_host(self, url: str) -> Optional[str]:
        if not url or url.startswith("about:"):
            return None
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        return parsed.hostname.lower()

    def _can_reuse_target(self, target_url: str) -> bool:
        if not target_url:
            return False
        if target_url.startswith("about:blank"):
            return True
        if not self._allowed_host:
            return False
        parsed = urlparse(target_url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        if host == self._allowed_host:
            return True
        return host.endswith(f".{self._allowed_host}")

    async def _ensure_connection_locked(self) -> None:
        if self.session:
            is_closed = getattr(self.session, "closed", True)
            if not is_closed:
                return

        ws_url, target_id = await self._create_target()
        self.target_id = target_id
        self._reused_existing = target_id == "existing"
        self.session = await websockets.connect(ws_url, close_timeout=1, max_size=None)
        logger.debug("Connected to Chrome target {}", target_id)
        
        # Start background read loop
        self._read_task = asyncio.create_task(self._read_loop())

        await self._send_locked("Page.enable")
        await self._send_locked("Runtime.enable")
        await self._send_locked("Network.enable")

    async def _read_loop(self) -> None:
        """Background loop to read messages from websocket."""
        try:
            async for raw in self.session:
                try:
                    data = json.loads(raw)
                    msg_id = data.get("id")
                    
                    if msg_id is not None:
                        # Response to a command
                        if msg_id in self._pending_requests:
                            future = self._pending_requests.pop(msg_id)
                            if not future.done():
                                if "error" in data:
                                    future.set_exception(RuntimeError(data["error"]))
                                else:
                                    future.set_result(data.get("result"))
                    else:
                        # Event
                        method = data.get("method")
                        if method and method in self._event_handlers:
                            for handler in self._event_handlers[method]:
                                try:
                                    await handler(data)
                                except Exception as e:
                                    logger.error(f"Event handler failed for {method}: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
        except Exception as e:
            logger.warning(f"Read loop terminated: {e}")
        finally:
            # Cancel all pending requests
            for future in self._pending_requests.values():
                if not future.done():
                    future.cancel()
            self._pending_requests.clear()

    def on(self, event: str, handler) -> None:
        """Register an event handler."""
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def _send_locked(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        assert self.session
        self._msg_id += 1
        msg_id = self._msg_id

        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        future = asyncio.Future()
        self._pending_requests[msg_id] = future
        
        await self.session.send(json.dumps(payload))
        
        # Wait for response
        return await future

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        async with self._lock:
            await self._ensure_connection_locked()
            return await self._send_locked(method, params)

    async def navigate(self, url: str) -> None:
        await self.send("Page.navigate", {"url": url})

    async def add_script_to_evaluate_on_new_document(self, source: str) -> str:
        """
        Injects JavaScript that runs before any other script on every page load.
        Returns the identifier of the added script.
        """
        result = await self.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
        )
        return result.get("identifier")

    async def evaluate(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        remote = result.get("result", {})
        if "value" in remote:
            return remote["value"]
        return remote

    async def get_cookies(self) -> list[dict[str, Any]]:
        """Return every cookie accessible to the page."""
        result = await self.send("Network.getAllCookies")
        return result.get("cookies", [])

    async def capture_screenshot(
        self,
        full_page: bool = True,
        clip: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        params: Dict[str, Any] = {"format": "png"}
        if clip:
            params["clip"] = clip
        elif full_page:
            try:
                layout_metrics = await self.send("Page.getLayoutMetrics")
                content = layout_metrics.get("contentSize", {})
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": content.get("width", 800),
                    "height": content.get("height", 600),
                    "scale": 1,
                }
            except Exception:
                pass
        result = await self.send("Page.captureScreenshot", params)
        return result.get("data")

    async def dispatch_mouse_event(
        self,
        event_type: str,
        x: float,
        y: float,
        *,
        button: Optional[str] = "left",
        buttons: Optional[int] = 1,
    ) -> None:
        params: Dict[str, Any] = {
            "type": event_type,
            "x": float(x),
            "y": float(y),
            "modifiers": 0,
            "clickCount": 1,
        }
        if button is not None:
            params["button"] = button
        if buttons is not None:
            params["buttons"] = buttons
        await self.send("Input.dispatchMouseEvent", params)

    async def drag_mouse(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        *,
        duration: float = 1.2,
        steps: int = 18,
    ) -> None:
        """Simulate a human-like drag gesture from start to end."""
        steps = max(steps, 2)
        start_x, start_y = start
        end_x, end_y = end
        await self.dispatch_mouse_event("mouseMoved", start_x, start_y, button="none", buttons=0)
        await self.dispatch_mouse_event("mousePressed", start_x, start_y, button="left", buttons=1)

        for step in range(1, steps):
            t = step / steps
            # Smoothstep easing makes the movement feel more human.
            smooth = t * t * (3 - 2 * t)
            jitter_x = random.uniform(-0.6, 0.6)
            jitter_y = random.uniform(-0.4, 0.4)
            x = start_x + (end_x - start_x) * smooth + jitter_x
            y = start_y + (end_y - start_y) * smooth + jitter_y
            await self.dispatch_mouse_event("mouseMoved", x, y, button="none", buttons=1)
            await asyncio.sleep(max(duration / steps, 0.02))

        await self.dispatch_mouse_event("mouseMoved", end_x, end_y, button="none", buttons=1)
        await self.dispatch_mouse_event("mouseReleased", end_x, end_y, button="left", buttons=0)

    async def wait_for_ready(self, timeout: float = 15.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                ready_state = await self.evaluate("document.readyState")
            except Exception:
                ready_state = None
            if ready_state == "complete":
                return True
            await asyncio.sleep(0.5)
        return False

    async def wait_for_expression(
        self,
        expression: str,
        timeout: float = 10.0,
        interval: float = 0.5,
    ) -> Optional[Any]:
        """
        Evaluate ``expression`` repeatedly until it returns a truthy value.

        Args:
            expression: JavaScript snippet returning a truthy value when the condition is met.
            timeout: Maximum time to wait.
            interval: Delay between evaluations.

        Returns:
            The first truthy value returned by the expression, or ``None`` if timed out.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await self.evaluate(expression)
            except Exception:
                result = None
            if result:
                return result
            await asyncio.sleep(interval)
        return None

    async def close(self) -> None:
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
        self.session = None

        if self.target_id and not self._reused_existing:
            close_url = f"{self.base_url}/json/close/{self.target_id}"
            try:
                async with httpx.AsyncClient() as client:
                    await client.get(close_url, timeout=3)
            except Exception:
                pass

        self.target_id = None
        self._reused_existing = False

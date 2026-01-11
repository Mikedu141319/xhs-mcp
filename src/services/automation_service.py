"""High-level workflow that chains search + note collection with login recovery."""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from src.schemas.automation import AutoWorkflowRequest, AutoWorkflowResponse
from src.schemas.login import LoginStatusResponse
from src.services.login_service import LoginService
from src.services.note_service import NoteDetailService
from src.services.search_service import SearchService


_POLLABLE_STATES = {"needs_qr_scan", "captcha_gate", "unknown"}


class AutomationService:
    """Coordinates login verification, search preparation, and note collection."""

    def __init__(
        self,
        login_service: Optional[LoginService] = None,
        search_service: Optional[SearchService] = None,
        note_service: Optional[NoteDetailService] = None,
        browser_guard: Optional["BrowserGuard"] = None,
    ) -> None:
        self.login_service = login_service or LoginService()
        self.search_service = search_service or SearchService()
        self.note_service = note_service or NoteDetailService()
        # import placed lazily to avoid circular reference at module import time
        if browser_guard is None:
            from src.utils.browser_guard import BrowserGuard  # noqa: WPS433

            browser_guard = BrowserGuard()
        self.browser_guard = browser_guard

    async def run_auto_workflow(self, request: AutoWorkflowRequest) -> AutoWorkflowResponse:
        diagnostics = [f"keyword={request.keyword}", f"note_limit={request.note_limit}"]

        async with self.browser_guard.lifecycle() as started_browser:
            diagnostics.append(f"browser_started_here={started_browser}")

            login_response = await self._wait_for_login(
                attempts=request.login_retry_limit,
                interval=request.login_retry_interval,
                diagnostics=diagnostics,
                context="login",
            )
            login_status = login_response.status
            if not login_response.success:
                message = login_status.message if login_status else "登录状态不可用，请扫码或重新验证"
                return AutoWorkflowResponse(
                    success=False,
                    stage="login",
                    message=message,
                    login_status=login_status,
                    diagnostics=diagnostics,
                )

            search_request = request.to_search_request()
            search_response = await self.search_service.run_search(search_request)
            diagnostics.extend(search_response.diagnostics)
            if not search_response.success:
                logger.warning("Search stage failed, attempting login refresh: {}", search_response.message)
                if request.auto_retry_after_login:
                    login_response = await self._wait_for_login(
                        attempts=2,
                        interval=request.login_retry_interval,
                        diagnostics=diagnostics,
                        context="search_retry",
                    )
                    login_status = login_response.status
                    if login_response.success:
                        search_response = await self.search_service.run_search(search_request)
                        diagnostics.extend(search_response.diagnostics)
                if not search_response.success:
                    return AutoWorkflowResponse(
                        success=False,
                        stage="search",
                        message=search_response.message,
                        login_status=login_status,
                        search_result=search_response,
                        diagnostics=diagnostics,
                    )

            note_response = await self.note_service.collect_note_details(note_limit=request.note_limit)
            diagnostics.extend(note_response.diagnostics)
            if not note_response.success and request.auto_retry_after_login:
                logger.warning("Note collection failed, retrying after login refresh: {}", note_response.message)
                login_response = await self._wait_for_login(
                    attempts=2,
                    interval=request.login_retry_interval,
                    diagnostics=diagnostics,
                    context="collect_retry",
                )
                login_status = login_response.status
                if login_response.success:
                    search_response = await self.search_service.run_search(search_request)
                    diagnostics.extend(search_response.diagnostics)
                    if search_response.success:
                        note_response = await self.note_service.collect_note_details(
                            note_limit=request.note_limit,
                        )
                        diagnostics.extend(note_response.diagnostics)

            if not note_response.success:
                return AutoWorkflowResponse(
                    success=False,
                    stage="collect",
                    message=note_response.message,
                    login_status=login_status,
                    search_result=search_response,
                    note_result=note_response,
                    diagnostics=diagnostics,
                )

            return AutoWorkflowResponse(
                success=True,
                stage="complete",
                message="自动执行完成，已返回搜索页和笔记详情",
                login_status=login_status,
                search_result=search_response,
                note_result=note_response,
                diagnostics=diagnostics,
            )

    async def _wait_for_login(
        self,
        *,
        attempts: int,
        interval: float,
        diagnostics: list[str],
        context: str,
    ) -> LoginStatusResponse:
        """Poll ensure_login_status until logged in or retries exhausted."""
        latest_response: Optional[LoginStatusResponse] = None
        for attempt in range(1, attempts + 1):
            response = await self.login_service.ensure_login_status()
            latest_response = response
            state = response.status.state
            diagnostics.append(f"{context}_state_{attempt}={state}")
            if response.success:
                return response
            if state not in _POLLABLE_STATES:
                break
            if attempt < attempts:
                await asyncio.sleep(interval)
        assert latest_response is not None
        return latest_response

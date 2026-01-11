"""Utility to ensure Chrome is running for the shared DevTools session."""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import httpx
from loguru import logger

from src.config import (
    CHROME_AUTO_CLOSE,
    CHROME_BINARY,
    CHROME_EXTRA_ARGS,
    CHROME_HEADLESS,
    CHROME_MANAGE_PROCESS,
    CHROME_REMOTE_HOST,
    CHROME_REMOTE_PORT,
    CHROME_REMOTE_URL,
    CHROME_STARTUP_TIMEOUT,
    CHROME_USER_DATA_DIR,
)


class BrowserGuard:
    """Launches Chrome if the remote debugging endpoint is unavailable."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        auto_close: bool | None = None,
        headless: bool | None = None,
        manage_process: bool | None = None,
    ) -> None:
        self.binary = binary or CHROME_BINARY
        self.auto_close = CHROME_AUTO_CLOSE if auto_close is None else auto_close
        self.headless = CHROME_HEADLESS if headless is None else headless
        self.manage_process = CHROME_MANAGE_PROCESS if manage_process is None else manage_process
        self.extra_args = self._parse_extra_args(CHROME_EXTRA_ARGS)
        self._proc: asyncio.subprocess.Process | None = None
        self._started_here = False
        self._launch_lock = asyncio.Lock()

    @staticmethod
    def _parse_extra_args(raw: str) -> List[str]:
        if not raw:
            return []
        # Windows 路径存在空格，用 shlex 解析可自动处理引号
        return [part for part in shlex.split(raw, posix=os.name != "nt") if part]

    async def _devtools_alive(self) -> bool:
        url = f"{CHROME_REMOTE_URL}/json/version"
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(url, timeout=2)
            resp.raise_for_status()
            return True
        except Exception:
            return False

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_event_loop().time() + CHROME_STARTUP_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if await self._devtools_alive():
                return
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(f"Chrome 启动失败，进程退出码 {self._proc.returncode}，请检查依赖或配置")
            await asyncio.sleep(0.4)
        raise RuntimeError("Chrome DevTools 端口在超时时间内未启动，请检查 Chrome 配置")

    async def _launch(self) -> None:
        self._cleanup_profile()
        args = [
            self.binary,
            f"--remote-debugging-port={CHROME_REMOTE_PORT}",
            f"--remote-debugging-address={CHROME_REMOTE_HOST}",
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.headless:
            args.append("--headless=new")
            args.append("--window-size=1920,1080")  # Ensure large viewport for scrolling logic
        else:
            args.extend([
                "--start-maximized",  # Open window maximized for better visibility
                "--disable-software-rasterizer",
                "--disable-blink-features=AutomationControlled", # Hide navigator.webdriver
                "--disable-infobars", # Hide "Chrome is being controlled by automated test software"
                "--exclude-switches=enable-automation", # Hide automation switch
                "--use-mock-keychain", # Avoid keychain prompts
            ])
        args.extend(self.extra_args)

        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        logger.info("Launching managed Chrome instance: {}", " ".join(args))
        stderr_path = Path(CHROME_USER_DATA_DIR) / "chrome_stderr.log"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_path.open("wb"),
            creationflags=creationflags,
        )
        self._started_here = True

    def _cleanup_profile(self) -> None:
        """Aggressively clean up Chrome lock files to prevent startup failures."""
        import shutil
        
        locks = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        base = Path(CHROME_USER_DATA_DIR)
        
        for name in locks:
            target = base / name
            try:
                if not target.exists() and not target.is_symlink():
                    continue
                    
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
                
                logger.info(f"Removed stale lock file: {target}")
            except Exception as exc:
                # In Docker, we might not have permission if the file is locked by Host
                # But we log it and proceed, hoping Chrome can handle it or it's not critical
                logger.warning(f"Could not remove {target}: {exc}. Chrome might fail to start.")

    async def ensure(self) -> bool:
        """Ensure Chrome DevTools endpoint is reachable. Returns True if launched now."""
        async with self._launch_lock:
            if await self._devtools_alive():
                return False
            if not self.manage_process:
                raise RuntimeError(
                    "Chrome DevTools 端口不可用：未检测到远程调试浏览器。\n"
                    "请先在宿主机运行 open_chrome_gui.bat 或手动启动带有 --remote-debugging-port=9222 的 Chrome，"
                    "然后重新执行 workflow。",
                )
            await self._launch()
            await self._wait_until_ready()
            return True

    async def shutdown(self) -> None:
        if self._proc is None:
            return
        logger.info("Shutting down managed Chrome instance")
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=3)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Chrome did not exit gracefully, forcing kill.")
            try:
                self._proc.kill()
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception as e:
                logger.error(f"Failed to kill Chrome process: {e}")
        finally:
            self._proc = None
            self._started_here = False

    @asynccontextmanager
    async def lifecycle(self):
        started = await self.ensure()
        try:
            yield started
        finally:
            if started and self.auto_close:
                await self.shutdown()


__all__ = ["BrowserGuard"]

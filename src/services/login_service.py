"""Login status detection service."""
from __future__ import annotations

import asyncio
import base64
import random
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, Dict, Any, Mapping
from urllib.parse import urljoin, urlparse, parse_qs, quote
from uuid import uuid4

import httpx
from loguru import logger
from websockets import exceptions as ws_exceptions

from src.clients.chrome_devtools import ChromeDevToolsClient
from src.config import CAPTCHA_DIR, DATA_DIR, HOST_DATA_DIR, chrome_entry_url
from src.schemas.login import (
    LoginStatus,
    LoginStatusResponse,
    LoginAssistantResponse,
)
from src.utils.browser_guard import BrowserGuard
from src.utils.cookie_storage import persist_cookies, load_cookies
from src.utils.qr_storage import save_qr_image_from_base64


LOGIN_PROBE_SCRIPT = r"""
(() => {
  const keywordsLogin = ['\u767b\u5f55', '\u767b\u5165', 'login'];
  const keywordsProfile = ['\u6211', 'profile'];

  const hasKeyword = (text, keywords) => {
    if (!text) return false;
    const normalized = text.trim().toLowerCase();
    return keywords.some((kw) => normalized.includes(kw.toLowerCase()));
  };

  const findByKeyword = (selectors, keywords) => {
    for (const selector of selectors) {
      const nodes = document.querySelectorAll(selector);
      for (const node of nodes) {
        if (hasKeyword(node.textContent || '', keywords)) {
          return true;
        }
      }
    }
    return false;
  };

  const collectTexts = (selectors) => {
    const texts = [];
    for (const selector of selectors) {
      const nodes = document.querySelectorAll(selector);
      for (const node of nodes) {
        const text = (node.textContent || '').trim();
        if (text) {
          texts.push(text.slice(0, 120));
        }
      }
    }
    return texts.slice(0, 5);
  };

  const loginModal = document.querySelector('.login-container, .passport-login-container, .login-dialog, .login-box');
  
  // Find QR code image - look for larger images or images in QR-specific containers
  let qrImage = null;
  if (loginModal) {
    // Try specific QR code selectors first
    const qrSelectors = [
      'img[class*="qr"]',
      'img[class*="QR"]', 
      'img[class*="code"]',
      '.qrcode img',
      '.qr-code img',
      '[class*="qrcode"] img',
      '[class*="QRCode"] img',
      'canvas',  // Sometimes QR is rendered as canvas
    ];
    for (const sel of qrSelectors) {
      const el = loginModal.querySelector(sel);
      if (el && (el.tagName === 'CANVAS' || (el.naturalWidth > 100 && el.naturalHeight > 100))) {
        if (el.tagName === 'CANVAS') {
          qrImage = { src: el.toDataURL('image/png') };
        } else {
          qrImage = el;
        }
        break;
      }
    }
    // Fallback: find larger images (QR codes are typically 150x150+)
    if (!qrImage) {
      const allImages = loginModal.querySelectorAll('img[src]');
      for (const img of allImages) {
        if (img.naturalWidth > 120 && img.naturalHeight > 120) {
          qrImage = img;
          break;
        }
      }
    }
  }
  const captchaImg = document.querySelector('img[src*="captcha"], img[src*="verify"]');
  const feedNodes = document.querySelectorAll('.note-item, [class*="note-card"], [class*="noteItem"], .waterfall-item, [class*="feeds-card"]');

  return {
    url: window.location.href,
    title: document.title,
    feedCount: feedNodes.length,
    hasLoginButton: findByKeyword(['button', 'a', '[role="button"]'], keywordsLogin),
    hasProfileButton: findByKeyword(['button', 'a', '[role="button"]'], keywordsProfile),
    hasLoginModal: Boolean(loginModal),
    modalTexts: loginModal ? collectTexts(['.login-container', '.passport-login-container', '.login-dialog']) : [],
    qrImage: qrImage ? qrImage.src : (captchaImg ? captchaImg.src : null),
    qrLoaded: qrImage ? (qrImage.naturalWidth > 100 && qrImage.naturalHeight > 100) : false,
    captchaPage: window.location.href.toLowerCase().includes('website-login/captcha'),
    pageTexts: collectTexts(['.dialog', '.modal', '.passport-login-container']),
  };
})();
"""


SLIDER_PROBE_SCRIPT = r"""
(() => {
  const normalizeRect = (rect) => {
    if (!rect) return null;
    return {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    };
  };

  const selectors = [
    '.captcha-drag-slider',
    '.captcha-drag-button',
    '.drag-button',
    '.drag-btn',
    '.slider-button',
    '.slider-btn',
    '.nc_iconfont.btn_slide',
    '.geetest_slider_button',
    '.secsdk-captcha-drag-icon',
    '.Verification-sliderButton',
  ];

  let button = null;
  for (const selector of selectors) {
    const node = document.querySelector(selector);
    if (node) {
      button = node;
      break;
    }
  }

  let track = null;
  if (button) {
    track =
      button.closest('.captcha-drag-area, .drag-area, .slider-container, .slider-bar, .drag-bar') ||
      button.parentElement;
  }
  if (!track) {
    const candidates = Array.from(
      document.querySelectorAll('[class*="slider"], [class*="drag"], [class*="verify"], button, div'),
    );
    for (const el of candidates) {
      const text = (el.textContent || '').trim().toLowerCase();
      if (!text) continue;
      if (text.includes('drag right') || text.includes('向右拖动') || text.includes('拖动完成')) {
        track = el;
        if (!button) {
          button = el.querySelector('button, .btn, [class*="button"], [class*="handle"]') || el.firstElementChild;
        }
        break;
      }
    }
  }

  if (!track) {
    return { hasSlider: false };
  }

  const trackRect = track.getBoundingClientRect();
  if (!trackRect || trackRect.width < 40 || trackRect.height < 10) {
    return { hasSlider: false };
  }

  const buttonRect = button ? button.getBoundingClientRect() : null;
  return {
    hasSlider: true,
    trackRect: normalizeRect(trackRect),
    buttonRect: normalizeRect(buttonRect),
  };
})();
"""


STEALTH_SCRIPT = r"""
(() => {
  const newProto = navigator.__proto__;
  delete newProto.webdriver;
  navigator.__proto__ = newProto;

  Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
  });

  Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en'],
  });

  Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
  });

  const originalQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
      Promise.resolve({ state: Notification.permission }) :
      originalQuery(parameters)
  );
})();
"""


class LoginService:
    """Encapsulates Chrome-driven login state detection."""

    def __init__(self, browser_guard: Optional[BrowserGuard] = None) -> None:
        self.entry_url = chrome_entry_url()
        self.browser_guard = browser_guard or BrowserGuard()

    async def ensure_login_status(self) -> LoginStatusResponse:
        await self.browser_guard.ensure()
        client = ChromeDevToolsClient(initial_url=self.entry_url)
        diagnostics: list[str] = [f"entry_url={self.entry_url}"]

        try:
            await self._restore_session_cookies(client, diagnostics)
            
            # Inject stealth script before navigation
            await client.add_script_to_evaluate_on_new_document(STEALTH_SCRIPT)
            
            logger.info("Navigating Chrome target to {}", self.entry_url)
            await client.navigate(self.entry_url)

            logger.info("Waiting for ready state...")
            ready = await client.wait_for_ready()
            diagnostics.append(f"page_ready={ready}")

            # Wait for feed content to render (XHS uses dynamic JS rendering)
            logger.info("Waiting for feed content to render...")
            feed_wait_script = """
            (() => {
                const feedNodes = document.querySelectorAll('.note-item, [class*="note-card"], [class*="noteItem"], .waterfall-item, [class*="feeds-card"]');
                return feedNodes.length;
            })()
            """
            feed_count = 0
            for attempt in range(10):  # Wait up to 5 seconds (10 * 0.5s)
                feed_count = await client.evaluate(feed_wait_script) or 0
                if feed_count > 0:
                    diagnostics.append(f"feed_appeared_at_attempt={attempt}")
                    break
                await asyncio.sleep(0.5)
            else:
                diagnostics.append("feed_wait_timeout")
            
            logger.info("Executing DOM probe... (feedCount={})", feed_count)
            probe = await client.evaluate(LOGIN_PROBE_SCRIPT)
            diagnostics.append(f"url={probe.get('url')}")
            if self._is_error_page(probe):
                recovered = await self._recover_from_error_page(client, probe, diagnostics)
                if recovered:
                    probe = await client.evaluate(LOGIN_PROBE_SCRIPT)
                    diagnostics.append(f"url_after_recover={probe.get('url')}")
            status = self._map_probe_to_status(probe, diagnostics)
            # Try slider solve for both captcha_gate and needs_qr_scan states
            # (Security Verification modal with slider may be detected as login modal)
            if status.state in {"captcha_gate", "needs_qr_scan"}:
                slider_cleared = await self._attempt_slider_solve(client, diagnostics)
                if slider_cleared:
                    await asyncio.sleep(2)  # Wait for page transition after slider
                    probe = await client.evaluate(LOGIN_PROBE_SCRIPT)
                    diagnostics.append(f"url_after_slider={probe.get('url')}")
                    status = self._map_probe_to_status(probe, diagnostics)
            if status.state in {"captcha_gate", "needs_qr_scan"}:
                # Wait for QR code to load (up to 8 seconds)
                for attempt in range(16):
                    if probe.get("qrLoaded"):
                        diagnostics.append(f"qr_loaded_at_attempt={attempt}")
                        break
                    await asyncio.sleep(0.5)
                    probe = await client.evaluate(LOGIN_PROBE_SCRIPT)
                else:
                    diagnostics.append("qr_load_timeout")
                # Re-map status with updated probe (may have new qrImage)
                status = self._map_probe_to_status(probe, diagnostics)
                cropped, full_page = await self._capture_verification_screenshots(client, diagnostics)
                updates: Dict[str, Optional[str]] = {}
                if cropped:
                    updates["captcha_screenshot_file"] = cropped
                if full_page:
                    updates["full_page_screenshot_file"] = full_page
                if updates:
                    status = status.model_copy(update=updates)
            if status.state == "logged_in":
                await self._persist_session_cookies(client, diagnostics)
            return LoginStatusResponse(success=status.state == "logged_in", status=status)
        except httpx.HTTPError as exc:
            diagnostics.append(f"http_error={exc}")
            logger.error("Chrome DevTools HTTP error: {}", exc)
            status = LoginStatus(
                state="browser_offline",
                message="Unable to reach Chrome DevTools. Please launch Chrome with --remote-debugging-port.",
                next_actions=[
                    "确认本地 Chrome 已使用 --remote-debugging-port=9222 启动并保持打开",
                    "如果端口已占用，请重新启动 Chrome 再调用 ensure_login_status",
                ],
                diagnostics=diagnostics,
            )
            return LoginStatusResponse(success=False, status=status)
        except (ws_exceptions.WebSocketException, OSError, RuntimeError) as exc:
            diagnostics.append(f"ws_error={exc}")
            logger.error("Chrome DevTools websocket error: {}", exc)
            status = LoginStatus(
                state="browser_offline",
                message="Chrome websocket connection failed. Check the port and network.",
                next_actions=[
                    "检查 9222 端口是否可访问，必要时重新启动 Chrome",
                    "关闭占用端口的旧进程，然后再次调用 ensure_login_status",
                ],
                diagnostics=diagnostics,
            )
            return LoginStatusResponse(success=False, status=status)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"unexpected_error={exc}")
            logger.exception("Unexpected error while probing login state")
            status = LoginStatus(
                state="unknown",
                message="Login probe failed due to an unexpected error. Please review the MCP logs.",
                next_actions=[
                    "�ظ��ص� ensure_login_status ���鿴�Ƿ���������ԣ����ҳ����Ƿ����",
                    "����Դ�ļ����߱����Ա�������־（logs/ Ŀ¼）�п�����ϸ����",
                ],
                diagnostics=diagnostics,
            )
            return LoginStatusResponse(success=False, status=status)
        finally:
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                pass

    async def guide_login_step(self, user_command: str = "") -> LoginAssistantResponse:
        """
        Conversational helper to guide users through captcha/login QR flows.

        Args:
            user_command: Arbitrary user text, logged for diagnostics.
        """
        diagnostics = [f"user_command={user_command!r}"]
        response = await self.ensure_login_status()
        status = response.status
        diagnostics.extend(status.diagnostics)

        captcha_url = self._to_file_url(status.captcha_screenshot_file)
        qr_url = self._to_file_url(status.qr_code_file)
        full_page_url = self._to_file_url(status.full_page_screenshot_file)

        if status.state == "logged_in":
            message = "检测到你已经登录，无需再次扫码。"
            next_hint = "可以返回主流程继续执行采集任务。"
            success = True
        elif status.state == "captcha_gate":
            link = captcha_url or status.captcha_screenshot_file
            full_link = full_page_url or status.full_page_screenshot_file
            message = "检测到人机验证，需要先扫码通过安全校验。"
            if link:
                message += f" 验证码链接：{link}"
            if full_link:
                message += f" 整页截图：{full_link}"
            next_hint = "打开下方验证码链接扫码，完成后等待 5 秒再次让我检查状态。"
            success = False
        elif status.state == "needs_qr_scan":
            link = qr_url or status.qr_code_file
            full_link = full_page_url or status.full_page_screenshot_file
            message = "浏览器已展示登录二维码，请使用小红书 App 扫码登录。"
            if link:
                message += f" 二维码链接：{link}"
            if full_link:
                message += f" 整页截图：{full_link}"
            next_hint = "扫码完成后对我说“已扫码”或重新调用 login_assistant，我会继续确认登录状态。"
            success = False
        elif status.state == "browser_offline":
            message = "Chrome DevTools 不可用，请确认浏览器已启动并开启 --remote-debugging-port=9222。"
            next_hint = "处理完浏览器问题后，再次调用 login_assistant。"
            success = False
        else:
            message = status.message or "当前登录状态未知，需要人工手动确认。"
            next_hint = "请检查浏览器界面后重试。"
            success = False

        diagnostics.append(f"assistant_state={status.state}")

        return LoginAssistantResponse(
            success=success,
            state=status.state,
            message=message,
            next_hint=next_hint,
            captcha_file=status.captcha_screenshot_file,
            captcha_file_url=captcha_url,
            qr_code_file=status.qr_code_file,
            qr_code_file_url=qr_url,
            qr_base64=status.qr_base64,
            full_page_file=status.full_page_screenshot_file,
            full_page_file_url=full_page_url,
            diagnostics=diagnostics,
        )

    def _is_error_page(self, probe: dict) -> bool:
        url = (probe.get("url") or "").lower()
        if "website-login/error" in url:
            return True
        texts = " ".join(probe.get("pageTexts") or [])
        return any(keyword in texts for keyword in ("网络连接异常", "安全限制", "返回首页"))

    async def _recover_from_error_page(
        self,
        client: ChromeDevToolsClient,
        probe: dict,
        diagnostics: list[str],
    ) -> bool:
        try:
            raw_url = probe.get("url") or ""
            parsed = urlparse(raw_url)
            query = parse_qs(parsed.query or "")
            redirect_values = query.get("redirectPath")
            if redirect_values:
                redirect_path = redirect_values[0]
            else:
                redirect_path = quote(self.entry_url, safe="")
            if "://" in redirect_path and "%2F" not in redirect_path:
                redirect_path = quote(redirect_path, safe="")
            captcha_url = (
                f"https://www.xiaohongshu.com/website-login/captcha?redirectPath={redirect_path}"
            )
            diagnostics.append(f"error_recover_to={captcha_url}")
            await client.navigate(captcha_url)
            await asyncio.sleep(0.8)
            await client.wait_for_ready()
            return True
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"error_recover_failed={exc}")
            return False


    def _map_probe_to_status(self, probe: dict, diagnostics: list[str]) -> LoginStatus:
        url = probe.get("url", "")
        diagnostics.append(f"feedCount={probe.get('feedCount')}")
        diagnostics.append(f"hasLoginModal={probe.get('hasLoginModal')}")
        diagnostics.append(f"hasLoginButton={probe.get('hasLoginButton')}")
        diagnostics.append(f"captchaPage={probe.get('captchaPage')}")

        qr_src = probe.get("qrImage")
        qr_url, qr_base64 = self._prepare_qr_payload(url, qr_src) if qr_src else (None, None)
        qr_file = save_qr_image_from_base64(qr_base64) if qr_base64 else None

        if probe.get("captchaPage"):
            return LoginStatus(
                state="captcha_gate",
                message="A captcha or QR verification is required. Please complete it in the visible browser.",
                qr_image_url=qr_url,
                qr_base64=qr_base64,
                qr_code_file=qr_file,
                next_actions=[
                    "在可视化 Chrome 窗口中刷新白底验证码页面并使用小红书 App 扫码",
                    "扫码成功后等待页面跳转，再次调用 ensure_login_status 确认登录",
                ],
                diagnostics=diagnostics,
            )

        feed_count = probe.get("feedCount", 0) or 0
        has_login_modal = probe.get("hasLoginModal", False)
        has_login_button = probe.get("hasLoginButton", False)

        if feed_count > 0 and not has_login_modal and not has_login_button:
            return LoginStatus(
                state="logged_in",
                message="Feed content detected. Session appears to be logged in.",
                next_actions=["当前会话有效，可以调用搜索和采集相关工具"],
                diagnostics=diagnostics,
            )

        if has_login_modal or has_login_button:
            return LoginStatus(
                state="needs_qr_scan",
                message="Login modal detected. Please scan the QR code to continue.",
                qr_image_url=qr_url,
                qr_base64=qr_base64,
                qr_code_file=qr_file,
                next_actions=[
                    "在可视化 Chrome 中查看登录弹窗二维码，并用小红书 App 扫码",
                    "扫码结束后再次调用 ensure_login_status，直到状态显示 logged_in",
                ],
                diagnostics=diagnostics,
            )

        return LoginStatus(
            state="unknown",
            message="Unable to determine login state. Please verify manually in Chrome.",
            qr_image_url=qr_url,
            qr_base64=qr_base64,
            qr_code_file=qr_file,
            next_actions=[
                "查看浏览器页面是否卡在其他提示（例如验证码或弹窗），必要时刷新页面",
                "若问题持续，请尝试重新登录或重启 Chrome 后再调用 ensure_login_status",
            ],
            diagnostics=diagnostics,
        )

    @staticmethod
    def _to_file_url(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            fs_path = Path(path)
            if not fs_path.is_absolute():
                fs_path = fs_path.resolve()
            if HOST_DATA_DIR:
                try:
                    rel = fs_path.relative_to(DATA_DIR)
                    host_prefix = str(HOST_DATA_DIR).rstrip("/\\")
                    base = (
                        PureWindowsPath(host_prefix)
                        if ":" in host_prefix[:3]
                        else PurePosixPath(host_prefix)
                    )
                    rel_parts = PurePosixPath(rel.as_posix()).parts
                    host_path = base.joinpath(*rel_parts)
                    return "file:///" + str(host_path).replace("\\", "/")
                except Exception:
                    pass
            return fs_path.as_uri()
        except Exception:
            return None

    def _prepare_qr_payload(
        self,
        current_url: str,
        qr_src: str,
    ) -> tuple[Optional[str], Optional[str]]:
        if qr_src.startswith("data:image"):
            try:
                return None, qr_src.split(",", 1)[1]
            except IndexError:
                return None, None
        if qr_src.startswith("//"):
            return f"https:{qr_src}", None
        if qr_src.startswith("/"):
            return urljoin(current_url, qr_src), None
        return qr_src, None

    async def _attempt_slider_solve(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
    ) -> bool:
        """Try to automatically drag the slider captcha."""
        try:
            slider = await client.evaluate(SLIDER_PROBE_SCRIPT)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"slider_probe_error={exc}")
            return False

        if not slider or not slider.get("hasSlider"):
            diagnostics.append("slider_detected=False")
            return False

        track = slider.get("trackRect") or {}
        button = slider.get("buttonRect") or {}
        start_x = button.get("x")
        start_y = button.get("y")
        if start_x is None or start_y is None:
            start_x = track.get("x", 0) + min(24, (track.get("width", 80) or 80) * 0.15)
            start_y = track.get("y", 0) + (track.get("height", 40) or 40) / 2
        else:
            start_x += (button.get("width") or 20) / 2
            start_y += (button.get("height") or 20) / 2

        end_x = track.get("x", 0) + (track.get("width") or 150) - max(12, (track.get("height") or 30) / 2)
        end_y = track.get("y", 0) + (track.get("height") or 30) / 2

        if end_x <= start_x:
            diagnostics.append("slider_geometry_invalid")
            return False

        diagnostics.append(
            f"slider_drag=attempt start=({start_x:.1f},{start_y:.1f}) end=({end_x:.1f},{end_y:.1f})",
        )
        try:
            await client.drag_mouse((start_x, start_y), (end_x, end_y), duration=random.uniform(0.9, 1.4))
            await asyncio.sleep(1.5)
            diagnostics.append("slider_drag=sent")
            return True
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"slider_drag_error={exc}")
            logger.warning("Failed to perform slider drag: {}", exc)
            return False

    async def _capture_verification_screenshots(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
    ) -> tuple[Optional[str], Optional[str]]:
        clip = await self._resolve_verification_clip(client, diagnostics)
        cropped = await self._capture_and_store_screenshot(
            client,
            diagnostics,
            clip=clip,
            prefix="captcha",
            delay=0.8,
        )
        # Capture a guaranteed full-page snapshot for manual review/scanning.
        full_page = await self._capture_and_store_screenshot(
            client,
            diagnostics,
            clip=None,
            prefix="full_page",
            delay=0.2,
        )
        return cropped, full_page

    async def _capture_and_store_screenshot(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
        *,
        clip: Optional[Dict[str, float]],
        prefix: str,
        delay: float = 0.5,
    ) -> Optional[str]:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            raw = await client.capture_screenshot(full_page=clip is None, clip=clip)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"{prefix}_screenshot_error={exc}")
            logger.warning("Failed to capture {} screenshot: {}", prefix, exc)
            return None

        if not raw:
            diagnostics.append(f"{prefix}_screenshot=empty")
            return None

        try:
            data = base64.b64decode(raw)
            filename = f"{prefix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}.png"
            path = CAPTCHA_DIR / filename
            path.write_bytes(data)
            diagnostics.append(f"{prefix}_screenshot_file={path}")
            return str(path)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"{prefix}_screenshot_write_error={exc}")
            logger.warning("Failed to write {} screenshot: {}", prefix, exc)
            return None

    async def _resolve_verification_clip(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
    ) -> Optional[Dict[str, float]]:
        script = r"""
(() => {
  const selectors = [
    'img[src*="captcha"]',
    'img[src*="verify"]',
    '.captcha-img img',
    '.captcha-img',
    '.login-container img[src]',
    '.passport-login-container img[src]',
    '.QRCode-img img',
  ];
  const pad = 20;
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width < 5 || rect.height < 5) continue;
    const ready = el.complete && el.naturalWidth > 50 && el.naturalHeight > 50;
    return {
      ready,
      src: el.src || null,
      clip: {
        x: Math.max(rect.x - pad, 0),
        y: Math.max(rect.y - pad, 0),
        width: rect.width + pad * 2,
        height: rect.height + pad * 2,
        scale: 1,
      },
    };
  }
  return { pending: true };
})();
"""
        for attempt in range(12):
            try:
                clip_info = await client.evaluate(script)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"captcha_clip_error={exc}")
                clip_info = None
            if clip_info and clip_info.get("ready") and clip_info.get("clip"):
                clip = clip_info["clip"]
                diagnostics.append(
                    f"captcha_clip_ready={clip_info.get('src')} size={clip.get('width')}x{clip.get('height')}",
                )
                return clip  # type: ignore[return-value]
            await asyncio.sleep(0.6)
        diagnostics.append("captcha_clip=fallback_full_page")
        return None

    async def _persist_session_cookies(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
    ) -> None:
        try:
            cookies = await client.get_cookies()
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"cookie_fetch_error={exc}")
            logger.warning("Failed to fetch cookies: {}", exc)
            return

        if not cookies:
            diagnostics.append("cookie_count=0")
            logger.warning("Chrome returned no cookies for the active profile")
            return

        diagnostics.append(f"cookie_count={len(cookies)}")
        saved_path = persist_cookies(cookies)
        if saved_path:
            diagnostics.append(f"cookies_file={saved_path}")

    async def _restore_session_cookies(
        self,
        client: ChromeDevToolsClient,
        diagnostics: list[str],
    ) -> bool:
        payload = load_cookies()
        if not payload:
            diagnostics.append("cookies_restore=missing")
            return False
        cookies_raw = payload.get("cookies") or []
        sanitized = []
        for cookie in cookies_raw:
            param = self._build_cookie_param(cookie)
            if param:
                sanitized.append(param)
        if not sanitized:
            diagnostics.append("cookies_restore=empty")
            return False
        try:
            await client.send("Network.setCookies", {"cookies": sanitized})
            diagnostics.append(f"cookies_restored={len(sanitized)}")
            return True
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"cookies_restore_error={exc}")
            logger.warning("Failed to restore cookies: {}", exc)
            return False

    @staticmethod
    def _build_cookie_param(cookie: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            return None
        domain = cookie.get("domain")
        param: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "path": cookie.get("path") or "/",
        }
        if domain:
            param["domain"] = domain
        else:
            url = cookie.get("url")
            if not url:
                return None
            param["url"] = url

        for key in ("expires", "httpOnly", "secure", "sameSite", "priority"):
            if key in cookie:
                param[key] = cookie[key]
        return param

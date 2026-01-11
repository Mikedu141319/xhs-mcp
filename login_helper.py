#!/usr/bin/env python3
"""
小红书登录助手 - Xiaohongshu Login Helper

双击运行此工具，在浏览器中扫码登录小红书。
登录成功后，cookies 会自动保存，MCP 服务就能正常使用了。

Double-click to run this tool, scan QR code in the browser to login.
After successful login, cookies will be saved automatically for MCP to use.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# 确定数据目录
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
COOKIES_FILE = DATA_DIR / "cookies.json"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)

try:
    import httpx
except ImportError:
    print("正在安装依赖 httpx...")
    os.system(f"{sys.executable} -m pip install httpx")
    import httpx


def find_chrome() -> str:
    """Find Chrome executable on the system."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "chrome"  # Fallback to PATH


def launch_chrome(debug_port: int = 9333) -> None:
    """Launch Chrome with remote debugging enabled."""
    chrome_path = find_chrome()
    user_data_dir = DATA_DIR / "login-chrome-profile"
    user_data_dir.mkdir(exist_ok=True)
    
    cmd = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-popup-blocking",
        "https://www.xiaohongshu.com/explore",
    ]
    
    import subprocess
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"✅ Chrome 已启动 (调试端口: {debug_port})")


async def wait_for_chrome(debug_port: int, timeout: int = 30) -> bool:
    """Wait for Chrome DevTools to be available."""
    url = f"http://127.0.0.1:{debug_port}/json"
    async with httpx.AsyncClient() as client:
        for _ in range(timeout * 2):
            try:
                resp = await client.get(url, timeout=2)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


async def get_ws_url(debug_port: int) -> str | None:
    """Get WebSocket URL for the main page target."""
    url = f"http://127.0.0.1:{debug_port}/json"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=5)
            targets = resp.json()
            for target in targets:
                if target.get("type") == "page" and "xiaohongshu" in target.get("url", ""):
                    return target.get("webSocketDebuggerUrl")
        except Exception:
            pass
    return None


async def check_login_status(debug_port: int) -> tuple[bool, list]:
    """Check if user is logged in and return cookies if so."""
    import websockets
    
    ws_url = await get_ws_url(debug_port)
    if not ws_url:
        return False, []
    
    try:
        async with websockets.connect(ws_url) as ws:
            # Check for feed items (indicates logged in state)
            # Also check for verification/captcha popups
            check_script = """
            (() => {
                const feeds = document.querySelectorAll('.note-item, [class*="note-card"], .waterfall-item');
                const loginModal = document.querySelector('.login-container, .passport-login-container');
                const loginButton = document.querySelector('[class*="login-btn"], .login-btn');
                
                // Check for verification/captcha popups (二次验证)
                const verifyModal = document.querySelector(
                    '.captcha-container, .verify-container, ' +
                    '[class*="captcha"], [class*="verify"], ' +
                    '.dialog, .modal'
                );
                
                // Check page text for verification keywords
                const bodyText = document.body.innerText || '';
                const hasVerifyText = bodyText.includes('请通过验证') || 
                                     bodyText.includes('扫码验证') ||
                                     bodyText.includes('安全验证') ||
                                     bodyText.includes('二维码已过期');
                
                // Check if there's a QR code overlay (verification QR)
                const qrOverlay = document.querySelector('.qr-code, [class*="qrcode"], canvas');
                const hasQrInModal = verifyModal && qrOverlay;
                
                return {
                    feedCount: feeds.length,
                    hasLoginModal: Boolean(loginModal),
                    hasLoginButton: Boolean(loginButton),
                    hasVerifyModal: Boolean(verifyModal && (hasVerifyText || hasQrInModal)),
                    hasVerifyText: hasVerifyText,
                };
            })();
            """
            
            await ws.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": check_script, "returnByValue": True}
            }))
            
            result = json.loads(await ws.recv())
            value = result.get("result", {}).get("result", {}).get("value", {})
            
            feed_count = value.get("feedCount", 0)
            has_login_modal = value.get("hasLoginModal", False)
            has_login_button = value.get("hasLoginButton", False)
            has_verify_modal = value.get("hasVerifyModal", False)
            has_verify_text = value.get("hasVerifyText", False)
            
            # Must have feed content AND no login/verify modals
            is_logged_in = (feed_count > 0 and 
                           not has_login_modal and 
                           not has_login_button and
                           not has_verify_modal and
                           not has_verify_text)
            
            # Debug info
            if has_verify_text:
                print("⚠️ 检测到验证弹窗，请完成验证后再等待...")
            
            # Get cookies
            cookies = []
            if is_logged_in:
                await ws.send(json.dumps({
                    "id": 2,
                    "method": "Network.getCookies"
                }))
                cookie_result = json.loads(await ws.recv())
                cookies = cookie_result.get("result", {}).get("cookies", [])
            
            return is_logged_in, cookies
            
    except Exception as e:
        print(f"⚠️ 检测状态时出错: {e}")
        return False, []


def save_cookies(cookies: list) -> None:
    """Save cookies to file."""
    # Filter xiaohongshu cookies
    xhs_cookies = [c for c in cookies if "xiaohongshu" in c.get("domain", "")]
    
    output = {
        "cookies": xhs_cookies,
        "exported_from": "login_helper",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"✅ 已保存 {len(xhs_cookies)} 个 cookies 到 {COOKIES_FILE}")


async def main():
    print()
    print("=" * 50)
    print("   🍠 小红书登录助手 - Xiaohongshu Login Helper")
    print("=" * 50)
    print()
    
    DEBUG_PORT = 9333  # 使用不同端口避免与 Docker 冲突
    
    # Launch Chrome
    print("📱 正在启动 Chrome 浏览器...")
    launch_chrome(DEBUG_PORT)
    
    # Wait for Chrome to be ready
    print("⏳ 等待浏览器启动...")
    if not await wait_for_chrome(DEBUG_PORT):
        print("❌ Chrome 启动失败，请检查是否安装了 Chrome")
        input("\n按回车键退出...")
        return
    
    print()
    print("🔔 请在浏览器中完成以下操作：")
    print("   1. 点击页面上的「登录」按钮")
    print("   2. 使用小红书 App 扫描二维码")
    print("   3. 在手机上确认登录")
    print()
    print("⏳ 等待登录完成...")
    print("   (登录成功后会自动保存 cookies)")
    print()
    
    # Poll for login status
    try:
        import websockets
    except ImportError:
        print("正在安装依赖 websockets...")
        os.system(f"{sys.executable} -m pip install websockets")
        import websockets
    
    max_wait = 300  # 5 minutes
    check_interval = 3
    elapsed = 0
    
    while elapsed < max_wait:
        is_logged_in, cookies = await check_login_status(DEBUG_PORT)
        
        if is_logged_in and cookies:
            print()
            print("🎉 登录成功！")
            save_cookies(cookies)
            print()
            print("✅ 现在可以关闭浏览器了。")
            print("✅ MCP 服务现在可以正常使用你的账号了！")
            print()
            input("按回车键退出...")
            return
        
        await asyncio.sleep(check_interval)
        elapsed += check_interval
        
        # Show progress every 15 seconds
        if elapsed % 15 == 0:
            print(f"   ... 仍在等待登录 ({elapsed}s)")
    
    print("❌ 等待超时，请重新运行此工具")
    input("\n按回车键退出...")


if __name__ == "__main__":
    asyncio.run(main())

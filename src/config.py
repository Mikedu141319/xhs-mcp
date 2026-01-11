"""Configuration management"""
import os
from pathlib import Path
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
QR_DIR = DATA_DIR / "qr"
CAPTCHA_DIR = DATA_DIR / "captchas"
COOKIES_FILE = DATA_DIR / "cookies.json"

CHROME_REMOTE_HOST = os.getenv("CHROME_REMOTE_HOST", "127.0.0.1")
CHROME_REMOTE_PORT = int(os.getenv("CHROME_REMOTE_PORT", "9222"))
CHROME_REMOTE_URL = os.getenv(
    "CHROME_REMOTE_URL",
    f"http://{CHROME_REMOTE_HOST}:{CHROME_REMOTE_PORT}",
)
import platform

system = platform.system()
if system == "Windows":
    DEFAULT_CHROME_BINARY = "C:/Program Files/Google/Chrome/Application/chrome.exe"
else:
    DEFAULT_CHROME_BINARY = "google-chrome"

CHROME_BINARY = os.getenv("CHROME_BINARY")
if not CHROME_BINARY:
    CHROME_BINARY = DEFAULT_CHROME_BINARY
elif system == "Linux" and "Program Files" in CHROME_BINARY:
    # Fallback if user accidentally left Windows path in .env while running in Docker
    CHROME_BINARY = "google-chrome"
CHROME_USER_DATA_DIR = Path(os.getenv("CHROME_USER_DATA_DIR", BASE_DIR / "chrome-profile")).resolve()
CHROME_EXTRA_ARGS = os.getenv(
    "CHROME_EXTRA_ARGS",
    "--remote-allow-origins=* --disable-dev-shm-usage --no-sandbox --disable-gpu --disable-software-rasterizer",
)
CHROME_HEADLESS = os.getenv("CHROME_HEADLESS", "true").lower() in {"1", "true", "yes"}
CHROME_AUTO_CLOSE = os.getenv("CHROME_AUTO_CLOSE", "true").lower() in {"1", "true", "yes"}
CHROME_MANAGE_PROCESS = os.getenv("CHROME_MANAGE_PROCESS", "true").lower() in {"1", "true", "yes"}
CHROME_STARTUP_TIMEOUT = float(os.getenv("CHROME_STARTUP_TIMEOUT", "40"))

MCP_SERVER_NAME = os.getenv("MCP_SERVER_NAME", "XHS Chrome MCP")

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
QR_DIR.mkdir(parents=True, exist_ok=True)
CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
CHROME_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

HOST_DATA_DIR_ENV = os.getenv("HOST_DATA_DIR")
HOST_DATA_DIR = Path(HOST_DATA_DIR_ENV) if HOST_DATA_DIR_ENV else None


@lru_cache(maxsize=1)
def chrome_entry_url() -> str:
    return os.getenv("XHS_ENTRY_URL", "https://www.xiaohongshu.com/explore")

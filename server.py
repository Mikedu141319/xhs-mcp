"""FastMCP entry for the chrome-devtools based XiaoHongShu tools."""
import os
from typing import List
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext

from src.config import MCP_SERVER_NAME, LOG_DIR
from src.schemas.automation import AutoWorkflowRequest
from src.schemas.search import SearchRequest
from src.schemas.author import AuthorNotesRequest
from src.schemas.note_link import NoteLinkRequest
from src.services.login_service import LoginService
from src.services.note_service import NoteDetailService
from src.services.search_service import SearchService
from src.services.automation_service import AutomationService
from src.services.author_service import AuthorService
from src.services.note_link_service import NoteLinkService
from src.utils.browser_guard import BrowserGuard
from src.utils.logger import configure_logging, logger


# =============================================================================
# Parameter Filter Middleware - 过滤 n8n Agent 传递的额外参数
# =============================================================================

# 每个工具允许的参数白名单
TOOL_ALLOWED_PARAMS = {
    "ensure_login_status": set(),  # 无参数
    "prepare_search": {
        "keyword", "sort_by", "note_type", "publish_time", 
        "search_scope", "location", "note_limit"
    },
    "collect_note_details": {"note_limit"},
    "auto_execute": {
        "keyword", "sort_by", "note_type", "publish_time",
        "search_scope", "location", "note_limit",
        "login_retry_limit", "login_retry_interval", "auto_retry_after_login"
    },
    "collect_author_notes": {
        "author_url", "skip_note_ids", "note_limit", "comment_limit"
    },
    "collect_note_by_link": {
        "note_url", "comment_limit"
    },
}


class ParameterFilterMiddleware(Middleware):
    """
    中间件：过滤 n8n Agent 传递的额外参数
    
    新版 n8n Agent 会传递额外的元数据参数（如 sessionId, toolCallId, chatInput, action）
    以及 prompt 中的所有字段名（如"核心关键词"、"目标笔记数量"等）。
    这个中间件在 FastMCP 验证之前过滤掉这些不需要的参数。
    """
    
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name
        
        # 获取该工具允许的参数
        allowed_params = TOOL_ALLOWED_PARAMS.get(tool_name)
        
        # 确保 arguments 不为 None
        if context.message.arguments is None:
            context.message.arguments = {}
        
        if allowed_params is not None:
            original_args = dict(context.message.arguments) if context.message.arguments else {}
            
            # 只保留白名单中的参数，同时过滤掉值为 None 的参数
            filtered_args = {
                key: value 
                for key, value in original_args.items() 
                if key in allowed_params and value is not None
            }
            
            # 记录被过滤的参数（用于调试）
            removed_keys = set(original_args.keys()) - set(filtered_args.keys())
            if removed_keys:
                logger.debug(
                    "ParameterFilterMiddleware: Tool '{}' - removed params: {}", 
                    tool_name, removed_keys
                )
            
            # 更新参数
            context.message.arguments = filtered_args
        
        # 继续执行
        return await call_next(context)


# =============================================================================
# Service Initialization
# =============================================================================

mcp = FastMCP(MCP_SERVER_NAME)

# 添加参数过滤中间件
mcp.add_middleware(ParameterFilterMiddleware())

configure_logging()
logger.info("Logging initialized at {}", LOG_DIR.resolve())
logger.info("ParameterFilterMiddleware enabled for n8n Agent compatibility")

browser_guard = BrowserGuard()
login_service = LoginService(browser_guard=browser_guard)
search_service = SearchService()
note_service = NoteDetailService()
author_service = AuthorService(browser_guard=browser_guard)
note_link_service = NoteLinkService(browser_guard=browser_guard)
automation_service = AutomationService(
    login_service=login_service,
    search_service=search_service,
    note_service=note_service,
    browser_guard=browser_guard,
)


# =============================================================================
# Filter Mappings
# =============================================================================

CHINESE_FILTERS = {
    "sort_by": {
        "综合": "comprehensive",
        "默认": "comprehensive",
        "最新": "latest",
        "最多点赞": "most_liked",
        "最多收藏": "most_collected",
        "最多评论": "most_commented",
    },
    "note_type": {
        "不限": "all",
        "全部": "all",
        "图文": "image",
        "视频": "video",
    },
    "publish_time": {
        "不限": "any",
        "一天内": "day",
        "24小时": "day",
        "一周内": "week",
        "七天内": "week",
        "半年内": "half_year",
        "六个月内": "half_year",
    },
    "search_scope": {
        "不限": "all",
        "全部": "all",
        "已看过": "seen",
        "看过": "seen",
        "未看过": "unseen",
        "未看": "unseen",
        "已关注": "following",
        "关注": "following",
    },
    "location": {
        "不限": "all",
        "全部": "all",
        "同城": "same_city",
        "附近": "nearby",
    },
}

ALIAS_FILTERS = {
    "publish_time": {
        "within_half_a_year": "half_year",
        "within_half_year": "half_year",
        "half_year_within": "half_year",
        "halfyear": "half_year",
        "six_months": "half_year",
        "half_a_year": "half_year",
        "last_half_year": "half_year",
        "within_1_week": "week",
        "within_week": "week",
        "one_week": "week",
        "within_day": "day",
        "one_day": "day",
        "24h": "day",
    },
    "note_type": {
        # AI 模型可能使用的各种变体
        "text": "image",
        "images": "image",
        "pictures": "image",
        "image_text": "image",
        "image_note": "image",       # Gemini/千问常用
        "image_notes": "image",
        "picture": "image",
        "photo": "image",
        "photos": "image",
        "graphic": "image",
        "graphics": "image",
        "video_note": "video",        # Gemini/千问常用
        "video_notes": "video",
        "videos": "video",
        "clip": "video",
        "clips": "video",
        "all_types": "all",
        "any": "all",
        "both": "all",
        "mixed": "all",
        "default": "all",
    },
    "sort_by": {
        # 更多排序别名
        "most_likes": "most_liked",
        "mostlikes": "most_liked",
        "likes": "most_liked",
        "like": "most_liked",
        "popular": "most_liked",
        "popularity": "most_liked",
        "hot": "most_liked",
        "most_comments": "most_commented",
        "mostcomments": "most_commented",
        "comments": "most_commented",
        "comment": "most_commented",
        "most_collects": "most_collected",
        "mostcollects": "most_collected",
        "collects": "most_collected",
        "collect": "most_collected",
        "favorites": "most_collected",
        "favorite": "most_collected",
        "bookmarks": "most_collected",
        "latest_posts": "latest",
        "latest_post": "latest",
        "newest": "latest",
        "recent": "latest",
        "new": "latest",
        "time": "latest",
        "date": "latest",
        "default": "comprehensive",
        "relevance": "comprehensive",
        "relevant": "comprehensive",
    },
}


def _canonical_value(field: str, value: str):
    if not isinstance(value, str):
        return value
    canonical_map = CHINESE_FILTERS.get(field, {})
    if value in canonical_map:
        return canonical_map[value]
    stripped = value.strip()
    if stripped in canonical_map:
        return canonical_map[stripped]
    alias_map = ALIAS_FILTERS.get(field, {})
    normalized = stripped.lower().replace(" ", "_")
    if normalized in alias_map:
        return alias_map[normalized]
    return stripped


# =============================================================================
# MCP Tools (恢复原始函数签名)
# =============================================================================

@mcp.tool()
async def ensure_login_status() -> dict:
    """Check XiaoHongShu login state inside the shared Chrome session."""
    logger.info("ensure_login_status called")
    response = await login_service.ensure_login_status()
    return response.model_dump()


@mcp.tool()
async def prepare_search(
    keyword: str,
    sort_by: str = "comprehensive",
    note_type: str = "all",
    publish_time: str = "any",
    search_scope: str = "all",
    location: str = "all",
    note_limit: int = 20,
) -> dict:
    """Navigate to the Xiaohongshu search result page and apply filters."""
    sort_by = _canonical_value("sort_by", sort_by)
    note_type = _canonical_value("note_type", note_type)
    publish_time = _canonical_value("publish_time", publish_time)
    search_scope = _canonical_value("search_scope", search_scope)
    location = _canonical_value("location", location)

    request = SearchRequest(
        keyword=keyword,
        sort_by=sort_by,  # type: ignore[arg-type]
        note_type=note_type,  # type: ignore[arg-type]
        publish_time=publish_time,  # type: ignore[arg-type]
        search_scope=search_scope,  # type: ignore[arg-type]
        location=location,  # type: ignore[arg-type]
        note_limit=note_limit,
    )
    logger.info("prepare_search called keyword={}", request.keyword)
    response = await search_service.run_search(request)
    return response.model_dump()


@mcp.tool()
async def collect_note_details(note_limit: int = 5) -> dict:
    """Collect note details from the current search results page."""
    logger.info("collect_note_details called note_limit={}", note_limit)
    response = await note_service.collect_note_details(note_limit=note_limit)
    return response.model_dump()


@mcp.tool()
async def auto_execute(
    keyword: str,
    sort_by: str = "comprehensive",
    note_type: str = "all",
    publish_time: str = "any",
    search_scope: str = "all",
    location: str = "all",
    note_limit: int = 20,
    login_retry_limit: int = 6,
    login_retry_interval: float = 5.0,
    auto_retry_after_login: bool = True,
) -> dict:
    """
    Run the default automation: ensure login, prepare the search page, and collect note details.
    """
    sort_by = _canonical_value("sort_by", sort_by)
    note_type = _canonical_value("note_type", note_type)
    publish_time = _canonical_value("publish_time", publish_time)
    search_scope = _canonical_value("search_scope", search_scope)
    location = _canonical_value("location", location)

    request = AutoWorkflowRequest(
        keyword=keyword,
        sort_by=sort_by,  # type: ignore[arg-type]
        note_type=note_type,  # type: ignore[arg-type]
        publish_time=publish_time,  # type: ignore[arg-type]
        search_scope=search_scope,  # type: ignore[arg-type]
        location=location,  # type: ignore[arg-type]
        note_limit=note_limit,
        login_retry_limit=login_retry_limit,
        login_retry_interval=login_retry_interval,
        auto_retry_after_login=auto_retry_after_login,
    )
    logger.info("auto_execute called keyword={} note_limit={}", keyword, note_limit)
    response = await automation_service.run_auto_workflow(request)
    # Optimize output for AI Agent
    from src.utils.output_cleaner import clean_auto_workflow_response
    logger.info("Cleaning response with keyword={}", keyword)
    return clean_auto_workflow_response(response.model_dump(), keyword=keyword)


@mcp.tool()
async def collect_author_notes(
    author_url: str,
    skip_note_ids: List[str] = None,
    note_limit: int = 50,
    comment_limit: int = 10,
) -> dict:
    """
    Collect notes from a specific author profile page.
    
    Args:
        author_url: The author profile URL (e.g., https://www.xiaohongshu.com/user/profile/{id})
        skip_note_ids: List of note IDs to skip (already collected in previous batches)
        note_limit: Maximum number of notes to collect in this batch
    
    Returns:
        Collection result with notes, progress info, and diagnostics
    """
    if skip_note_ids is None:
        skip_note_ids = []
    
    logger.info(
        "collect_author_notes called author_url={} note_limit={} skip_count={}",
        author_url, note_limit, len(skip_note_ids)
    )
    response = await author_service.collect_author_notes(
        author_url=author_url,
        skip_note_ids=skip_note_ids,
        note_limit=note_limit,
        comment_limit=comment_limit,
    )
    return response.model_dump()


@mcp.tool()
async def collect_note_by_link(
    note_url: str,
    comment_limit: int = 10,
) -> dict:
    """
    Collect all information from a single Xiaohongshu note by its link.
    
    Supports mobile share short links (e.g. http://xhslink.com/xxx) and
    full note URLs. Extracts title, content, images, videos, live photos,
    tags, engagement metrics (likes, collects, shares), and comments.
    
    Args:
        note_url: The note URL (supports short links and full URLs)
        comment_limit: Maximum number of comments to collect (default 10)
    
    Returns:
        Note detail with all extracted information
    """
    logger.info(
        "collect_note_by_link called note_url={} comment_limit={}",
        note_url, comment_limit
    )
    response = await note_link_service.collect_note_by_link(
        note_url=note_url,
        comment_limit=comment_limit,
    )
    return response.model_dump()


# =============================================================================
# REST API Layer (FastAPI with MCP mounted)
# =============================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# REST API 请求模型
class AutoExecuteRequest(BaseModel):
    keyword: str
    note_limit: int = 5
    sort_by: str = "comprehensive"
    note_type: str = "all"
    publish_time: str = "any"
    search_scope: str = "all"
    location: str = "all"
    login_retry_limit: int = 6
    login_retry_interval: float = 5.0
    auto_retry_after_login: bool = True


class CollectAuthorNotesRequest(BaseModel):
    """REST API请求模型：按作者采集笔记"""
    author_url: str = None  # 可选：直接的作者主页URL
    xhs_id: str = None  # 可选：小红书号，通过搜索方式进入博主页面（推荐！更接近人工操作）
    skip_note_ids: List[str] = []
    note_limit: int = 50
    comment_limit: int = 10
    collection_update_time: str = None
    initial_scan_limit: int = 10


class CollectNoteLinkRequest(BaseModel):
    """REST API请求模型：通过笔记链接采集"""
    note_url: str  # 笔记链接（支持手机分享短链和完整链接）
    comment_limit: int = 10  # 评论数量上限


# =============================================================================
# 创建主 FastAPI 应用（包含 REST API 端点）
# =============================================================================

# 获取 MCP 的 ASGI 应用
mcp_app = mcp.http_app(path="/mcp")

# 创建主 FastAPI 应用，使用 MCP 的 lifespan
app = FastAPI(
    title="3K RedNote MCP Server",
    description="MCP server with REST API for n8n integration",
    version="1.0.0",
    lifespan=mcp_app.lifespan,
)

# 添加 CORS 支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 MCP 到 /mcp 路径
app.mount("/mcp", mcp_app)


@app.get("/api/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "service": "3K RedNote MCP REST API"}

@app.post("/api/reload_cookies")
async def reload_cookies_endpoint():
    """
    热重载 Cookies：重新从 data/cookies.json 读取，清理 Chrome profile 缓存，
    并关闭当前 Chrome 进程，下次采集时会用新 cookies 启动。
    """
    from src.utils.cookie_storage import load_cookies
    from src.config import COOKIES_FILE, CHROME_USER_DATA_DIR
    from pathlib import Path
    import shutil

    result = {"steps": []}

    # 1. 验证 cookies 文件
    payload = load_cookies()
    if not payload:
        return {
            "success": False,
            "message": "cookies.json 文件不存在或格式错误",
            "cookies_path": str(COOKIES_FILE.resolve()),
        }

    cookie_count = len(payload.get("cookies", []))
    exported_at = payload.get("exported_at", "unknown")
    result["steps"].append(f"cookies_loaded={cookie_count}, exported_at={exported_at}")

    # 2. 检查关键 cookie 是否存在
    cookie_names = {c.get("name") for c in payload.get("cookies", [])}
    critical_cookies = {"web_session", "a1", "webId"}
    missing = critical_cookies - cookie_names
    if missing:
        result["steps"].append(f"WARNING: missing critical cookies: {missing}")
    else:
        result["steps"].append("all critical cookies present (web_session, a1, webId)")

    # 3. 清理 Chrome profile 缓存（避免旧 session 干扰）
    profile_dir = Path(CHROME_USER_DATA_DIR)
    cache_dirs = ["Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/Service Worker"]
    cleaned = []
    for subdir in cache_dirs:
        target = profile_dir / subdir
        if target.exists():
            try:
                shutil.rmtree(target, ignore_errors=True)
                cleaned.append(subdir)
            except Exception as e:
                result["steps"].append(f"cache_clean_error={subdir}: {e}")
    if cleaned:
        result["steps"].append(f"cleaned_cache_dirs={cleaned}")
    else:
        result["steps"].append("no_cache_dirs_to_clean")

    # 4. 关闭当前 Chrome 进程（下次采集会自动重启并注入新 cookies）
    try:
        await browser_guard.shutdown()
        result["steps"].append("chrome_shutdown=success")
    except Exception as e:
        result["steps"].append(f"chrome_shutdown_error={e}")

    result["success"] = True
    result["message"] = f"Cookies 热重载完成：{cookie_count} 个 cookies 已就绪，Chrome 已关闭，下次采集将使用新 cookies"
    result["cookie_count"] = cookie_count
    result["exported_at"] = exported_at
    return result


@app.get("/api/diagnose")
async def diagnose_endpoint():
    """
    诊断端点：快速检查服务状态、cookies、Chrome 连接等。
    """
    from src.utils.cookie_storage import load_cookies
    from src.config import COOKIES_FILE, CHROME_USER_DATA_DIR, CHROME_REMOTE_URL
    from pathlib import Path
    import httpx as _httpx

    diag = {}

    # 1. Cookies 状态
    payload = load_cookies()
    if payload:
        cookies = payload.get("cookies", [])
        cookie_names = [c.get("name") for c in cookies]
        diag["cookies"] = {
            "file": str(COOKIES_FILE.resolve()),
            "count": len(cookies),
            "exported_at": payload.get("exported_at"),
            "has_web_session": "web_session" in cookie_names,
            "has_a1": "a1" in cookie_names,
            "has_webId": "webId" in cookie_names,
            "names": cookie_names,
        }
    else:
        diag["cookies"] = {"error": "cookies.json missing or invalid", "file": str(COOKIES_FILE.resolve())}

    # 2. Chrome 状态
    try:
        async with _httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(f"{CHROME_REMOTE_URL}/json/version", timeout=3)
        diag["chrome"] = {"status": "online", "info": resp.json()}
    except Exception as e:
        diag["chrome"] = {"status": "offline", "error": str(e)}

    # 3. Chrome profile 目录
    profile_dir = Path(CHROME_USER_DATA_DIR)
    diag["chrome_profile"] = {
        "path": str(profile_dir.resolve()),
        "exists": profile_dir.exists(),
    }

    # 4. 环境变量
    diag["env"] = {
        "DEBUG_SCREENSHOT": os.environ.get("DEBUG_SCREENSHOT", "not set"),
        "CHROME_HEADLESS": os.environ.get("CHROME_HEADLESS", "not set"),
        "DATA_DIR": os.environ.get("DATA_DIR", "not set"),
    }

    return diag



@app.post("/api/auto_execute")
async def rest_auto_execute(request: AutoExecuteRequest):
    """
    REST API 端点：执行自动化采集流程
    
    供 n8n HTTP Request 节点直接调用，无需通过 AI Agent
    """
    try:
        logger.info("REST API: auto_execute called keyword={} note_limit={}", 
                    request.keyword, request.note_limit)
        
        # 标准化参数值
        sort_by = _canonical_value("sort_by", request.sort_by)
        note_type = _canonical_value("note_type", request.note_type)
        publish_time = _canonical_value("publish_time", request.publish_time)
        search_scope = _canonical_value("search_scope", request.search_scope)
        location = _canonical_value("location", request.location)
        
        # 直接调用 automation_service（绕过 MCP 装饰器）
        workflow_request = AutoWorkflowRequest(
            keyword=request.keyword,
            sort_by=sort_by,
            note_type=note_type,
            publish_time=publish_time,
            search_scope=search_scope,
            location=location,
            note_limit=request.note_limit,
            login_retry_limit=request.login_retry_limit,
            login_retry_interval=request.login_retry_interval,
            auto_retry_after_login=request.auto_retry_after_login,
        )
        
        response = await automation_service.run_auto_workflow(workflow_request)
        
        # 优化输出
        from src.utils.output_cleaner import clean_auto_workflow_response
        return clean_auto_workflow_response(response.model_dump(), keyword=request.keyword)
        
    except Exception as e:
        logger.error("REST API error: {}", str(e))
        return {
            "success": False,
            "error": str(e),
            "message": f"REST API 调用失败: {str(e)}"
        }


@app.post("/api/collect_author_notes")
async def rest_collect_author_notes(request: CollectAuthorNotesRequest):
    """
    REST API 端点：按作者采集笔记
    
    根据作者主页URL采集该作者发布的笔记。支持分批采集和去重。
    
    参数:
        author_url: 作者主页链接
        skip_note_ids: 已采集的笔记ID列表（用于去重）
        note_limit: 本批最多采集数量
    """
    try:
        # Check for Conditional Collection Logic
        current_limit = request.note_limit
        
        if request.collection_update_time:
            from datetime import datetime
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                # If collection time is NOT today (meaning it's a new day or catch-up),
                # restrict the limit to initial_scan_limit (default 10)
                if request.collection_update_time != today:
                    logger.info(f"Catch-up Mode: Last update {request.collection_update_time} != Today {today}. Enforcing limit {request.initial_scan_limit}")
                    current_limit = request.initial_scan_limit
            except Exception as e:
                logger.warning(f"Date comparison failed: {e}")

        logger.info(
            "REST API: collect_author_notes called author_url={} xhs_id={} note_limit={} (orig={}) skip_count={}",
            request.author_url, request.xhs_id, current_limit, request.note_limit, len(request.skip_note_ids)
        )
        
        import asyncio as _asyncio
        try:
            # Hard 15-min timeout: actually interrupts mid-execution (unlike deadline loop check).
            # Chrome is still shut down via the finally block in collect_author_notes.
            response = await _asyncio.wait_for(
                author_service.collect_author_notes(
                    author_url=request.author_url,
                    xhs_id=request.xhs_id,
                    skip_note_ids=request.skip_note_ids,
                    note_limit=current_limit,
                    comment_limit=request.comment_limit,
                ),
                timeout=900.0,  # 15 minutes (increased from 9 min to handle slow pages)
            )
            return response.model_dump()
        except _asyncio.TimeoutError:
            logger.warning("collect_author_notes reached 15-min hard timeout. Chrome cleaned up by finally block.")
            return {
                "success": False,
                "timed_out": True,
                "message": "采集超时（15分钟限制），请检查服务器性能或减少 note_limit",
                "notes": [],
            }
        
    except Exception as e:
        logger.error("REST API collect_author_notes error: {}", str(e))
        return {
            "success": False,
            "error": str(e),
            "message": f"按作者采集失败: {str(e)}"
        }


@app.post("/api/collect_note_by_link")
async def rest_collect_note_by_link(request: CollectNoteLinkRequest):
    """
    REST API 端点：通过笔记链接采集笔记信息
    
    支持手机分享短链（如 http://xhslink.com/xxx）和完整笔记链接。
    返回笔记的所有信息：标题、正文、图片、视频、Live图、标签、
    互动数据（点赞、收藏、转发）以及评论内容。
    
    参数:
        note_url: 笔记链接
        comment_limit: 评论数量上限（默认10条）
    """
    try:
        logger.info(
            "REST API: collect_note_by_link called note_url={} comment_limit={}",
            request.note_url, request.comment_limit
        )

        import asyncio as _asyncio
        try:
            response = await _asyncio.wait_for(
                note_link_service.collect_note_by_link(
                    note_url=request.note_url,
                    comment_limit=request.comment_limit,
                ),
                timeout=180.0,  # 3 minutes (single note should be fast)
            )
            return response.model_dump()
        except _asyncio.TimeoutError:
            logger.warning("collect_note_by_link reached 3-min hard timeout.")
            return {
                "success": False,
                "timed_out": True,
                "message": "采集超时（3分钟限制），请检查链接是否有效",
                "note": None,
            }

    except Exception as e:
        logger.error("REST API collect_note_by_link error: {}", str(e))
        return {
            "success": False,
            "error": str(e),
            "message": f"笔记链接采集失败: {str(e)}"
        }


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    transport = os.getenv("FASTMCP_TRANSPORT", "stdio")
    host = os.getenv("FASTMCP_HOST", "0.0.0.0")
    port = int(os.getenv("FASTMCP_PORT", "9431"))
    
    if transport == "stdio":
        # STDIO 模式：直接运行 MCP
        mcp.run(transport=transport)
    else:
        # HTTP 模式：运行 FastAPI 应用（包含 REST API 和挂载的 MCP）
        logger.info("Starting combined FastAPI + MCP server on port {}", port)
        logger.info("REST API endpoints:")
        logger.info("  - GET  http://{}:{}/api/health", host, port)
        logger.info("  - POST http://{}:{}/api/auto_execute", host, port)
        logger.info("  - POST http://{}:{}/api/collect_author_notes", host, port)
        logger.info("  - POST http://{}:{}/api/collect_note_by_link", host, port)
        logger.info("MCP endpoint:")
        logger.info("  - http://{}:{}/mcp", host, port)
        
        uvicorn.run(app, host=host, port=port, log_level="info")

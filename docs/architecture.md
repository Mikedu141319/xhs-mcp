## chrome-devtools MCP 〞 Login Flow Design

### Goals
- Control a persistent human-driven Chrome session via the DevTools Protocol (port 9222).
- Keep MCP headless: FastMCP exposes tools, Chrome performs page actions via CDP.
- Provide smart login handling so scans only happen when needed.

### Components
1. `server.py` 每 FastMCP entrypoint.
2. `src/config.py` 每 Environment + path management.
3. `src/clients/chrome_devtools.py` 每 Minimal CDP wrapper (create target, navigate, evaluate, wait).
4. `src/services/login_service.py` 每 DOM probe logic -> structured status.
5. `src/schemas/login.py` 每 Pydantic models.
6. `src/utils/logger.py` 每 Loguru configuration.

### Login States
| State | Detection | Action |
|-------|-----------|--------|
| logged_in | Feed cards detected, no login buttons | proceed |
| needs_qr_scan | Login modal/button present, QR available | return QR info |
| captcha_gate | URL contains `website-login/captcha` or captcha image | ask user to solve manually |
| browser_offline | CDP HTTP/WS errors | restart Chrome with remote debugging |
| unknown | fallback | request manual confirmation |

### Workflow
1. `ensure_login_status` navigates to `XHS_ENTRY_URL`.
2. Injects `LOGIN_PROBE_SCRIPT` to read DOM hints.
3. Maps hints to status + diagnostics + QR payload.
4. Future search/detail tools reuse same client.

### Deployment
- Dev: `FASTMCP_PORT=9431 python server.py`, Chrome launched with `chrome.exe --remote-debugging-port=9222 --user-data-dir=...`.
- Docker: `docker-compose up --build`, overriding env vars as needed.

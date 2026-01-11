# Repository Guidelines

## Project Structure & Module Organization
Root-level `server.py` registers FastMCP tools, while `.env.example` documents Chrome and path settings. Runtime artifacts are isolated under `data/` (downloaded QR snapshots) and `logs/` (Loguru output). All Python logic lives inside `src/`: `clients/` drives the Chrome DevTools protocol, `services/` orchestrates workflows such as `login_service.py`, `schemas/` defines the Pydantic payloads passed to tools, `utils/` configures logging, and `core/` hosts shared constants. Keep any new documentation in `docs/` to accompany the existing architecture note.

## Build, Test, and Development Commands
- `python -m venv .venv && .\.venv\Scripts\activate` - create/activate a local environment.
- `pip install -r requirements.txt` - install FastMCP, FastAPI, and supporting libraries.
- `FASTMCP_PORT=9431 python server.py` - run the MCP server over stdio (default Chrome controller).
- `FASTMCP_TRANSPORT=uvicorn FASTMCP_HOST=0.0.0.0 FASTMCP_PORT=9431 python server.py` - expose the toolset over WebSocket for remote agents.
- `docker-compose up --build` - build + run the same stack using the provided Dockerfile.

## Coding Style & Naming Conventions
Use Python 3.11+, PEP 8 spacing (4 spaces, 120-col soft limit), and snake_case identifiers for functions, async coroutines, and modules. Keep configuration constants in ALL_CAPS (see `src/config.py`) and favor explicit type hints plus docstrings, as already used in `src/clients/chrome_devtools.py`. Logging should go through `src/utils/logger.logger` so Loguru routing stays consistent.

## Testing Guidelines
No tests are checked in yet, so create a `tests/` package that mirrors `src/` (for example `tests/services/test_login_service.py`). Use `pytest -q` locally; prefer descriptive test names such as `test_login_service_flags_qr_flow`. When mocking Chrome responses, reuse fixtures from `data/` to keep inputs reproducible. Target minimum 80% coverage on `clients/` and `services/` before opening a PR.

## Commit & Pull Request Guidelines
Adopt concise, present-tense messages with a scope prefix (e.g., `feat: add captcha fallback` or `fix: tighten websocket timeout`). Reference related issue IDs in the body. PRs should describe the scenario, list manual test evidence (console output or screenshots showing login detection), and mention any new env vars or migration steps.

## Security & Configuration Tips
Copy `.env.example` to `.env` and never commit real credentials. Launch Chrome with `--remote-debugging-port=9222 --user-data-dir=<path>` so the MCP client can reuse the same profile. When sharing logs, scrub XHS identifiers but keep timestamps for traceability.

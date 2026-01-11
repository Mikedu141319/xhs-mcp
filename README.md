# Xiaohongshu MCP Tool

This is a Multi-Channel Platform (MCP) tool for automating interactions with Xiaohongshu (Little Red Book), including searching notes, collecting details, and parsing comments.

## 🚀 Deployment on a New Machine / 迁移部署指南

To run this tool on another computer, follow these steps:

### 1. Prerequisites / 环境准备
*   **Install Docker Desktop**: verify installation by running `docker --version` and `docker-compose --version` in terminal.
    *   下载并安装 Docker Desktop (Windows/Mac) 或 Docker Engine (Linux).

### 2. Copy Files / 文件拷贝
Copy the entire project folder `3k-rednote-mcp` to the new machine.
*   **Critical Files**:
    *   `docker-compose.yml`: Service configuration.
    *   `src/`: Source code.
    *   `cookies.json`: **Important** - Contains your login session. Copying this preserves your login state. (If you want a fresh login, delete this file on the new machine).
    *   `data/`: Contains downloaded notes and images.

### 3. Run the Service / 启动服务
1.  Open a terminal (PowerShell or CMD) and navigate to the project folder.
    ```bash
    cd path/to/3k-rednote-mcp
    ```
2.  Start the service using Docker Compose:
    ```bash
    docker-compose up -d --build
    ```
    *   `-d`: Run in background.
    *   `--build`: Rebuild images to ensure code changes are applied.

### 4. Verification / 验证
*   Check if the service is running:
    ```bash
    docker-compose ps
    ```
*   View logs:
    ```bash
    docker-compose logs -f
    ```

### 5. Troubleshooting / 常见问题
*   **Login Failed**: If `cookies.json` is invalid or expired, the tool log will show a login failure.
    *   **Solution**: Connect to the running container's browser via VNC or CDP to manually log in, OR delete `cookies.json` and restart to trigger a fresh login flow (if implemented).
*   **Permission Issues**: Ensure the user has read/write permissions for the project folder.

## 🛠️ Configuration
*   **`server.py`**: Main entry point defining MCP tools (`auto_execute`, etc.).
*   **`src/services/note_service.py`**: Core logic for note collection.
*   **`src/utils/output_cleaner.py`**: Logic for cleaning up the JSON output.

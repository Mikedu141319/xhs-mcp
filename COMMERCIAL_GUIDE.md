# 商业化部署方案 (Protective Distribution Strategy)

如果您希望商业化分发此工具，且**不希望暴露源代码**，最推荐的方案是使用 **Docker 镜像交付**。

## 核心原理
将您的源代码打包成一个“黑盒”（Docker Image），上传到云端仓库（Docker Hub 或 阿里云等）。客户只需要一个简单的配置文件即可运行，**无需拷贝 src 源代码文件夹**。

---

## 步骤 1：构建并上传镜像 (开发者操作)

在您的开发机上执行（假设您的 Docker Hub 账号是 `yourname`）：

1.  **登录 Docker Hub**:
    ```bash
    docker login
    ```

2.  **构建镜像 (Build)**:
    ```bash
    # 注意最后的 . 代表当前目录
    docker build -t yourname/xhs-mcp:v1.0 .
    ```

3.  **上传镜像 (Push)**:
    ```bash
    docker push yourname/xhs-mcp:v1.0
    ```

---

## 步骤 2：给客户发什么？ (交付物)

您只需要发给客户**一个文件夹**，里面包含 **2个文件** 和 **1个目录**：

1.  **`docker-compose.yml`** (修改版，见下文)
2.  **`cookies.json`** (如果需要预设登录)
3.  **`data/`** (空目录，用于保存客户的数据)

**绝对不要**发送 `src/`、`Dockerfile` 或 `requirements.txt` 给客户。

### 修改版 docker-compose.yml (给客户用的)

将原来的 `build: .` 替换为 `image: ...`：

```yaml
version: '3.9'
services:
  xhs-mcp:
    # 关键修改：不再使用本地代码构建，而是拉取您上传的镜像
    image: yourname/xhs-mcp:v1.0  
    ports:
      - 9431:9431
    # env_file: 
    #   - .env  <-- 商业化通常建议把关键配置直接写在 environment 里，或者给一个示例 .env
    environment:
      - DATA_DIR=/app/data
      - LOG_DIR=/app/logs
      # 如果有 license key 验证逻辑，可以在这里加
      # - LICENSE_KEY=xxxxxx 
    volumes:
      # 只挂载数据和日志，不挂载代码！
      - ./logs:/app/logs
      - ./data/cookies.json:/app/data/cookies.json
      - ./data/qr:/app/data/qr
      - ./data/captchas:/app/data/captchas
      # 注意：千万不要挂载 ./src:/app/src，否则容器里的代码会被本地空文件夹覆盖
```

---

## 步骤 3：客户如何使用？

客户拿到文件夹后，只需要安装 Docker Desktop，然后运行：

```bash
docker-compose up -d
```

Docker 会自动从云端下载您的 `yourname/xhs-mcp:v1.0` 镜像并运行。客户**看不到**镜像里的源代码（除非他们是非常专业的高手去逆向工程 Pyc文件，但这已经阻拦了99%的人）。

---

## 进阶保护 (防逆向)

如果需要更强的保护：

1.  **代码混淆 (Obfuscation)**: 在 `docker build` 之前使用 `PyArmor` 等工具加密 Python 代码。
2.  **私有仓库**: 如果不想公开镜像，可以使用阿里云容器镜像服务（私有版），客户 `docker login` 后才能下载。
3.  **License 验证**: 在代码启动时（`server.py`）加入联网验证逻辑，检查 License Key 是否有效，无效则 `sys.exit(1)`。

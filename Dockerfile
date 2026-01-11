FROM debian:bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /app

RUN set -eux; \
    echo 'deb https://mirrors.tuna.tsinghua.edu.cn/debian bookworm main' > /etc/apt/sources.list; \
    echo 'deb https://mirrors.tuna.tsinghua.edu.cn/debian bookworm-updates main' >> /etc/apt/sources.list; \
    echo 'deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main' >> /etc/apt/sources.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends wget gnupg ca-certificates python3 python3-pip python3-venv python3-dev build-essential; \
    ln -sf /usr/bin/python3 /usr/bin/python; \
    ln -sf /usr/bin/pip3 /usr/bin/pip; \
    mkdir -p /usr/share/keyrings; \
    wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg; \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list; \
    for attempt in 1 2 3; do \
    if apt-get update && apt-get install -y --no-install-recommends google-chrome-stable fonts-liberation fonts-noto-cjk; then \
    break; \
    fi; \
    echo "apt install failed, retrying (${attempt}/3)..." >&2; \
    if [ "$attempt" -eq 3 ]; then exit 1; fi; \
    sleep 5; \
    done; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

EXPOSE 9431

CMD ["python", "server.py"]

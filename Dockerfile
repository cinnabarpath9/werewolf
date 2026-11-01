FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（这样改代码不会让依赖层失效）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码与内容（.dockerignore 会挡掉 .env / games / var / dist）
COPY . .

# 云端持久化目录（Render 上可挂 disk；不挂也能跑，只是存档随重启丢失）
VOLUME ["/app/games", "/app/var"]

EXPOSE 8077

# --host 0.0.0.0 是 server.py 的默认值；这里显式写出来，免得被平台的环境覆盖搞混
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8077"]
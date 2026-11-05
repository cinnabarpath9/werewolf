FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（这样改代码不会让依赖层失效）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码与内容（.dockerignore 会挡掉 .env / games / var / dist / docs / tests）
COPY . .

# 构建期用**和发布包同一份规则**清洗 config，并断言（清不干净直接构建失败）。
RUN python -c "import sys; sys.path.insert(0, 'tools'); from release_rules import sanitize_file; sanitize_file('config.yaml')" \
 && rm -rf docs tests dist dist-play dist-exe games var experience voice_cache \
              tools/exam_2026*.yaml .git .github

# 云端持久化目录（Render 上可挂 disk；不挂也能跑，只是存档随重启丢失）
VOLUME ["/app/games", "/app/var"]

EXPOSE 8077

# 端口交给平台：server.py 会先读 $PORT（Render 注入），没有才退回 8077
CMD ["python", "-u", "server.py", "--host", "0.0.0.0"]